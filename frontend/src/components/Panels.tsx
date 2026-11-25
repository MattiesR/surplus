import { useState } from "react";
import type { Source, Status } from "../api/client";
import { pct } from "../lib/format";

/** Hero numbers: who used this week's limit. */
export function SplitCard({ split }: { split: Status["week_split"] }) {
  const items = [
    { label: "You · Claude Code", value: split?.manual, color: "var(--series-manual)" },
    { label: "You · chat & other apps", value: split?.invisible, color: "var(--series-invisible)" },
    { label: "Surplus agents", value: split?.agent, color: "var(--series-agent)" },
  ];
  return (
    <section className="card p-4" aria-labelledby="split-title">
      <h2 id="split-title" className="mb-3 text-sm font-medium muted">
        This week, by who used it
      </h2>
      {split ? (
        <dl className="grid grid-cols-3 gap-3">
          {items.map((i) => (
            <div key={i.label}>
              <dt className="flex items-center gap-1.5 text-xs muted">
                <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} aria-hidden />
                {i.label}
              </dt>
              <dd className="text-2xl font-semibold tabular-nums">{pct(i.value ?? 0, 1)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-sm muted">Needs a probe reading with the weekly breakdown.</p>
      )}
    </section>
  );
}

export function SourcesCard({ sources }: { sources: Source[] }) {
  return (
    <section className="card p-4" aria-labelledby="sources-title">
      <h2 id="sources-title" className="mb-2 text-sm font-medium muted">
        Sources
      </h2>
      <ul className="space-y-1 text-sm">
        {sources.length === 0 && <li className="muted">No source has reported yet.</li>}
        {sources.map((s) => (
          <li key={s.name} className="flex items-start gap-2">
            <span aria-hidden style={{ color: s.ok ? "var(--status-good)" : "var(--status-warning)" }}>
              {s.ok ? "●" : "▲"}
            </span>
            <span className="font-medium">{s.name}</span>
            <span className="muted">{s.ok ? "ok" : "stale or failing"}</span>
            {!s.ok && s.last_error && (
              <span className="ml-auto max-w-[60%] truncate text-xs muted" title={s.last_error}>
                {s.last_error}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Manual calibration (fallback source D). Only a percentage: reset times are never typed in. */
export function CalibrateCard({ onSubmit, busy }: { onSubmit: (bucket: string, value: number) => void; busy: boolean }) {
  const [bucket, setBucket] = useState("seven_day");
  const [value, setValue] = useState("");
  const n = Number(value);
  const valid = value !== "" && n >= 0 && n <= 100;
  return (
    <section className="card p-4" aria-labelledby="cal-title">
      <h2 id="cal-title" className="mb-2 text-sm font-medium muted">
        Manual calibration
      </h2>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) {
            onSubmit(bucket, n);
            setValue("");
          }
        }}
      >
        <label className="flex flex-col text-xs muted">
          Bucket
          <select className="field mt-1" value={bucket} onChange={(e) => setBucket(e.target.value)}>
            <option value="five_hour">5-hour window</option>
            <option value="seven_day">Weekly · all models</option>
          </select>
        </label>
        <label className="flex flex-col text-xs muted">
          % used
          <input
            className="field mt-1 w-20"
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={value !== "" && !valid}
          />
        </label>
        <button className="btn" type="submit" disabled={!valid || busy}>
          Save
        </button>
      </form>
    </section>
  );
}
