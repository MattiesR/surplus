"""The Inbox (plan §7.4, §11, ADR 0018): agent questions waiting for the user, and answering them.

`decisions.md` stays the single authority (plan §4.1). An answer is appended there as a decision whose heading
names the task, the run and the question (``## 2026-11-26 10:00 UTC · T-004 · r7 Q1``). A question is
answered when such an entry exists. So answering by hand in the editor works too, and nothing about it lives
only in the database. The next run of the task gets the answer in its prompt (decisions that mention the task
come first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.db.models import RunRow
from surplus.domain.events import Event, EventBus
from surplus.projects import documents, repo
from surplus.projects.service import ProjectService
from surplus.runner.report import RunReport
from surplus.runner.service import run_name

log = structlog.get_logger(__name__)
SKIPPED = "(no answer: use your best judgment and say so in the report)"


class InboxError(ValueError):
    pass


@dataclass(frozen=True)
class PendingQuestion:
    project_id: str
    project_name: str
    run_id: int
    task_id: str | None
    task_title: str | None
    question_id: str
    text: str
    options: tuple[str, ...]
    recommended: str | None
    asked_at: datetime

    @property
    def ref(self) -> str:
        return f"{self.task_id + ' · ' if self.task_id else ''}{run_name(self.run_id)} {self.question_id}"


def answered_refs(decisions: list[documents.Decision]) -> set[tuple[int, str]]:
    """(run id, question id) pairs that decisions.md already answers."""
    out: set[tuple[int, str]] = set()
    for d in decisions:
        for m in re.finditer(r"\br(\d+) (Q\d{1,3})\b", d.heading):
            out.add((int(m.group(1)), m.group(2)))
    return out


@dataclass(frozen=True)
class TaskQuestions:
    """Per task: questions still open, and when the newest answer was given (for selection)."""

    pending: bool
    answered_at: datetime | None


class Inbox:
    def __init__(
        self, sm: sessionmaker[Session], clock: Clock, bus: EventBus, projects: ProjectService
    ) -> None:
        self.sm, self.clock, self.bus, self.projects = sm, clock, bus, projects

    def _asked(self) -> list[tuple[RunRow, RunReport]]:
        with self.sm() as session:
            rows = list(
                session.scalars(
                    select(RunRow)
                    .where(RunRow.report_json.is_not(None))
                    .order_by(RunRow.id.desc())
                    .limit(500)
                )
            )
        out = []
        for row in rows:
            try:
                rep = RunReport.model_validate_json(row.report_json or "")
            except ValidationError:
                continue
            if rep.questions:
                out.append((row, rep))
        return out

    def _decisions(self, project_id: str) -> tuple[str, list[documents.Decision]] | None:
        try:
            snap = self.projects.snapshot(project_id)
        except Exception:
            return None
        name = snap.meta.name if snap.meta else project_id
        return name, snap.decisions

    def pending(self) -> list[PendingQuestion]:
        out: list[PendingQuestion] = []
        cache: dict[str, tuple[str, set[tuple[int, str]]] | None] = {}
        for row, rep in self._asked():
            if row.project_id not in cache:
                got = self._decisions(row.project_id)
                cache[row.project_id] = (got[0], answered_refs(got[1])) if got else None
            entry = cache[row.project_id]
            if entry is None:
                continue
            name, answered = entry
            for q in rep.questions:
                if (row.id, q.id) in answered:
                    continue
                out.append(
                    PendingQuestion(
                        project_id=row.project_id,
                        project_name=name,
                        run_id=row.id,
                        task_id=row.task_id,
                        task_title=row.task_title,
                        question_id=q.id,
                        text=q.text,
                        options=tuple(q.options),
                        recommended=q.recommended,
                        asked_at=row.ended_at or row.updated_at,
                    )
                )
        return out

    def by_task(self) -> dict[tuple[str, str], TaskQuestions]:
        """For task selection: does a task wait for an answer, and when was it last answered?"""
        pending = {(q.project_id, q.task_id) for q in self.pending() if q.task_id}
        answered: dict[tuple[str, str], datetime] = {}
        for project_id in {row.project_id for row, _ in self._asked()}:
            got = self._decisions(project_id)
            if got is None:
                continue
            for d in got[1]:
                m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC · (T-\d+) · r\d+ Q\d+", d.heading)
                if m:
                    when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                    key = (project_id, m.group(2))
                    answered[key] = max(answered.get(key, when), when)
        keys = pending | set(answered)
        return {
            k: TaskQuestions(pending=k in pending, answered_at=answered.get(k))
            for k in keys
            if k[1] is not None
        }

    def answer(self, project_id: str, run_id: int, question_id: str, answer: str | None) -> PendingQuestion:
        """Append the answer (None = skip) to decisions.md. InboxError if there is nothing to answer."""
        q = next(
            (
                p
                for p in self.pending()
                if p.project_id == project_id and p.run_id == run_id and p.question_id == question_id
            ),
            None,
        )
        if q is None:
            raise InboxError(f"no open question {question_id} from {run_name(run_id)} in {project_id}")
        text = " ".join((answer or "").split())
        if answer is not None and not text:
            raise InboxError("the answer is empty")
        if len(text) > 2000:
            raise InboxError("the answer is too long (2000 characters at most)")
        folder = self.projects.folder(project_id)
        files = repo.ProjectFiles(folder)
        for _ in range(3):  # the file may change between read and write (an editor, another answer)
            current, h = files.read(repo.DocName.DECISIONS)
            updated = documents.append_decision(
                current or documents.NEW_DECISIONS, self.clock.now(), q.text, text or SKIPPED, q.ref
            )
            try:
                self.projects.write_doc(project_id, repo.DocName.DECISIONS, updated, h)
                break
            except repo.ConflictError:
                continue
        else:
            raise InboxError("decisions.md kept changing; try again")
        log.info(
            "inbox.answered", project=project_id, run=run_id, question=question_id, skipped=answer is None
        )
        self.bus.publish(Event("inbox.changed", {"project_id": project_id}))
        return q
