# 0016 — Task selection for automatic runs

- Status: accepted · Date: 2026-11-26

## Context
Plan §7.4: "answered questions that unblock a task first, then by project priority × goal order × task size
fit. A large task does not start in a small slot; the agent is asked to split it instead. The fair-share rule
per project is enforced over a 7-day rolling window." Several terms were open. Implementation:
`optimizer/selection.py` (pure).

## Decision
1. **Goal order.** Backlog tasks do not reference goals (the backlog format has no goal field), so a task's
   position in `backlog.md` stands in for goal order. The user sets it by dragging in the UI.
2. **Lexicographic order:** answered question first, then project priority (1 first), task priority (P0
   first), backlog position, and real work before split mode. Nothing is multiplied: a strict order is
   predictable and easy to explain in the decision log.
3. **Not eligible:** a task with a question waiting for the user, a task whose previous run waits for merge or
   discard (no pile-up of unreviewed branches), and anything done, proposed or blocked, or in a paused
   project.
4. **Size need**, in pp of the 5-hour window: S 5, M 15, L 30, unsized = M. An L task in a slot with room for
   at least an S is chosen in *split* mode: the agent proposes smaller tasks instead of starting it.
5. **Fair share is work-conserving.** A project above its `max_share_of_surplus` of the agents' 7-day spend
   is skipped only while another project has eligible work. A lone project may use the whole surplus, and if
   every project is above its cap, nobody is starved. Hypothesis checks that the favoured project stays within
   its cap plus one run.

## Consequences
- Every selection returns the skipped candidates with reasons, for the decision log and the UI.
