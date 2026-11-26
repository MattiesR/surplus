# 0020 — A desktop window: `surplus app`

- Status: accepted · Date: 2026-11-26

## Context
The user asked for an application they can open from the desktop instead of a localhost tab in the browser.
The server has to keep running in the background anyway: it records usage and starts agent runs whether a
window is open or not (ADR 0019).

## Options
1. A packaged native shell (Electron, Tauri, pywebview + Qt/GTK). This means a large download or system packages,
   a second rendering stack to maintain, and its own update path.
2. **The dashboard in a Chromium-family browser in app mode** (`--app=URL`): its own window with no tabs and no
   address bar, an icon in the app menu and the dock, and no new dependency (Chromium is already installed).
3. A PWA installed by hand from the browser. This takes manual steps and is tied to the browser's main profile.

## Decision
Option 2, the safest of the three: it adds nothing to install and keeps one UI.

1. `surplus app` opens the window and `surplus app --install` adds `~/.local/share/applications/surplus.desktop`
   with an icon (`StartupWMClass=Surplus`). `surplus install` adds the launcher too, and `uninstall` removes it.
2. **No token on a command line.** On Linux, any local user can read a process's arguments (`ps`). So
   `surplus app` asks the running server for a **one-time sign-in code** with a bearer token in a header
   (`POST /api/auth/code`) and opens `/auth?code=…`. A code is valid for one sign-in within 60 s. Codes live
   only in memory and are capped at 20.
3. **Its own browser profile.** The window's sign-in cookie is not shared with normal browsing. Snap browsers
   can only write under `~/snap/<name>/`, so their profile lives there.
4. If the server is down and the service is installed, `surplus app` starts the service and waits. Errors go
   to a desktop notification when there is no terminal.
5. Without a Chromium-family browser it falls back to a tab in the default browser (Firefox has no app mode).
   `SURPLUS_APP_BROWSER` picks a specific browser.

## Consequences
- Closing the window does not stop Surplus. `surplus pause` or the dashboard's Pause button stops runs.
- The `?token=` sign-in link (`surplus link`) still works, for a browser of your choice.
