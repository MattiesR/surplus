import { useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { SeriesPoint } from "../api/client";
import { cumulative, pct } from "../lib/format";

const SERIES = [
  { key: "manual", label: "You · Claude Code", color: "var(--series-manual)" },
  { key: "invisible", label: "You · chat & other apps", color: "var(--series-invisible)" },
  { key: "agent", label: "Surplus agents", color: "var(--series-agent)" },
] as const;

const dayFmt = (t: number) => new Date(t).toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
const timeFmt = (t: number) =>
  new Date(t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });

interface TooltipEntry {
  dataKey?: string | number;
  value?: number | string;
}

function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: TooltipEntry[]; label?: number }) {
  if (!active || !payload?.length || label === undefined) return null;
  const total = payload.reduce((s, p) => s + Number(p.value ?? 0), 0);
  return (
    <div className="card px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-medium">{timeFmt(label)}</div>
      {SERIES.map((s) => {
        const v = payload.find((p) => p.dataKey === s.key)?.value;
        return (
          <div key={s.key} className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color }} aria-hidden />
            <span className="muted">{s.label}</span>
            <span className="ml-auto tabular-nums">{pct(Number(v ?? 0), 1)}</span>
          </div>
        );
      })}
      <div className="mt-1 flex border-t pt-1" style={{ borderColor: "var(--border)" }}>
        <span className="muted">Total</span>
        <span className="ml-auto tabular-nums">{pct(total, 1)}</span>
      </div>
    </div>
  );
}

/** Stacked area: how this week's limit filled up, split by who used it (one % axis). */
export function UsageChart({ points, weekStart, weekEnd }: { points: SeriesPoint[]; weekStart?: number; weekEnd?: number }) {
  const [table, setTable] = useState(false);
  const data = useMemo(() => cumulative(points), [points]);
  const last = data.at(-1);
  // Only plot series that have usage, so an empty series never draws an outline on top of the stack.
  const plotted = SERIES.filter((s) => (last?.[s.key] ?? 0) > 0);

  return (
    <section className="card p-4" aria-labelledby="usage-title">
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="usage-title" className="text-sm font-medium">
          Weekly limit used this week <span className="muted">(cumulative)</span>
        </h2>
        <ul className="flex flex-wrap gap-3 text-xs" aria-label="Legend">
          {SERIES.map((s) => (
            <li key={s.key} className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
              {s.label}
              {last && <span className="muted tabular-nums">{pct(last[s.key], 1)}</span>}
            </li>
          ))}
        </ul>
        <button className="btn ml-auto" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Chart" : "Table"}
        </button>
      </div>

      {data.length === 0 ? (
        <p className="py-16 text-center text-sm muted">No weekly reading yet. Run a probe to start.</p>
      ) : table ? (
        <UsageTable data={data} />
      ) : (
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={[weekStart ?? "dataMin", weekEnd ?? "dataMax"]}
                tickFormatter={dayFmt}
                tick={{ fill: "var(--text-secondary)", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                minTickGap={24}
              />
              <YAxis
                tickFormatter={(v: number) => `${v}%`}
                tick={{ fill: "var(--text-secondary)", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickCount={6}
                domain={[0, (max: number) => Math.max(5, Math.ceil(max / 5) * 5)]}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--text-secondary)", strokeWidth: 1 }} />
              {plotted.map((s) => (
                <Area
                  key={s.key}
                  type="stepAfter"
                  dataKey={s.key}
                  stackId="usage"
                  stroke={s.color}
                  strokeWidth={2}
                  fill={s.color}
                  fillOpacity={0.35}
                  isAnimationActive={false}
                  activeDot={{ r: 4, stroke: "var(--surface-1)", strokeWidth: 2 }}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

function UsageTable({ data }: { data: ReturnType<typeof cumulative> }) {
  // One row per day: the cumulative value at the end of that day.
  const byDay = new Map<string, (typeof data)[number]>();
  for (const p of data) byDay.set(dayFmt(p.t), p);
  return (
    <table className="w-full text-sm">
      <thead className="muted text-left text-xs">
        <tr>
          <th className="py-1 font-normal">Day (end)</th>
          {SERIES.map((s) => (
            <th key={s.key} className="py-1 text-right font-normal">
              {s.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="tabular-nums">
        {[...byDay].map(([day, p]) => (
          <tr key={day} className="border-t" style={{ borderColor: "var(--border)" }}>
            <td className="py-1">{day}</td>
            {SERIES.map((s) => (
              <td key={s.key} className="py-1 text-right">
                {pct(p[s.key], 1)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
