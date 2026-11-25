"""Re-index projects when their files change on disk, e.g. in the user's own editor (plan §4.1).

DoD (Phase 2): an external edit shows up in the UI within 2 s. watchfiles debounces for 200 ms; the event
bus then pushes `project.changed` over SSE and the UI refetches.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import structlog
from watchfiles import Change, awatch

from surplus.projects.registry import ProjectRegistry

log = structlog.get_logger(__name__)

DEBOUNCE_MS = 200


def relevant(change: Change, path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith(".") and name != ".surplus":  # our own temp files, editor swap files
        return False
    return name.endswith(".md") or name == ".surplus" or change == Change.deleted


class ProjectWatcher:
    def __init__(self, registry: ProjectRegistry, force_polling: bool | None = None) -> None:
        self.registry = registry
        env = os.environ.get("SURPLUS_WATCH_POLL")
        self.force_polling = force_polling if force_polling is not None else (env == "1")
        self._restart = asyncio.Event()

    def restart(self) -> None:
        """Call after a project is added outside the root, so its folder gets watched too."""
        self._restart.set()

    async def run_forever(self) -> None:
        while True:
            dirs = self.registry.watched_dirs()
            if not dirs:
                # Nothing to watch yet (the projects root does not exist): check again later.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._restart.wait(), timeout=30)
                self._restart.clear()
                continue
            self._restart.clear()
            try:
                async for changes in awatch(
                    *dirs,
                    debounce=DEBOUNCE_MS,
                    step=50,
                    stop_event=self._restart,
                    force_polling=self.force_polling,
                    poll_delay_ms=300,
                    recursive=True,
                ):
                    await asyncio.to_thread(self._handle, changes)
            except Exception:
                log.exception("watcher.failed")
                await asyncio.sleep(5)

    def _handle(self, changes: set[tuple[Change, str]]) -> None:
        owners: set[Path] = set()
        new_root_folder = False
        for change, raw in changes:
            if not relevant(change, raw):
                continue
            path = Path(raw)
            owner = self.registry.owner_of(path)
            if owner is not None:
                owners.add(owner)
            elif path.parent == self.registry.root or path == self.registry.root:
                new_root_folder = True
        for owner in owners:
            self.registry.index(owner)
        if new_root_folder:
            self.registry.scan_all()
