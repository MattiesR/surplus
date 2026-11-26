"""`surplus app`: the dashboard in its own desktop window, plus an app-menu launcher (ADR 0020).

The server keeps running in the background (it has to, to record usage); this only gives it a window. The
window is a Chromium-family browser in app mode (no tabs, no address bar) with its own profile, so its sign-in
cookie is separate from your normal browsing. It signs in with a one-time code (`LoginCodes`), never with the
API token on a command line, where other local users could read it in `ps`.

Without a Chromium-family browser it falls back to a tab in your default browser.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

ICON = Path(__file__).resolve().parent / "icon.svg"
DESKTOP_FILE = "surplus.desktop"
WM_CLASS = "Surplus"
CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
    "vivaldi",
)


def data_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"))


def launcher_paths(env: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """(desktop entry, icon) under XDG_DATA_HOME."""
    base = data_home(env)
    return base / "applications" / DESKTOP_FILE, base / "icons/hicolor/scalable/apps/surplus.svg"


def render_desktop_entry(exe: Path, port: int = 8765) -> str:
    """The app-menu entry (pure). StartupWMClass ties the window to this icon in the dock."""
    exec_line = f"{shlex.quote(str(exe))} app" + (f" --port {port}" if port != 8765 else "")
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Surplus",
            "Comment=Put unused Claude subscription capacity to work",
            f"Exec={exec_line}",
            "Icon=surplus",
            "Terminal=false",
            "Categories=Utility;",
            f"StartupWMClass={WM_CLASS}",
            "StartupNotify=true",
            "",
        ]
    )


def install_launcher(exe: Path, port: int = 8765, env: Mapping[str, str] | None = None) -> Path:
    entry, icon = launcher_paths(env)
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, icon)
    entry.write_text(render_desktop_entry(exe, port), encoding="utf-8")
    entry.chmod(0o644)
    caches = (
        ["update-desktop-database", str(entry.parent)],
        ["gtk-update-icon-cache", "-q", "-t", str(icon.parents[2])],
    )
    for cmd in caches:  # best effort: GNOME also notices the new files by itself
        if shutil.which(cmd[0]):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    return entry


def remove_launcher(env: Mapping[str, str] | None = None) -> list[Path]:
    removed = []
    for path in launcher_paths(env):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


@dataclass(frozen=True)
class Browser:
    path: Path
    snap: bool


def _is_snap(path: Path) -> bool:
    if str(path).startswith("/snap/") or str(path.resolve()).startswith("/snap/"):
        return True
    try:  # Ubuntu's /usr/bin/chromium-browser is a shell wrapper around the snap
        head = path.read_bytes()[:2048]
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/bin/" in head


def find_browser(env: Mapping[str, str] | None = None) -> Browser | None:
    """A Chromium-family browser (they have an app mode); SURPLUS_APP_BROWSER overrides the search."""
    env = os.environ if env is None else env
    names = [env["SURPLUS_APP_BROWSER"]] if env.get("SURPLUS_APP_BROWSER") else list(CANDIDATES)
    for name in names:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return Browser(Path(found), _is_snap(Path(found)))
    return None


def profile_dir(browser: Browser, env: Mapping[str, str] | None = None) -> Path:
    """The window's own browser profile. A snap browser may only write in ~/snap/<name>/ or visible dirs."""
    if browser.snap:
        name = "chromium" if "chromium" in browser.path.name else browser.path.name
        return Path.home() / "snap" / name / "common" / "surplus-app"
    return data_home(env) / "surplus" / "app-profile"


def browser_command(browser: Browser, url: str, profile: Path) -> list[str]:
    """Pure. `url` must carry a one-time code, never the API token (the command line is public)."""
    return [
        str(browser.path),
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--class={WM_CLASS}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def launch(cmd: list[str]) -> None:
    """Start the window detached: closing the terminal does not close it."""
    subprocess.Popen(  # a fixed argument list, no shell
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_in_default_browser(url: str) -> None:
    webbrowser.open(url, new=1)


def server_up(base: str) -> bool:
    try:
        return httpx.get(f"{base}/api/health", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


class SignInError(RuntimeError):
    pass


def login_code(base: str, token: str) -> str:
    """Ask the running server for a one-time sign-in code (the token travels in a header, not a URL)."""
    try:
        r = httpx.post(f"{base}/api/auth/code", headers={"Authorization": f"Bearer {token}"}, timeout=5)
    except httpx.HTTPError as exc:
        raise SignInError(f"cannot reach Surplus at {base}: {exc}") from None
    if r.status_code == 404:
        raise SignInError(
            "the running server is older than `surplus app`: restart it (systemctl --user restart surplus)"
        )
    if r.status_code != 200:
        raise SignInError(f"the server refused a sign-in code (HTTP {r.status_code})")
    code = r.json().get("code")
    if not isinstance(code, str) or not code:
        raise SignInError("the server sent no sign-in code")
    return code


def notify(message: str) -> None:
    """Show an error when started from the app menu, where nobody sees the terminal."""
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "--app-name=Surplus", "-i", "surplus", "Surplus", message],
            capture_output=True,
            timeout=10,
            check=False,
        )
