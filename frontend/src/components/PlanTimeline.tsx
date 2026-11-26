import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import { pct } from "../lib/format";
import { timelineRows, weeklySummary, type TimelineRow } from "../lib/plan";

const SERIES = [
  { key: "you", label: "You (P90 forecast)", color: "var(--series-manual)" },
  { key: "agents", label: "Agents (planned)", color: "var(--series-agent)" },
] as const;

const HARD_STOP = 85;
const timeFmt = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });

function WindowTooltip({ active, payload }: { active?: boolean; payload?: { payload?: TimelineRow }[] }) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return (
    <div className="card max-w-xs px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-medium">
        {timeFmt(row.start)} – {timeFmt(row.end)}
        {row.current && " (now)"}
      </div>
      {SERIES.map((s) => (
        <div key={s.key} className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color }} aria-hidden />
          <span className="muted">{s.label}</span>
          <span className="ml-auto tabular-nums">{pct(row[s.key], 1)}</span>
        </div>
      ))}
      <div className="mt-1 border-t pt-1 muted" style={{ borderColor: "var(--border)" }}>
        {row.reason}
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <div className="text-xs muted">{label}</div>
      <div className="text-lg font-medium tabular-nums">{value}</div>
      {hint && <div className="text-xs muted">{hint}</div>}
    </div>
  );
}

/** The plan until the weekly reset: per 5-hour window, the user's predicted usage and the agents' share. */
export function PlanTimeline() {
  const [table, setTable] = useState(false);
  const plan = useQuery({ queryKey: ["plan"], queryFn: api.plan, refetchInterval: 5 * 60_000 });
  const p = plan.data;
  const rows = useMemo(() => timelineRows(p?.windows ?? []), [p]);

  return (
    <section className="card p-4" aria-labelledby="plan-title">
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="plan-title" className="text-sm font-medium">
          Plan until the weekly reset
        </h2>
        <span className="text-xs muted">per 5-hour window, in % of that window</span>
        {rows.length > 1 && (
          <ul className="flex flex-wrap gap-3 text-xs" aria-label="Legend">
            {SERIES.map((s) => (
              <li key={s.key} className="flex items-center gap-1.5">
                <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
                {s.label}
              </li>
            ))}
            <li className="flex items-center gap-1.5 muted">
              <span className="inline-block w-3 border-t border-dashed" style={{ borderColor: "var(--text-secondary)" }} aria-hidden />
              Hard stop {HARD_STOP}%
            </li>
          </ul>
        )}
        {rows.length > 0 && (
          <button className="btn ml-auto" onClick={() => setTable((t) => !t)} aria-pressed={table}>
            {table ? "Chart" : "Table"}
          </button>
        )}
      </div>

      {plan.error ? (
        <p className="text-sm" role="alert">
          <span aria-hidden>▲</span> {plan.error.message}
        </p>
      ) : !p ? (
        <p className="py-8 text-center text-sm muted">Loading the plan…</p>
      ) : !p.available ? (
        <p className="py-8 text-center text-sm muted">No plan yet: {p.unavailable}.</p>
      ) : (
        <>
          <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="plan-stats">
            <Stat
              label="Allowed now"
              value={pct(p.allowed_now, 1)}
              hint={p.user_active ? "you are active" : "of the 5-hour window"}
            />
            <Stat label="Weekly surplus" value={pct(p.weekly?.surplus ?? 0, 1)} hint={weeklySummary(p)} />
            <Stat
              label="Confidence"
              value={p.confidence === null || p.confidence === undefined ? "–" : pct(p.confidence * 100)}
              hint={p.confidence_reasons[0]}
            />
            <Stat
              label="1 window costs"
              value={pct((p.weekly_per_five ?? 0) * 100)}
              hint={`of the week (${p.conversion_source ?? "prior"})`}
            />
          </div>
          {p.drift_minutes !== null && p.drift_minutes !== undefined && (
            <p className="mb-2 text-xs" role="status">
              <span aria-hidden style={{ color: "var(--status-warning)" }}>
                ▲
              </span>{" "}
              Claude moved the 5-hour reset by {Math.round(p.drift_minutes)} min; the plan follows it.
            </p>
          )}
          {table ? (
            <PlanTable rows={rows} />
          ) : (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -12 }} barCategoryGap={2}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis
                    dataKey="label"
                    tick={{ fill: "var(--text-secondary)", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                    minTickGap={16}
                  />
                  <YAxis
                    domain={[0, 100]}
                    ticks={[0, 25, 50, 75, 100]}
                    tickFormatter={(v: number) => `${v}%`}
                    tick={{ fill: "var(--text-secondary)", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <ReferenceLine y={HARD_STOP} stroke="var(--text-secondary)" strokeDasharray="4 4" />
                  <Tooltip content={<WindowTooltip />} cursor={{ fill: "var(--surface-2)" }} />
                  <Bar
                    dataKey="you"
                    stackId="w"
                    fill={SERIES[0].color}
                    stroke="var(--surface-1)"
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                  <Bar
                    dataKey="agents"
                    stackId="w"
                    fill={SERIES[1].color}
                    stroke="var(--surface-1)"
                    strokeWidth={2}
                    radius={[4, 4, 0, 0]}
                    isAnimationActive={false}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
          {p.weekly && p.weekly.reasons.length > 0 && (
            <details className="mt-2 text-xs muted">
              <summary className="cursor-pointer">Why these numbers</summary>
              <ul className="mt-1 list-disc pl-4">
                {[...p.weekly.reasons, p.conversion_reason ?? "", ...p.confidence_reasons]
                  .filter(Boolean)
                  .map((r) => (
                    <li key={r}>{r}</li>
                  ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}

function PlanTable({ rows }: { rows: TimelineRow[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="muted text-left text-xs">
        <tr>
          <th className="py-1 font-normal">Window</th>
          <th className="py-1 text-right font-normal">You (P90)</th>
          <th className="py-1 text-right font-normal">Agents</th>
          <th className="py-1 text-right font-normal">Room</th>
          <th className="py-1 pl-3 font-normal">Why</th>
        </tr>
      </thead>
      <tbody className="tabular-nums">
        {rows.map((r) => (
          <tr key={r.key} className="border-t align-top" style={{ borderColor: "var(--border)" }}>
            <td className="py-1">{r.current ? "now" : timeFmt(r.start)}</td>
            <td className="py-1 text-right">{pct(r.you, 1)}</td>
            <td className="py-1 text-right">{pct(r.agents, 1)}</td>
            <td className="py-1 text-right">{pct(r.capacity, 1)}</td>
            <td className="py-1 pl-3 muted">{r.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
