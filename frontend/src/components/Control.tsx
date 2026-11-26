import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { controlHeadline, PAUSE_CHOICES, type Tone } from "../lib/control";
import { since } from "../lib/format";

const TONE: Record<Tone, { icon: string; color: string }> = {
  ok: { icon: "●", color: "var(--status-good)" },
  warning: { icon: "▲", color: "var(--status-warning)" },
  paused: { icon: "❚❚", color: "var(--status-warning)" },
};

/** Global pause and the guard rails' state (plan §7.3). */
export function ControlCard({ now }: { now: Date }) {
  const qc = useQueryClient();
  const control = useQuery({ queryKey: ["control"], queryFn: api.control, refetchInterval: 30_000 });
  const [reason, setReason] = useState("");
  const onDone = () => {
    void qc.invalidateQueries({ queryKey: ["control"] });
    void qc.invalidateQueries({ queryKey: ["runs"] });
  };
  const pause = useMutation({
    mutationFn: (duration: string | null) => api.pause(duration, reason),
    onSuccess: (data) => {
      qc.setQueryData(["control"], data);
      setReason("");
    },
    onSettled: onDone,
  });
  const resume = useMutation({
    mutationFn: api.resume,
    onSuccess: (data) => qc.setQueryData(["control"], data),
    onSettled: onDone,
  });

  const c = control.data;
  const head = c ? controlHeadline(c, now) : null;
  const error = pause.error ?? resume.error ?? control.error;
  return (
    <section className="card p-4" aria-labelledby="control-title">
      <h2 id="control-title" className="mb-2 text-sm font-medium muted">
        Agent runs
      </h2>
      {head && c && (
        <div className="space-y-2 text-sm">
          <p className="font-medium" data-testid="control-headline">
            <span aria-hidden style={{ color: TONE[head.tone].color }}>
              {TONE[head.tone].icon}
            </span>{" "}
            {head.text}
          </p>
          {c.paused && c.pause_reason && <p className="muted">Reason: {c.pause_reason}</p>}
          {c.user_active && c.activity_reason && <p className="muted">{c.activity_reason}</p>}
          {!c.paused && !c.user_active && <p className="muted">{c.reason}</p>}
          {c.last_seen && !c.user_active && <p className="muted">You were last active {since(c.last_seen, now)}.</p>}
          {c.breaker_paused.length > 0 && (
            <p role="alert">
              <span aria-hidden style={{ color: "var(--status-warning)" }}>
                ▲
              </span>{" "}
              Paused after 3 failed runs: {c.breaker_paused.join(", ")}. Check the runs, then set the project active.
            </p>
          )}
          <p className="muted text-xs">Runs are started by hand until the dispatcher arrives (Phase 5).</p>
        </div>
      )}
      {c?.paused ? (
        <button className="btn mt-3" onClick={() => resume.mutate()} disabled={resume.isPending}>
          {resume.isPending ? "Resuming…" : "Resume"}
        </button>
      ) : (
        <div className="mt-3 space-y-2">
          <label className="block text-xs muted">
            Reason (optional)
            <input
              className="field mt-1 block w-full"
              value={reason}
              maxLength={200}
              onChange={(e) => setReason(e.target.value)}
              placeholder="deadline, demo, …"
            />
          </label>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Pause agent runs">
            {PAUSE_CHOICES.map((p) => (
              <button
                key={p.label}
                className="btn"
                disabled={pause.isPending}
                onClick={() => pause.mutate(p.duration)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
      )}
      {error && (
        <p className="mt-2 text-sm" role="alert">
          <span aria-hidden>▲</span> {error.message}
        </p>
      )}
    </section>
  );
}
