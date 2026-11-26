# ruff: noqa: F811 - the run fixtures (`env`) are imported from test_runner_service
"""The Inbox (ADR 0018): questions from run reports, answers into decisions.md, the next prompt, selection."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_runner_service import Env, env, git_identity, hello_agent, run_to_end  # noqa: F401 - fixtures

from surplus.api.app import create_app
from surplus.api.auth import load_or_create_token
from surplus.collector.service import Collector
from surplus.config import Paths, Settings
from surplus.dispatcher import Dispatcher
from surplus.inbox import SKIPPED, Inbox, InboxError, answered_refs
from surplus.projects import documents, repo


def inbox(e: Env) -> Inbox:
    return Inbox(e.sm, e.clock, e.bus, e.projects)


def test_answered_refs() -> None:
    ds = documents.parse_decisions(
        "# Decisions\n\n## 2026-11-26 10:00 UTC · T-004 · r7 Q1\n\n**Question:** x\n**Answer:** y\n"
        "\n## 2026-11-26 11:00 UTC · hand-written\n\nSomething.\n"
    )
    assert answered_refs(ds) == {(7, "Q1")}


async def test_question_answer_reaches_the_next_prompt(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    first = await run_to_end(env)
    ib = inbox(env)
    [q] = ib.pending()
    assert (q.run_id, q.question_id, q.task_id) == (first.id, "Q1", "T-001")
    assert q.options == ("A yes", "B no") and q.recommended == "B"
    assert ib.by_task()[("demo", "T-001")].pending

    ib.answer("demo", first.id, "Q1", "  B   no, English only ")
    assert ib.pending() == []
    text, _ = repo.ProjectFiles(env.folder).read(repo.DocName.DECISIONS)
    assert "· T-001 · r1 Q1" in text and "**Answer:** B no, English only" in text
    t = ib.by_task()[("demo", "T-001")]
    assert not t.pending and t.answered_at is not None
    with pytest.raises(InboxError, match="no open question"):
        ib.answer("demo", first.id, "Q1", "again")

    # The next run of the task sees the answer in its prompt.
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.update_task("demo", "T-001", h, done=False)
    env.runtime.agents.append(hello_agent())
    second = await run_to_end(env)
    prompt = (env.svc.run_dir(second.id) / "prompt.md").read_text()
    assert "Greet in Dutch too? → A: B no, English only" in prompt


async def test_skip_and_validation(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    ib = inbox(env)
    with pytest.raises(InboxError, match="empty"):
        ib.answer("demo", run.id, "Q1", "   ")
    ib.answer("demo", run.id, "Q1", None)
    text, _ = repo.ProjectFiles(env.folder).read(repo.DocName.DECISIONS)
    assert SKIPPED in text and ib.pending() == []


async def test_answering_by_hand_in_decisions_md_counts(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    files = repo.ProjectFiles(env.folder)
    text, h = files.read(repo.DocName.DECISIONS)
    env.projects.write_doc(
        "demo", repo.DocName.DECISIONS, text + "\n## 2026-11-26 12:00 UTC · T-001 · r1 Q1\n\nNo Dutch.\n", h
    )
    assert inbox(env).pending() == []
    assert run.id == 1


async def test_selection_waits_for_the_answer_then_prefers_the_task(env: Env, paths: Paths) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.update_task("demo", "T-001", h, done=False)
    col = Collector(env.sm, paths, Settings(), env.clock, env.bus)
    d = Dispatcher(env.sm, env.clock, env.bus, col, env.svc, env.projects, 1, None, inbox(env))
    [c] = [c for c in d.candidates() if c.task_id == "T-001"]
    assert c.question_pending and not c.answered
    env.clock.advance(timedelta(minutes=5))
    inbox(env).answer("demo", run.id, "Q1", "B")
    [c] = [c for c in d.candidates() if c.task_id == "T-001"]
    assert c.answered and not c.question_pending


async def test_inbox_and_prompt_api(env: Env, paths: Paths) -> None:
    env.runtime.agents.append(hello_agent())
    await run_to_end(env)
    app = create_app(paths, Settings(), env.clock, background=False)
    app.state.inbox = inbox(env)
    app.state.runs = env.svc
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {load_or_create_token(paths.api_token_file)}"}
        [q] = c.get("/api/inbox", headers=h).json()
        assert q["question_id"] == "Q1" and q["project_name"] == "Demo"
        bad = c.post("/api/inbox/answer", json={**q, "question_id": "Q9", "answer": "x"}, headers=h)
        assert bad.status_code == 422
        ok = c.post("/api/inbox/answer", json={**q, "answer": "B"}, headers=h)
        assert ok.status_code == 200 and ok.json() == []
        prompt = c.get("/api/runs/1/prompt", headers=h).json()["text"]
        assert "# Your task: T-001" in prompt
        assert c.get("/api/runs/99/prompt", headers=h).status_code == 404
