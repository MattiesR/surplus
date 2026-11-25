"""One project folder on disk: its `.surplus/*.md` files, atomic writes, creation, path safety.

The Markdown files are the source of truth for intent (plan §4.1). Every write is atomic (temp file +
rename) and guarded by the hash the editor started from, so an edit made in an external editor is never
silently overwritten.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from surplus.projects import backlog as backlog_mod
from surplus.projects import documents, frontmatter

SURPLUS_DIR = ".surplus"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class DocName(StrEnum):
    PROJECT = "project"
    GOALS = "goals"
    BACKLOG = "backlog"
    DECISIONS = "decisions"

    @property
    def filename(self) -> str:
        return f"{self.value}.md"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class ProjectMeta(BaseModel):
    """project.md front matter. Unknown keys are preserved in the file but ignored here."""

    id: str = Field(pattern=ID_RE.pattern)
    name: str = Field(min_length=1, max_length=120)
    status: ProjectStatus = ProjectStatus.ACTIVE
    priority: int = Field(default=2, ge=1, le=5)
    model: str = Field(default="sonnet", pattern=r"^(haiku|sonnet|opus)$")
    max_share_of_surplus: float = Field(default=0.5, gt=0, le=1)
    test_command: str | None = None
    # Runner overrides for this project (ADR 0008). Unset = the global defaults in Settings.
    effort: Effort | None = None
    max_turns: int | None = Field(default=None, ge=1, le=200)
    run_timeout_minutes: int | None = Field(default=None, ge=5, le=240)
    max_five_hour_pct: float | None = Field(default=None, ge=10, le=95)
    max_weekly_pct: float | None = Field(default=None, ge=10, le=95)


class ConflictError(Exception):
    """The file changed on disk since the caller read it."""

    def __init__(self, current_hash: str) -> None:
        self.current_hash = current_hash
        super().__init__("file changed on disk")


class UnsafePathError(ValueError):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return slug or "project"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed temp name: the watcher ignores it.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class ProjectFiles:
    root: Path  # the project folder (git repo)

    @property
    def meta_dir(self) -> Path:
        return self.root / SURPLUS_DIR

    @property
    def runs_dir(self) -> Path:
        return self.meta_dir / "runs"

    def path(self, doc: DocName) -> Path:
        return self.meta_dir / doc.filename

    def read(self, doc: DocName) -> tuple[str, str]:
        """(text, hash). A missing optional file reads as empty with the hash of b''."""
        p = self.path(doc)
        data = p.read_bytes() if p.exists() else b""
        return data.decode("utf-8"), content_hash(data)

    def write(self, doc: DocName, text: str, base_hash: str | None) -> str:
        """Write if the file still has `base_hash` (None = unconditional). Returns the new hash."""
        if base_hash is not None:
            _, current = self.read(doc)
            if current != base_hash:
                raise ConflictError(current)
        atomic_write(self.path(doc), text)
        return content_hash(text.encode("utf-8"))

    def exists(self) -> bool:
        return self.path(DocName.PROJECT).is_file()


# ---------------------------------------------------------------- loading


@dataclass
class DocError:
    doc: DocName
    message: str
    line: int | None = None


@dataclass
class ProjectSnapshot:
    root: Path
    meta: ProjectMeta | None
    meta_values: dict[str, frontmatter.Scalar]
    description_md: str
    goals: documents.GoalsDoc
    backlog: backlog_mod.Backlog | None
    decisions: list[documents.Decision]
    runs: list[str]
    texts: dict[DocName, str]
    hashes: dict[DocName, str]
    errors: list[DocError] = field(default_factory=list)

    @property
    def combined_hash(self) -> str:
        return content_hash("".join(self.hashes[d] for d in DocName).encode())


def load(root: Path) -> ProjectSnapshot:
    files = ProjectFiles(root)
    texts: dict[DocName, str] = {}
    hashes: dict[DocName, str] = {}
    for doc in DocName:
        texts[doc], hashes[doc] = files.read(doc)
    errors: list[DocError] = []

    meta: ProjectMeta | None = None
    values: dict[str, frontmatter.Scalar] = {}
    body = ""
    try:
        fm = frontmatter.parse(texts[DocName.PROJECT])
        values, body = dict(fm.values), fm.body
        meta = ProjectMeta.model_validate(values)
    except frontmatter.FrontMatterError as exc:
        errors.append(DocError(DocName.PROJECT, str(exc), exc.line))
    except ValidationError as exc:
        msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        errors.append(DocError(DocName.PROJECT, msg))

    bl: backlog_mod.Backlog | None = None
    try:
        bl = backlog_mod.parse(texts[DocName.BACKLOG])
    except backlog_mod.BacklogError as exc:
        errors.append(DocError(DocName.BACKLOG, exc.message, exc.line))

    runs = (
        sorted((p.name for p in files.runs_dir.glob("*.md")), reverse=True) if files.runs_dir.is_dir() else []
    )
    return ProjectSnapshot(
        root=root,
        meta=meta,
        meta_values=values,
        description_md=body,
        goals=documents.parse_goals(texts[DocName.GOALS]),
        backlog=bl,
        decisions=documents.parse_decisions(texts[DocName.DECISIONS]),
        runs=runs,
        texts=texts,
        hashes=hashes,
        errors=errors,
    )


# ---------------------------------------------------------------- creation


def render_project_md(meta: ProjectMeta, description: str, constraints: list[str]) -> str:
    fm = frontmatter.FrontMatter(lines=[], body="")
    for key, value, comment in (
        ("id", meta.id, None),
        ("name", meta.name, None),
        ("status", meta.status.value, "active | paused | archived"),
        ("priority", meta.priority, "1 = highest"),
        ("model", meta.model, "default model for runs"),
        ("max_share_of_surplus", meta.max_share_of_surplus, "share of surplus capacity this project may use"),
        ("test_command", meta.test_command, None),
    ):
        fm.set(key, value)
        if comment:
            fm.lines[-1].raw += f"  # {comment}"
    desc = description.strip() or "_Describe the project in one paragraph._"
    body = f"\n# {meta.name}\n\n{desc}\n"
    if constraints:
        body += "\n## Tech constraints\n\n" + "".join(f"- {' '.join(c.split())}\n" for c in constraints)
    fm.body = body
    return fm.render()


SYSTEM_DIRS = tuple(
    Path(s)
    for s in (
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/var",
        "/boot",
        "/proc",
        "/sys",
        "/dev",
        "/run",
        "/opt",
        "/root",
        "/snap",
    )
)


def check_path_safe(
    path: Path, home: Path, protected: tuple[Path, ...] = (), system: tuple[Path, ...] = SYSTEM_DIRS
) -> Path:
    """Refuse folders an agent must never be given write access to (plan §9, G5)."""
    p = path.expanduser()
    if not p.is_absolute():
        raise UnsafePathError("use an absolute path")
    p = p.resolve()
    home = home.resolve()
    if p == Path("/") or any(p == s or s in p.parents for s in system):
        raise UnsafePathError(f"{p} is a system location")
    if p == home or p in home.parents:
        raise UnsafePathError(
            "pick a dedicated project folder, not your home directory or one of its parents"
        )
    if home in p.parents:
        first = p.relative_to(home).parts[0]
        if first.startswith("."):
            raise UnsafePathError(f"~/{first} holds configuration or secrets; pick another folder")
    for prot in protected:
        prot = prot.resolve()
        if p == prot or prot in p.parents or p in prot.parents:
            raise UnsafePathError(f"{p} overlaps Surplus's own data ({prot})")
    return p


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True, timeout=30
    )


@dataclass
class NewProject:
    name: str
    description: str = ""
    goals: list[tuple[str, list[str]]] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    priority: int = 2
    model: str = "sonnet"
    test_command: str | None = None


def create(
    folder: Path, spec: NewProject, *, git_init_if_needed: bool, project_id: str | None = None
) -> ProjectSnapshot:
    """Write the .surplus files into `folder` (created if missing). Never overwrites an existing project."""
    files = ProjectFiles(folder)
    if files.exists():
        raise FileExistsError(f"{folder} already has a {SURPLUS_DIR}/project.md")
    folder.mkdir(parents=True, exist_ok=True)
    created_repo = False
    if not is_git_repo(folder):
        if not git_init_if_needed:
            raise ValueError(f"{folder} is not a git repository")
        git_init(folder)
        created_repo = True
    meta = ProjectMeta(
        id=project_id or slugify(spec.name),
        name=" ".join(spec.name.split()),
        priority=spec.priority,
        model=spec.model,
        test_command=spec.test_command,
    )
    atomic_write(files.path(DocName.GOALS), documents.render_new_goals(spec.goals))
    atomic_write(files.path(DocName.BACKLOG), backlog_mod.NEW_BACKLOG)
    atomic_write(files.path(DocName.DECISIONS), documents.NEW_DECISIONS)
    files.runs_dir.mkdir(parents=True, exist_ok=True)
    # project.md last: its presence marks the project as complete for the scanner.
    atomic_write(files.path(DocName.PROJECT), render_project_md(meta, spec.description, spec.constraints))
    if created_repo:
        start_repo(folder)
    return load(folder)


def start_repo(folder: Path) -> None:
    """For a repository Surplus created: keep `.surplus/` out of git (repo-local exclude) and make an empty
    first commit, so runs have a base to branch from. Uses the user's git identity when one is configured."""
    exclude = folder / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text() if exclude.exists() else ""
    if ".surplus/" not in current.split():
        exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + ".surplus/\n")
    base = ["git", "-C", str(folder), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    ident = subprocess.run(
        [*base, "var", "GIT_COMMITTER_IDENT"], capture_output=True, timeout=30, check=False
    )
    if ident.returncode != 0:
        base += ["-c", "user.name=Surplus", "-c", "user.email=surplus@localhost"]
    subprocess.run(
        [*base, "commit", "--allow-empty", "-q", "-m", "chore: start project"],
        check=True,
        capture_output=True,
        timeout=30,
    )
