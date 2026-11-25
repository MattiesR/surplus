"""Project operations used by the API. Every write goes to the Markdown files first, then re-indexes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from surplus.projects import backlog as bl
from surplus.projects import frontmatter, repo
from surplus.projects.registry import ProjectRegistry


class NotFoundError(KeyError):
    pass


class InvalidDocumentError(ValueError):
    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(message)


@dataclass
class ProjectService:
    registry: ProjectRegistry
    home: Path
    protected: tuple[Path, ...]
    on_new_watch_dir: Callable[[], None] = lambda: None

    # ------------------------------------------------------------------ lookup

    def folder(self, project_id: str) -> Path:
        path = self.registry.path_of(project_id)
        if path is None:
            raise NotFoundError(project_id)
        return path

    def snapshot(self, project_id: str) -> repo.ProjectSnapshot:
        return repo.load(self.folder(project_id))

    # ------------------------------------------------------------------ create

    def create(
        self, spec: repo.NewProject, existing_path: Path | None, git_init: bool
    ) -> repo.ProjectSnapshot:
        pid = repo.slugify(spec.name)
        if self.registry.path_of(pid) is not None:
            raise FileExistsError(f"a project with id {pid!r} already exists")
        if existing_path is None:
            folder = self.registry.root / pid
            if folder.exists() and any(folder.iterdir()):
                raise FileExistsError(f"{folder} already exists and is not empty")
            git_init = True
        else:
            folder = repo.check_path_safe(existing_path, self.home, self.protected)
            if not folder.is_dir():
                raise FileNotFoundError(f"{folder} is not an existing folder")
        repo.check_path_safe(folder, self.home, self.protected)
        snap = repo.create(folder, spec, git_init_if_needed=git_init, project_id=pid)
        outside_root = folder.parent != self.registry.root.resolve()
        self.registry.index(folder, registered=outside_root)
        self.on_new_watch_dir()
        return snap

    # ------------------------------------------------------------------ whole-file edits

    def write_doc(self, project_id: str, doc: repo.DocName, text: str, base_hash: str) -> str:
        folder = self.folder(project_id)
        validate_doc(doc, text, project_id)
        if doc is repo.DocName.DECISIONS:
            current, _ = repo.ProjectFiles(folder).read(doc)
            if not text.startswith(current):
                raise InvalidDocumentError("decisions.md is append-only: existing entries cannot be changed")
        new_hash = repo.ProjectFiles(folder).write(doc, text, base_hash)
        self.registry.index(folder)
        return new_hash

    # ------------------------------------------------------------------ settings (front matter)

    def update_settings(self, project_id: str, changes: dict[str, frontmatter.Scalar], base_hash: str) -> str:
        folder = self.folder(project_id)
        files = repo.ProjectFiles(folder)
        text, current = files.read(repo.DocName.PROJECT)
        if current != base_hash:
            raise repo.ConflictError(current)
        fm = frontmatter.parse(text)
        for key, value in changes.items():
            if key == "id":
                raise InvalidDocumentError("the project id cannot be changed")
            fm.set(key, value)
        new_text = fm.render()
        validate_doc(repo.DocName.PROJECT, new_text, project_id)
        new_hash = files.write(repo.DocName.PROJECT, new_text, base_hash)
        self.registry.index(folder)
        return new_hash

    # ------------------------------------------------------------------ backlog operations

    def _edit_backlog(self, project_id: str, base_hash: str, fn: Callable[[bl.Backlog], None]) -> str:
        folder = self.folder(project_id)
        files = repo.ProjectFiles(folder)
        text, current = files.read(repo.DocName.BACKLOG)
        if current != base_hash:
            raise repo.ConflictError(current)
        try:
            doc = bl.parse(text)
        except bl.BacklogError as exc:
            raise InvalidDocumentError(f"fix backlog.md first: {exc.message}", exc.line) from exc
        try:
            fn(doc)
        except (KeyError, ValueError) as exc:
            raise InvalidDocumentError(str(exc)) from exc
        new_hash = files.write(repo.DocName.BACKLOG, doc.render(), base_hash)
        self.registry.index(folder)
        return new_hash

    def edit_backlog_latest(self, project_id: str, fn: Callable[[bl.Backlog], None], retries: int = 3) -> str:
        """Apply `fn` to the current backlog.md, re-reading and re-applying on a concurrent edit.

        For Surplus's own edits (run results), where there is no editor-held base hash to protect.
        """
        files = repo.ProjectFiles(self.folder(project_id))
        for _ in range(retries):
            _, current = files.read(repo.DocName.BACKLOG)
            try:
                return self._edit_backlog(project_id, current, fn)
            except repo.ConflictError:
                continue
        raise repo.ConflictError(files.read(repo.DocName.BACKLOG)[1])

    def add_task(
        self,
        project_id: str,
        base_hash: str,
        title: str,
        priority: int,
        size: bl.Size | None,
        acceptance: str | None,
        proposed: bool = False,
    ) -> str:
        def fn(doc: bl.Backlog) -> None:
            doc.add(
                bl.BacklogItem(
                    id=doc.next_id(),
                    title=" ".join(title.split()),
                    priority=priority,
                    size=size,
                    acceptance=" ".join(acceptance.split()) if acceptance and acceptance.strip() else None,
                    flags=["proposed"] if proposed else [],
                )
            )

        return self._edit_backlog(project_id, base_hash, fn)

    def update_task(self, project_id: str, task_id: str, base_hash: str, **fields: object) -> str:
        def fn(doc: bl.Backlog) -> None:
            item = doc.get(task_id)
            if "title" in fields:
                item.title = " ".join(str(fields["title"]).split())
            if "priority" in fields:
                item.priority = int(str(fields["priority"]))
            if "size" in fields:
                item.size = bl.Size(str(fields["size"])) if fields["size"] else None
            if "done" in fields:
                item.done = bool(fields["done"])
            if "acceptance" in fields:
                acc = fields["acceptance"]
                item.acceptance = " ".join(str(acc).split()) if acc and str(acc).strip() else None
            if "proposed" in fields:
                flags = [f for f in item.flags if f != "proposed"]
                item.flags = [*flags, "proposed"] if fields["proposed"] else flags
            if "blocked" in fields:
                flags = [f for f in item.flags if f != "blocked"]
                item.flags = [*flags, "blocked"] if fields["blocked"] else flags
            item.validate()

        return self._edit_backlog(project_id, base_hash, fn)

    def delete_task(self, project_id: str, task_id: str, base_hash: str) -> str:
        def fn(doc: bl.Backlog) -> None:
            doc.remove(task_id)

        return self._edit_backlog(project_id, base_hash, fn)

    def reorder_tasks(self, project_id: str, ids: list[str], base_hash: str) -> str:
        return self._edit_backlog(project_id, base_hash, lambda doc: doc.reorder(ids))


def validate_doc(doc: repo.DocName, text: str, project_id: str) -> None:
    """Reject a whole-file edit that the parsers would not accept, with the offending line when known."""
    if doc is repo.DocName.PROJECT:
        try:
            fm = frontmatter.parse(text)
        except frontmatter.FrontMatterError as exc:
            raise InvalidDocumentError(str(exc), exc.line) from exc
        try:
            meta = repo.ProjectMeta.model_validate(fm.values)
        except ValueError as exc:
            raise InvalidDocumentError(f"invalid settings: {exc}") from exc
        if meta.id != project_id:
            raise InvalidDocumentError("the project id cannot be changed")
    elif doc is repo.DocName.BACKLOG:
        try:
            bl.parse(text)
        except bl.BacklogError as exc:
            raise InvalidDocumentError(exc.message, exc.line) from exc
