"""Project files, registry, watcher and service (Phase 2 DoD lives in the API and watcher tests)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.config import Paths
from surplus.domain.events import EventBus
from surplus.projects import repo
from surplus.projects.registry import ProjectRegistry
from surplus.projects.service import InvalidDocumentError, ProjectService
from surplus.projects.watcher import ProjectWatcher


@pytest.fixture
def root(paths: Paths) -> Path:
    r = paths.data_dir.parent / "projects"
    r.mkdir()
    return r


@pytest.fixture
def registry(sm: sessionmaker[Session], root: Path, clock: FakeClock) -> ProjectRegistry:
    return ProjectRegistry(sm, root, clock, EventBus())


@pytest.fixture
def service(registry: ProjectRegistry, paths: Paths, tmp_path: Path) -> ProjectService:
    return ProjectService(registry, home=tmp_path / "home", protected=(paths.data_dir, paths.config_dir))


SPEC = repo.NewProject(
    name="My Cool Idea",
    description="A CSV tool.",
    goals=[("Ship a CLI", ["prints help"])],
    constraints=["Python 3.12"],
    test_command="uv run pytest -q",
)


def test_create_writes_expected_files(service: ProjectService, root: Path) -> None:
    snap = service.create(SPEC, None, git_init=False)
    folder = root / "my-cool-idea"
    assert snap.root == folder
    assert (folder / ".git").is_dir()
    meta = (folder / ".surplus/project.md").read_text()
    assert meta.startswith(
        "---\nid: my-cool-idea\nname: My Cool Idea\nstatus: active  # active | paused | archived\n"
    )
    assert 'test_command: "uv run pytest -q"' in meta or "test_command: uv run pytest -q" in meta
    assert "# My Cool Idea\n\nA CSV tool.\n\n## Tech constraints\n\n- Python 3.12\n" in meta
    goals = (folder / ".surplus/goals.md").read_text()
    assert "## G1. Ship a CLI\n\nAcceptance:\n- prints help\n" in goals
    assert (folder / ".surplus/backlog.md").read_text().startswith("# Backlog")
    assert (folder / ".surplus/runs").is_dir()
    assert snap.errors == []
    assert snap.meta is not None and snap.meta.test_command == "uv run pytest -q"


def test_create_refuses_duplicates(service: ProjectService) -> None:
    service.create(SPEC, None, git_init=False)
    with pytest.raises(FileExistsError):
        service.create(SPEC, None, git_init=False)


def test_existing_folder_requires_git_or_consent(service: ProjectService, tmp_path: Path) -> None:
    folder = tmp_path / "home" / "code" / "thing"
    folder.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a git repository"):
        service.create(repo.NewProject(name="Thing"), folder, git_init=False)
    snap = service.create(repo.NewProject(name="Thing"), folder, git_init=True)
    assert snap.root == folder
    rows = {r.id: r for r in service.registry.rows()}
    assert rows["thing"].registered


@pytest.mark.parametrize("bad", ["/", "/etc/x", "relative/path"])
def test_unsafe_paths(bad: str, tmp_path: Path) -> None:
    with pytest.raises(repo.UnsafePathError):
        repo.check_path_safe(Path(bad), tmp_path / "home")


def test_unsafe_home_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for p in (home, home.parent, home / ".ssh" / "x", home / ".config" / "proj"):
        with pytest.raises(repo.UnsafePathError):
            repo.check_path_safe(p, home)
    with pytest.raises(repo.UnsafePathError, match="Surplus"):
        repo.check_path_safe(home / "data" / "p", home, protected=(home / "data",))
    assert repo.check_path_safe(home / "code" / "p", home) == (home / "code" / "p").resolve()


def test_write_doc_conflict_and_validation(service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    text, h = snap.texts[repo.DocName.BACKLOG], snap.hashes[repo.DocName.BACKLOG]
    with pytest.raises(InvalidDocumentError) as exc:
        service.write_doc("my-cool-idea", repo.DocName.BACKLOG, text + "- [ ] T-1 (P1) bad\n", h)
    assert exc.value.line is not None
    new_hash = service.write_doc("my-cool-idea", repo.DocName.BACKLOG, text + "- [ ] T-001 (P1) ok\n", h)
    with pytest.raises(repo.ConflictError):
        service.write_doc("my-cool-idea", repo.DocName.BACKLOG, text, h)  # stale hash
    assert service.write_doc("my-cool-idea", repo.DocName.BACKLOG, text, new_hash)


def test_project_id_and_decisions_are_protected(service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    proj = snap.texts[repo.DocName.PROJECT]
    with pytest.raises(InvalidDocumentError, match="id cannot be changed"):
        service.write_doc(
            "my-cool-idea",
            repo.DocName.PROJECT,
            proj.replace("id: my-cool-idea", "id: other"),
            snap.hashes[repo.DocName.PROJECT],
        )
    dec = snap.texts[repo.DocName.DECISIONS]
    with pytest.raises(InvalidDocumentError, match="append-only"):
        service.write_doc(
            "my-cool-idea", repo.DocName.DECISIONS, "rewritten\n", snap.hashes[repo.DocName.DECISIONS]
        )
    service.write_doc(
        "my-cool-idea", repo.DocName.DECISIONS, dec + "\n## extra\n", snap.hashes[repo.DocName.DECISIONS]
    )


def test_settings_update_preserves_comments(service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    service.update_settings(
        "my-cool-idea", {"status": "paused", "priority": 1}, snap.hashes[repo.DocName.PROJECT]
    )
    text = (snap.root / ".surplus/project.md").read_text()
    assert "status: paused  # active | paused | archived\n" in text
    rows = {r.id: r for r in service.registry.rows()}
    assert (rows["my-cool-idea"].status, rows["my-cool-idea"].priority) == ("paused", 1)
    with pytest.raises(InvalidDocumentError):
        service.update_settings(
            "my-cool-idea", {"priority": 9}, repo.load(snap.root).hashes[repo.DocName.PROJECT]
        )


def test_backlog_operations_and_task_index(service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    h = snap.hashes[repo.DocName.BACKLOG]
    h = service.add_task("my-cool-idea", h, "First  task", 1, None, "it works")
    h = service.add_task("my-cool-idea", h, "Second", 2, None, None)
    h = service.update_task("my-cool-idea", "T-001", h, done=True, size="S")
    h = service.reorder_tasks("my-cool-idea", ["T-002", "T-001"], h)
    text = (snap.root / ".surplus/backlog.md").read_text()
    assert text.endswith("- [ ] T-002 (P2) Second\n- [x] T-001 (P1, ~S) First task — acceptance: it works\n")
    assert service.registry.task_counts()["my-cool-idea"] == {"open": 1, "done": 1}
    service.delete_task("my-cool-idea", "T-002", h)
    assert service.registry.task_counts()["my-cool-idea"] == {"done": 1}


def test_broken_file_is_indexed_with_error(registry: ProjectRegistry, service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    (snap.root / ".surplus/backlog.md").write_text("- [ ] broken\n")
    res = registry.index(snap.root)
    assert res is not None and res.error is not None and "does not match" in res.error


def test_scan_marks_missing(registry: ProjectRegistry, service: ProjectService) -> None:
    snap = service.create(SPEC, None, git_init=False)
    (snap.root / ".surplus/project.md").unlink()
    registry.scan_all()
    assert registry.rows()[0].missing
    assert registry.path_of("my-cool-idea") is None


async def test_watcher_picks_up_external_edit_within_2s(
    registry: ProjectRegistry, service: ProjectService
) -> None:
    """Phase 2 DoD: an edit in an external editor reaches the index (and the UI's SSE) within 2 s."""
    snap = service.create(SPEC, None, git_init=False)
    watcher = ProjectWatcher(registry, force_polling=False)
    task = asyncio.create_task(watcher.run_forever())
    async with registry.bus.subscribe() as q:
        await asyncio.sleep(0.3)  # let the watcher start
        backlog = snap.root / ".surplus/backlog.md"
        start = time.monotonic()
        backlog.write_text(backlog.read_text() + "\n- [ ] T-001 (P1) Edited in vim\n")
        event = await asyncio.wait_for(q.get(), timeout=2)
        elapsed = time.monotonic() - start
    task.cancel()
    assert event.type == "project.changed"
    assert event.data["id"] == "my-cool-idea"
    assert elapsed < 2
    assert registry.task_counts()["my-cool-idea"] == {"open": 1}


def test_new_repo_gets_a_first_commit_and_excludes_surplus(tmp_path: Path) -> None:
    import subprocess

    from surplus.projects import repo as repo_mod

    folder = tmp_path / "fresh"
    repo_mod.create(folder, repo_mod.NewProject(name="Fresh"), git_init_if_needed=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(folder), *args], capture_output=True, text=True, check=True
        ).stdout

    assert git("log", "--format=%s") == "chore: start project\n"
    assert git("status", "--porcelain", "--untracked-files=all") == ""  # .surplus/ is excluded locally
    assert ".surplus/" in (folder / ".git/info/exclude").read_text()


def test_existing_repo_is_left_alone(tmp_path: Path) -> None:
    import subprocess

    from surplus.projects import repo as repo_mod

    folder = tmp_path / "mine"
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    exclude_before = (folder / ".git/info/exclude").read_text()
    repo_mod.create(folder, repo_mod.NewProject(name="Mine"), git_init_if_needed=True)
    assert (folder / ".git/info/exclude").read_text() == exclude_before
    log = subprocess.run(["git", "-C", str(folder), "log"], capture_output=True, text=True, check=False)
    assert log.returncode != 0  # still no commits: Surplus does not commit into a repo it did not create
