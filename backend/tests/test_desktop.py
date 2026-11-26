"""`surplus app` (ADR 0020): one-time sign-in codes, the launcher, browser choice, and the CLI flow."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from surplus import desktop
from surplus.api.app import create_app
from surplus.api.auth import SESSION_COOKIE, LoginCodes, load_or_create_token
from surplus.clock import FakeClock
from surplus.config import Paths, Settings

BASE = "http://127.0.0.1:8765"


# --- one-time codes --------------------------------------------------------------------------------------


def test_code_is_single_use_and_expires(clock: FakeClock) -> None:
    codes = LoginCodes(clock, ttl=timedelta(seconds=60))
    a = codes.issue()
    assert codes.redeem(a) and not codes.redeem(a)  # single use
    b = codes.issue()
    clock.advance(timedelta(seconds=61))
    assert not codes.redeem(b)  # expired
    assert not codes.redeem("made-up")


def test_codes_are_bounded(clock: FakeClock) -> None:
    codes = LoginCodes(clock, limit=3)
    first = codes.issue()
    for _ in range(3):
        codes.issue()
    assert not codes.redeem(first)  # pushed out by newer codes


@pytest.fixture
def client(paths: Paths, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(paths, Settings(), clock, background=False)
    with TestClient(app, base_url=BASE) as c:
        yield c


def test_sign_in_with_a_code(client: TestClient, paths: Paths) -> None:
    token = load_or_create_token(paths.api_token_file)
    assert client.post("/api/auth/code").status_code == 401  # minting needs the token
    r = client.post("/api/auth/code", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    code = r.json()["code"]
    assert token not in code
    got = client.get(f"/auth?code={code}", follow_redirects=False)
    assert got.status_code == 303 and got.cookies.get(SESSION_COOKIE) == token
    assert client.get("/api/status").status_code == 200
    client.cookies.clear()
    assert client.get(f"/auth?code={code}", follow_redirects=False).status_code == 401  # used up
    assert client.get("/auth", follow_redirects=False).status_code == 401


def test_favicon_is_served(client: TestClient) -> None:
    r = client.get("/favicon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


# --- launcher and browser --------------------------------------------------------------------------------


def test_desktop_entry() -> None:
    text = desktop.render_desktop_entry(Path("/opt/s p/bin/surplus"))
    assert text.startswith("[Desktop Entry]")
    assert "Exec='/opt/s p/bin/surplus' app\n" in text
    assert "StartupWMClass=Surplus" in text and "Icon=surplus" in text and "Terminal=false" in text
    assert "--port 9000" in desktop.render_desktop_entry(Path("/x/surplus"), 9000)


def test_install_and_remove_launcher(tmp_path: Path) -> None:
    env = {"XDG_DATA_HOME": str(tmp_path), "PATH": ""}
    entry = desktop.install_launcher(Path("/x/surplus"), env=env)
    icon = tmp_path / "icons/hicolor/scalable/apps/surplus.svg"
    assert entry == tmp_path / "applications/surplus.desktop" and entry.exists()
    assert icon.read_text().lstrip().startswith("<svg")
    assert set(desktop.remove_launcher(env)) == {entry, icon}
    assert desktop.remove_launcher(env) == []


def _exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_browser_order_and_override(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {"PATH": str(bin_dir)}
    assert desktop.find_browser(env) is None
    _exe(bin_dir / "chromium-browser", '#!/bin/sh\nexec /snap/bin/chromium "$@"\n')
    got = desktop.find_browser(env)
    assert got is not None and got.path.name == "chromium-browser" and got.snap
    _exe(bin_dir / "google-chrome")
    got = desktop.find_browser(env)
    assert got is not None and got.path.name == "google-chrome" and not got.snap
    _exe(bin_dir / "my-browser")
    got = desktop.find_browser({**env, "SURPLUS_APP_BROWSER": "my-browser"})
    assert got is not None and got.path.name == "my-browser"


def test_profile_dir_and_command(tmp_path: Path) -> None:
    snap = desktop.Browser(Path("/snap/bin/chromium"), snap=True)
    assert desktop.profile_dir(snap) == Path.home() / "snap/chromium/common/surplus-app"
    plain = desktop.Browser(Path("/usr/bin/google-chrome"), snap=False)
    assert desktop.profile_dir(plain, {"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "surplus/app-profile"
    cmd = desktop.browser_command(plain, f"{BASE}/auth?code=abc", tmp_path / "p")
    assert cmd[0] == "/usr/bin/google-chrome" and f"--app={BASE}/auth?code=abc" in cmd
    assert f"--user-data-dir={tmp_path / 'p'}" in cmd and "--class=Surplus" in cmd


# --- window placement ------------------------------------------------------------------------------------

XRANDR = """Monitors: 2
 0: +*HDMI-1-0 2560/597x1440/336+0+0  HDMI-1-0
 1: +eDP-1 1920/344x1080/193+2560+200  eDP-1
"""


def test_parse_monitors() -> None:
    main, laptop = desktop.parse_monitors(XRANDR)
    assert main == desktop.Monitor("HDMI-1-0", desktop.Rect(0, 0, 2560, 1440), primary=True)
    assert laptop.rect == desktop.Rect(2560, 200, 1920, 1080) and not laptop.primary
    assert desktop.parse_monitors("") == []


def test_default_bounds_fit_the_primary_screen() -> None:
    screens = desktop.parse_monitors(XRANDR)
    b = desktop.default_bounds(screens)
    assert b == desktop.Rect(560, 220, 1440, 1000) and screens[0].rect.contains(b)
    small = [desktop.Monitor("x", desktop.Rect(0, 0, 1366, 768), True)]
    sb = desktop.default_bounds(small)
    assert sb is not None and small[0].rect.contains(sb) and sb.h <= 768 * 0.8
    assert desktop.default_bounds([]) is None


def _prefs(profile: Path, left: int, top: int, right: int, bottom: int) -> None:
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    place = {"left": left, "top": top, "right": right, "bottom": bottom, "maximized": False}
    data = {"browser": {"app_window_placement": {"127": {"0": {"1_/auth": place}}}}}
    (profile / "Default/Preferences").write_text(json.dumps(data))


def test_saved_placement_is_kept_only_when_it_fits(tmp_path: Path) -> None:
    screens = desktop.parse_monitors(XRANDR)
    assert not desktop.saved_placement_fits(tmp_path, screens)  # nothing saved yet
    _prefs(tmp_path, 2560, 37, 3737, 1440)  # taller than the laptop screen, top above it (the real bug)
    assert not desktop.saved_placement_fits(tmp_path, screens)
    _prefs(tmp_path, 100, 100, 1500, 1100)
    assert desktop.saved_placement_fits(tmp_path, screens)


def test_bounds_go_on_the_command_line() -> None:
    b = desktop.Browser(Path("/usr/bin/chromium"), snap=False)
    cmd = desktop.browser_command(b, "u", Path("/p"), desktop.Rect(560, 220, 1440, 1000))
    assert "--window-position=560,220" in cmd and "--window-size=1440,1000" in cmd
    assert not any(c.startswith("--window-") for c in desktop.browser_command(b, "u", Path("/p")))


# --- CLI ---------------------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DATA", "CONFIG", "CLAUDE"):
        monkeypatch.setenv(f"SURPLUS_{name}_DIR", str(tmp_path / name))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgc"))
    monkeypatch.setattr(desktop, "notify", lambda message: None)
    monkeypatch.setattr(desktop, "monitors", list)
    return tmp_path


def test_app_opens_a_window_with_a_code_not_the_token(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    launched: list[list[str]] = []
    tokens: list[str] = []
    monkeypatch.setattr(desktop, "server_up", lambda base: True)
    monkeypatch.setattr(desktop, "login_code", lambda base, token: tokens.append(token) or "one-time")
    monkeypatch.setattr(desktop, "find_browser", lambda: desktop.Browser(Path("/usr/bin/chromium"), False))
    monkeypatch.setattr(desktop, "launch", launched.append)
    out = CliRunner().invoke(app, ["app"])
    assert out.exit_code == 0, out.output
    (cmd,) = launched
    assert f"--app={BASE}/auth?code=one-time" in cmd
    token = (cli_env / "CONFIG/api_token").read_text().strip()
    assert tokens == [token] and not any(token in part for part in cmd)


def test_app_without_a_server_or_service(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    monkeypatch.setattr(desktop, "server_up", lambda base: False)
    out = CliRunner().invoke(app, ["app"])
    assert out.exit_code == 1 and "not running" in out.output


def test_app_falls_back_to_the_default_browser(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    opened: list[str] = []
    monkeypatch.setattr(desktop, "server_up", lambda base: True)
    monkeypatch.setattr(desktop, "login_code", lambda base, token: "c1")
    monkeypatch.setattr(desktop, "find_browser", lambda: None)
    monkeypatch.setattr(desktop, "open_in_default_browser", opened.append)
    out = CliRunner().invoke(app, ["app"])
    assert out.exit_code == 0 and opened == [f"{BASE}/auth?code=c1"]


def test_app_install_and_uninstall_launcher(cli_env: Path) -> None:
    from surplus.cli import app

    run = CliRunner()
    out = run.invoke(app, ["app", "--install"])
    entry = cli_env / "xdg/applications/surplus.desktop"
    assert out.exit_code == 0 and entry.exists(), out.output
    assert os.access(entry, os.R_OK)
    assert "Removed" in run.invoke(app, ["app", "--uninstall"]).output and not entry.exists()


def test_login_code_against_an_old_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def fake_post(url: str, **kw: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr(desktop.httpx, "post", fake_post)
    with pytest.raises(desktop.SignInError, match="restart"):
        desktop.login_code(BASE, "t")
