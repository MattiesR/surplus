"""The Inbox: agent questions waiting for the user (plan §11), answered into decisions.md."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from surplus.inbox import Inbox, InboxError, PendingQuestion

router = APIRouter(prefix="/api", tags=["inbox"])


class QuestionOut(BaseModel):
    project_id: str
    project_name: str
    run_id: int
    task_id: str | None
    task_title: str | None
    question_id: str
    text: str
    options: list[str]
    recommended: str | None
    asked_at: datetime


class AnswerIn(BaseModel):
    project_id: str = Field(max_length=100)
    run_id: int
    question_id: str = Field(pattern=r"^Q\d{1,3}$")
    answer: str | None = Field(default=None, max_length=2000, description="null = skip: the agent decides")


def _out(q: PendingQuestion) -> QuestionOut:
    return QuestionOut(
        project_id=q.project_id,
        project_name=q.project_name,
        run_id=q.run_id,
        task_id=q.task_id,
        task_title=q.task_title,
        question_id=q.question_id,
        text=q.text,
        options=list(q.options),
        recommended=q.recommended,
        asked_at=q.asked_at,
    )


def _inbox(request: Request) -> Inbox:
    inbox: Inbox = request.app.state.inbox
    return inbox


@router.get("/inbox")
def list_inbox(request: Request) -> list[QuestionOut]:
    return [_out(q) for q in _inbox(request).pending()]


@router.post("/inbox/answer")
def answer(request: Request, body: AnswerIn) -> list[QuestionOut]:
    try:
        _inbox(request).answer(body.project_id, body.run_id, body.question_id, body.answer)
    except InboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    return [_out(q) for q in _inbox(request).pending()]
