"""Project registry: discovers project folders, indexes them into SQLite, and keeps the index fresh.

Discovery: every `<projects_root>/<folder>/.surplus/project.md`, plus folders registered explicitly (an
existing repo chosen in the wizard). The index (`project`, `task` tables) is derived data: deleting it and
rescanning loses nothing except which outside folders were registered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.db.models import ProjectRow, TaskRow
from surplus.domain.events import Event, EventBus
from surplus.projects import repo

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IndexResult:
    id: str
    changed: bool
    error: str | None = None


class ProjectRegistry:
    def __init__(self, sm: sessionmaker[Session], root: Path, clock: Clock, bus: EventBus) -> None:
        self.sm, self.root, self.clock, self.bus = sm, root, clock, bus

    # ------------------------------------------------------------------ discovery

    def discover(self) -> list[Path]:
        found: list[Path] = []
        if self.root.is_dir():
            found = sorted(p.parent.parent for p in self.root.glob(f"*/{repo.SURPLUS_DIR}/project.md"))
        with self.sm() as s:
            registered = [Path(p) for p in s.scalars(select(ProjectRow.path).where(ProjectRow.registered))]
        return found + [p for p in registered if p not in found]

    def scan_all(self) -> list[IndexResult]:
        results = []
        seen: set[str] = set()
        for folder in self.discover():
            res = self.index(folder)
            if res is not None:
                results.append(res)
                seen.add(str(folder.resolve()))
        with self.sm.begin() as s:
            for row in s.scalars(select(ProjectRow)):
                gone = row.path not in seen and not repo.ProjectFiles(Path(row.path)).exists()
                if gone != row.missing:
                    row.missing = gone
        return results

    def owner_of(self, changed: Path) -> Path | None:
        """The project folder a changed file belongs to (its `.surplus` parent), if any."""
        for parent in changed.parents:
            if parent.name == repo.SURPLUS_DIR:
                return parent.parent
        return None

    # ------------------------------------------------------------------ indexing

    def index(self, folder: Path, *, registered: bool | None = None) -> IndexResult | None:
        folder = folder.resolve()
        if not repo.ProjectFiles(folder).exists():
            with self.sm.begin() as s:
                row = s.scalar(select(ProjectRow).where(ProjectRow.path == str(folder)))
                if row is not None and not row.missing:
                    row.missing = True
                    self.bus.publish(Event("project.changed", {"id": row.id, "missing": True}))
            return None
        snap = repo.load(folder)
        pid = _project_id(snap, folder)
        now = self.clock.now()
        errors = [{"doc": e.doc.value, "message": e.message, "line": e.line} for e in snap.errors]
        with self.sm.begin() as s:
            existing = s.get(ProjectRow, pid)
            if existing is not None and existing.path != str(folder) and not existing.missing:
                msg = f"project id {pid!r} is already used by {existing.path}"
                log.warning("project.duplicate_id", id=pid, path=str(folder), other=existing.path)
                return IndexResult(pid, False, msg)
            by_path = s.scalar(select(ProjectRow).where(ProjectRow.path == str(folder)))
            if by_path is not None and by_path.id != pid:
                s.delete(by_path)  # the id in project.md was changed: re-key the index
                s.flush()
            changed = existing is None or existing.content_hash != snap.combined_hash or existing.missing
            row = existing or ProjectRow(id=pid, path=str(folder), registered=bool(registered))
            row.path = str(folder)
            row.name = snap.meta.name if snap.meta else str(snap.meta_values.get("name") or folder.name)
            row.status = snap.meta.status.value if snap.meta else "error"
            row.priority = snap.meta.priority if snap.meta else 5
            row.content_hash = snap.combined_hash
            row.errors_json = json.dumps(errors)
            row.indexed_at = now
            row.missing = False
            if registered is not None:
                row.registered = registered or row.registered
            if existing is None:
                s.add(row)
            s.flush()
            if snap.backlog is not None:
                _sync_tasks(s, pid, snap)
        if changed:
            log.info("project.indexed", id=pid, path=str(folder), errors=len(errors))
            self.bus.publish(Event("project.changed", {"id": pid}))
        return IndexResult(pid, changed, str(errors[0]["message"]) if errors else None)

    # ------------------------------------------------------------------ queries

    def rows(self, include_missing: bool = True) -> list[ProjectRow]:
        with self.sm() as s:
            stmt = select(ProjectRow).order_by(ProjectRow.priority, ProjectRow.name)
            if not include_missing:
                stmt = stmt.where(ProjectRow.missing.is_(False))
            return list(s.scalars(stmt))

    def path_of(self, project_id: str) -> Path | None:
        with self.sm() as s:
            row = s.get(ProjectRow, project_id)
            return None if row is None or row.missing else Path(row.path)

    def watched_dirs(self) -> list[Path]:
        dirs = [self.root] if self.root.is_dir() else []
        for row in self.rows(include_missing=False):
            p = Path(row.path)
            if not (p == self.root or self.root in p.parents):
                dirs.append(p / repo.SURPLUS_DIR)
        return dirs

    def task_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with self.sm() as s:
            for pid, state in s.execute(select(TaskRow.project_id, TaskRow.state)):
                out.setdefault(pid, {}).setdefault(state, 0)
                out[pid][state] += 1
        return out

    def last_indexed(self, project_id: str) -> datetime | None:
        with self.sm() as s:
            row = s.get(ProjectRow, project_id)
            return None if row is None else row.indexed_at


def _project_id(snap: repo.ProjectSnapshot, folder: Path) -> str:
    if snap.meta is not None:
        return snap.meta.id
    raw = snap.meta_values.get("id")
    if isinstance(raw, str) and repo.ID_RE.match(raw):
        return raw
    return repo.slugify(folder.name)


def _sync_tasks(s: Session, pid: str, snap: repo.ProjectSnapshot) -> None:
    assert snap.backlog is not None
    items = snap.backlog.items
    ids = [i.id for i in items]
    s.execute(delete(TaskRow).where(TaskRow.project_id == pid, TaskRow.backlog_id.not_in(ids)))
    for pos, item in enumerate(items):
        state = (
            "done"
            if item.done
            else "proposed"
            if item.proposed
            else "blocked"
            if "blocked" in item.flags
            else "open"
        )
        values = {
            "project_id": pid,
            "backlog_id": item.id,
            "title": item.title,
            "state": state,
            "priority": item.priority,
            "est_size": item.size.value if item.size else None,
            "position": pos,
        }
        stmt = insert(TaskRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "backlog_id"],
            set_={k: stmt.excluded[k] for k in ("title", "state", "priority", "est_size", "position")},
        )
        s.execute(stmt)
