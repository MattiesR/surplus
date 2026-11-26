"""`surplus` command line (Typer)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from surplus.config import Paths, Settings

app = typer.Typer(help="Surplus: put unused AI subscription capacity to work.", no_args_is_help=True)
statusline_app = typer.Typer(help="Opt-in status-line spooler (passive percent source).")
app.add_typer(statusline_app, name="statusline")
runner_app = typer.Typer(help="Agent runner: container image and checks.")
app.add_typer(runner_app, name="runner")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_logging() -> None:
    import logging

    import structlog

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address. Keep it on loopback (plan §9.3).")] = "127.0.0.1",
    port: int = 8765,
    log_level: Annotated[str, typer.Option(help="uvicorn log level (info shows requests)")] = "warning",
) -> None:
    """Run surplusd: API, dashboard and the collector loop."""
    import uvicorn

    from surplus.api.app import create_app
    from surplus.api.auth import load_or_create_token

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho("Refusing to bind to a non-loopback address; use Tailscale for remote access.", fg="red")
        raise typer.Exit(2)
    _setup_logging()
    paths = Paths.from_env()
    paths.ensure()
    settings = Settings.from_env(host=host, port=port)
    token = load_or_create_token(paths.api_token_file)
    typer.secho(f"Open http://127.0.0.1:{port}/auth?token={token}", fg="green", err=True)
    uvicorn.run(create_app(paths, settings), host=host, port=port, log_level=log_level)


@app.command()
def ingest() -> None:
    """Ingest Claude Code logs once and print statistics."""
    from surplus.clock import SystemClock
    from surplus.collector.store import ingest_logs
    from surplus.db.session import make_engine, make_sessionmaker, migrate

    paths = Paths.from_env()
    paths.ensure()
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    stats = ingest_logs(make_sessionmaker(engine), paths.log_roots, SystemClock())
    engine.dispose()
    typer.echo(
        f"files={stats.files} changed={stats.files_changed} lines={stats.lines} bad={stats.bad_lines} "
        f"events={stats.events_seen} inserted={stats.events_inserted} in {stats.seconds:.2f}s"
    )
    for err in stats.errors:
        typer.secho(err, fg="yellow")


@app.command()
def probe() -> None:
    """Read limits and reset times now (zero-quota get_usage probe) and store them."""
    from surplus.clock import SystemClock
    from surplus.collector.service import Collector
    from surplus.db.session import make_engine, make_sessionmaker, migrate
    from surplus.domain.events import EventBus

    paths = Paths.from_env()
    paths.ensure()
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    collector = Collector(make_sessionmaker(engine), paths, Settings.from_env(), SystemClock(), EventBus())
    snap = asyncio.run(collector.probe_once())
    engine.dispose()
    if snap is None:
        typer.secho("probe failed (see log above)", fg="red")
        raise typer.Exit(1)
    for r in snap.readings:
        local = r.resets_at.astimezone().strftime("%a %d %b %H:%M") if r.resets_at else "-"
        typer.echo(f"{r.bucket:<24} {r.used_pct:5.1f}%   resets {local}")
    for s in snap.weekly_breakdown:
        typer.echo(f"  weekly via {s.display_name:<12} {s.pct_of_limit:5.1f}% of limit")


@app.command()
def status() -> None:
    """Print the latest stored readings (no network, no probe)."""
    from surplus.clock import SystemClock
    from surplus.collector.reset_watcher import project_reset
    from surplus.collector.store import latest_readings
    from surplus.db.session import make_engine, make_sessionmaker, migrate

    paths = Paths.from_env()
    if not paths.db_path.exists():
        typer.echo("No data yet. Run `surplus probe` or `surplus serve`.")
        raise typer.Exit(1)
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    with make_sessionmaker(engine)() as session:
        readings = latest_readings(session)
    engine.dispose()
    now = SystemClock().now()
    for r in readings:
        reset = (
            project_reset(r.resets_at, r.bucket, now).astimezone().strftime("%a %H:%M")
            if r.resets_at
            else "-"
        )
        age = int((now - r.ts).total_seconds() // 60)
        typer.echo(f"{r.bucket:<24} {r.used_pct:5.1f}%  resets {reset:<10} ({r.source.value}, {age} min ago)")


@app.command()
def doctor(
    no_probe: Annotated[bool, typer.Option("--no-probe", help="Skip the live limits probe")] = False,
) -> None:
    """Check the CLI, auth, Podman, logs, percent sources and channels."""
    from surplus.doctor import Level, run_all

    colors = {Level.OK: "green", Level.WARN: "yellow", Level.FAIL: "red"}
    checks = run_all(Paths.from_env(), Settings.from_env(), probe=not no_probe)
    for c in checks:
        typer.secho(f"[{c.level.value:>4}] {c.name:<24} {c.detail}", fg=colors[c.level])
    if any(c.level is Level.FAIL for c in checks):
        raise typer.Exit(1)


@app.command()
def simulate(
    profile: Annotated[str, typer.Option(help="light, heavy-weekday, irregular or all")] = "all",
    weeks: Annotated[int, typer.Option(min=1, max=52)] = 4,
    seed: Annotated[int, typer.Option()] = 1,
) -> None:
    """Replay a synthetic user through predictor, planner and guard rails with a fake runner (plan §13).

    Exits with 1 if any user-blocked incident happened.
    """
    from surplus.simulator.engine import SimConfig
    from surplus.simulator.engine import simulate as run_sim
    from surplus.simulator.profiles import PROFILES

    names = list(PROFILES) if profile == "all" else [profile]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise typer.BadParameter(f"unknown profile {unknown[0]}; choose from {', '.join(PROFILES)} or all")
    incidents = 0
    for name in names:
        res = run_sim(PROFILES[name], SimConfig(weeks=weeks, seed=seed))
        incidents += len(res.incidents)
        typer.echo(res.summary())
        for w in res.weeks:
            typer.echo(
                f"  week of {w.start:%Y-%m-%d}: you {w.user_pct:5.1f}% + agents {w.agent_pct:5.1f}%"
                f" = {w.total_pct:5.1f}% of the weekly limit"
            )
    if incidents:
        raise typer.Exit(1)


@app.command()
def openapi(out: Path = Path("openapi.json")) -> None:
    """Write the OpenAPI schema (the frontend's typed client is generated from it)."""
    import tempfile

    from surplus.api.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        paths = Paths(data_dir=base / "data", config_dir=base / "config", claude_dir=base / "claude")
        schema = create_app(paths, Settings(), background=False).openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n")
    typer.echo(f"wrote {out}")


@statusline_app.command("spool")
def statusline_spool() -> None:
    """Status-line command: spool rate limits from stdin, print a short status. Never fails."""
    try:
        from surplus.collector.sources.statusline import append_to_spool, spool_line

        line, text = spool_line(sys.stdin.read(), datetime.now(UTC))
        if line is not None:
            append_to_spool(Paths.from_env().statusline_spool, line)
        typer.echo(text)
    except Exception:
        typer.echo("")


@statusline_app.command("install")
def statusline_install(
    apply: Annotated[
        bool, typer.Option("--apply", help="Write ~/.claude/settings.json (a backup is made)")
    ] = False,
) -> None:
    """Show (or, with --apply, add) the statusLine entry. Refuses to replace an existing status line."""
    paths = Paths.from_env()
    settings_file = paths.claude_dir / "settings.json"
    exe = shutil.which("surplus") or str(Path(sys.argv[0]).resolve())
    entry = {"type": "command", "command": f"{exe} statusline spool"}
    current = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    if "statusLine" in current:
        typer.secho(
            "A statusLine is already configured; not replacing it. To feed Surplus as well, pipe its stdin "
            f"through `{entry['command']}` in your own script.",
            fg="yellow",
        )
        raise typer.Exit(1)
    if not apply:
        typer.echo(f"Would add to {settings_file}:\n" + json.dumps({"statusLine": entry}, indent=2))
        typer.echo("Re-run with --apply to write it.")
        return
    if settings_file.exists():
        backup = settings_file.with_suffix(f".json.surplus-backup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        shutil.copy2(settings_file, backup)
        typer.echo(f"Backup: {backup}")
    current["statusLine"] = entry
    settings_file.write_text(json.dumps(current, indent=2) + "\n")
    typer.secho("Status line installed.", fg="green")


if __name__ == "__main__":
    app()


@runner_app.command("build")
def runner_build(
    containerfile: Annotated[Path, typer.Option(help="Containerfile to build")] = REPO_ROOT
    / "runner-image"
    / "Containerfile",
    tag: Annotated[str | None, typer.Option(help="Image tag (default: the configured runner image)")] = None,
) -> None:
    """Build the runner image with rootless Podman (downloads Debian packages; no Claude quota)."""
    import subprocess

    image = tag or Settings.from_env().runner_image
    if not containerfile.is_file():
        typer.secho(f"{containerfile} not found", fg="red", err=True)
        raise typer.Exit(2)
    cmd = ["podman", "build", "--tag", image, "--file", str(containerfile), str(containerfile.parent)]
    typer.echo(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        raise typer.Exit(rc)
    typer.secho(f"built {image}", fg="green")
