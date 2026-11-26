# 0018 — Inbox: decisions.md is the record of answers

- Status: accepted · Date: 2026-11-26

## Context
Agents ask questions in their reports (`questions`, with options and a recommendation). Phase 5 needs an
Inbox to answer them, and the DoD says the answer must influence the next run's prompt, visibly.

## Decision
1. **No answers table.** An answer is appended to the project's `decisions.md` (append-only, as before), with
   a heading that names the task, the run and the question: `## 2026-11-26 10:00 UTC · T-004 · r7 Q1`. A
   question counts as answered exactly when such an entry exists. Writing that entry by hand in the editor
   therefore works too, and the project folder stays the authority (plan §4.1).
2. **Open questions** come from the reports of the last 500 runs. Answering checks the question is still
   open and retries on concurrent edits of `decisions.md` (3 attempts, hash-checked).
3. **Skip** records "no answer: use your best judgment and say so in the report", so the question leaves the
   Inbox and the agent knows it may decide.
4. **The next prompt** already lists decisions that mention the task first. The run page now has a "Prompt"
   section that shows the exact prompt and rules a run was started with (`GET /api/runs/{id}/prompt`).
5. **Selection** (ADR 0016): a task with an open question is not started automatically. A task answered
   after its last run is preferred.

## Consequences
- The Inbox count in the navigation is the number of open questions across projects. `g i` opens it.
- Phone answers (Phase 6, Telegram) will use the same `Inbox.answer`.
