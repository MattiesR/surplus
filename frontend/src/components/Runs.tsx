import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, EFFORTS, type Effort, type Run, type Task } from "../api/client";
import { since } from "../lib/format";
import { Link, navigate } from "../lib/router";

const IN_PROGRESS = new Set(["queued", "preparing", "running", "wrapping_up", "verifying", "reporting"]);

type Tone = "good" | "warning" | "critical" | "active" | "neutral";

export function runTone(run: Pick<Run, "state" | "resolution">): { tone: Tone; label: string } {
  if (IN_PROGRESS.has(run.state)) return { tone: "active", label: run.state.replace("_", " ") };
  switch (run.state) {
    case "awaiting_feedback":
      return { tone: "warning", label: "needs review" };
    case "done":
      return run.resolution === "merged" ? { tone: "good", label: "merged" } : { tone: "neutral", label: "discarded" };
    case "failed":
      return { tone: "critical", label: "failed" };
    case "budget_exhausted":
      return { tone: "warning", label: "no budget" };
    case "paused_by_user":
      return { tone: "neutral", label: "paused" };
    case "cancelled":
      return { tone: "neutral", label: "cancelled" };
    default:
      return { tone: "neutral", label: run.state };
  }
}

const TONE_STYLE: Record<Tone, { color: string; icon: string }> = {
  good: { color: "var(--status-good)", icon: "●" },
  warning: { color: "var(--status-warning)", icon: "▲" },
  critical: { color: "var(--status-critical)", icon: "■" },
  active: { color: "var(--focus)", icon: "◐" },
  neutral: { color: "var(--text-secondary)", icon: "○" },
};

/** State with an icon and a word: never colour alone. */
export function RunStateBadge({ run }: { run: Pick<Run, "state" | "resolution"> }) {
  const { tone, label } = runTone(run);
  const st = TONE_STYLE[tone];
  return (
    <span className="inline-flex items-center gap-1 text-xs whitespace-nowrap">
      <span aria-hidden style={{ color: st.color }}>
        {st.icon}
      </span>
      {label}
    </span>
  );
}

export function isActive(run: Pick<Run, "state">): boolean {
  return IN_PROGRESS.has(run.state);
}

/** Runs of one project, newest first. */
export function RunsList({ projectId }: { projectId: string }) {
  const runs = useQuery({ queryKey: ["runs", projectId], queryFn: () => api.runs(projectId) });
  if (runs.error) return <p role="alert">▲ {runs.error.message}</p>;
  if (!runs.data) return <p className="text-sm muted">Loading…</p>;
  if (runs.data.length === 0)
    return (
      <p className="card p-4 text-sm muted">
        No runs yet. Start one from the Backlog tab with “Run”, next to an open task.
      </p>
    );
  return (
    <table className="card w-full text-sm">
      <thead>
        <tr className="text-left text-xs muted">
          <th className="p-2 font-normal">Run</th>
          <th className="p-2 font-normal">Task</th>
          <th className="p-2 font-normal">State</th>
          <th className="p-2 font-normal">Tests</th>
          <th className="p-2 font-normal text-right">Commits</th>
          <th className="p-2 font-normal text-right">Spend</th>
          <th className="p-2 font-normal">Started</th>
        </tr>
      </thead>
      <tbody>
        {runs.data.map((r) => (
          <tr key={r.id} className="border-t" style={{ borderColor: "var(--border)" }}>
            <td className="p-2">
              <Link href={`/runs/${r.id}`} className="underline">
                {r.name}
              </Link>
            </td>
            <td className="p-2">
              <span className="text-xs muted">{r.task_id}</span> {r.task_title}
            </td>
            <td className="p-2">
              <RunStateBadge run={r} />
            </td>
            <td className="p-2 text-xs">{r.verified_tests ?? "—"}</td>
            <td className="p-2 text-right tabular-nums">{r.commits}</td>
            <td className="p-2 text-right tabular-nums">
              {r.spent_pct_est != null ? `${r.spent_pct_est.toFixed(1)} pp` : "—"}
            </td>
            <td className="p-2 text-xs muted">{r.started_at ? since(r.started_at) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const MODELS = ["haiku", "sonnet", "opus"] as const;

/** "Run now" for one task: preflight first, then an explicit start. Runs are manual only (Phase 3). */
export function RunNow({ projectId, task, onClose }: { projectId: string; task: Task; onClose: () => void }) {
  const [model, setModel] = useState<(typeof MODELS)[number] | null>(null);
  const [turns, setTurns] = useState<number | null>(null);
  const [effort, setEffort] = useState<Effort | null>(null);
  const pf = useQuery({
    queryKey: ["preflight", projectId, task.id, model, effort],
    queryFn: () => api.preflight({ project_id: projectId, task_id: task.id, model, effort }),
    staleTime: 0,
  });
  const start = useMutation({
    mutationFn: () =>
      api.startRun({
        project_id: projectId,
        task_id: task.id,
        model: model ?? undefined,
        max_turns: turns ?? undefined,
        effort: effort ?? undefined,
      }),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
  });
  const data = pf.data;
  const startErr = start.error instanceof ApiError ? start.error : null;

  return (
    <div className="mt-2 space-y-2 rounded-md border p-3 text-sm" style={{ borderColor: "var(--border)" }} role="group"
      aria-label={`Run ${task.id}`}>
      <p className="text-xs muted">
        The agent works in an isolated container on a new branch. Nothing is merged without you.
      </p>
      {pf.isLoading && <p className="text-xs muted">Checking limits, container and repository…</p>}
      {pf.error && <p role="alert">▲ {pf.error.message}</p>}
      {data && (
        <>
          {data.problems.length > 0 && (
            <ul className="space-y-1" role="alert" aria-label="Problems">
              {data.problems.map((p) => (
                <li key={p}>
                  <span aria-hidden style={{ color: "var(--status-critical)" }}>
                    ■
                  </span>{" "}
                  {p}
                </li>
              ))}
            </ul>
          )}
          {data.warnings.map((w) => (
            <p key={w} className="text-xs">
              <span aria-hidden style={{ color: "var(--status-warning)" }}>
                ▲
              </span>{" "}
              {w}
            </p>
          ))}
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col text-xs muted">
              Model
              <select
                className="field mt-1"
                value={model ?? data.model}
                onChange={(e) => setModel(e.target.value as (typeof MODELS)[number])}
              >
                {MODELS.map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col text-xs muted">
              Effort
              <select
                className="field mt-1"
                value={effort ?? data.effort ?? ""}
                onChange={(e) => setEffort((e.target.value || null) as Effort | null)}
              >
                <option value="">CLI default</option>
                {EFFORTS.map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col text-xs muted">
              Max turns
              <input
                className="field mt-1 w-20"
                type="number"
                min={1}
                max={200}
                value={turns ?? data.max_turns}
                onChange={(e) => setTurns(Number(e.target.value))}
              />
            </label>
            <p className="text-xs muted">
              {data.budget_pct != null
                ? `Uses your subscription. Hard stop at ${data.cap_five_hour_pct.toFixed(0)}% of the 5-hour window: ${data.budget_pct.toFixed(0)} pp left; you are asked before it goes further.`
                : "Uses your subscription."}
            </p>
          </div>
          {startErr && (
            <p role="alert">
              ▲ {startErr.problems.length ? startErr.problems.join("; ") : startErr.message}
            </p>
          )}
          <div className="flex gap-2">
            <button className="btn" onClick={() => start.mutate()} disabled={!data.ok || start.isPending}>
              {start.isPending ? "Starting…" : `Start run on ${task.id}`}
            </button>
            <button className="btn" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      )}
    </div>
  );
}
