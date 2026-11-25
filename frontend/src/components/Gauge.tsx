import type { Bucket } from "../api/client";
import { ago, bucketLabel, countdown, localReset, pct } from "../lib/format";

const R = 52;
const SWEEP = 270; // degrees of arc
const CIRC = 2 * Math.PI * R;
const ARC = (CIRC * SWEEP) / 360;

/** Radial meter for one limit bucket, with its reset countdown. Reset times come from Claude only. */
export function Gauge({ bucket, now }: { bucket: Bucket; now: Date }) {
  const value = Math.max(0, Math.min(100, bucket.used_pct));
  const reset = bucket.next_reset_at ? new Date(bucket.next_reset_at) : null;
  const high = value >= 90;
  return (
    <section className="card flex flex-col items-center p-4" aria-label={`${bucketLabel(bucket.bucket)} usage`}>
      <h2 className="self-start text-sm font-medium muted">{bucketLabel(bucket.bucket)}</h2>
      <svg viewBox="0 0 128 128" className="h-40 w-40" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value} aria-valuetext={`${pct(value)} used`}>
        <g transform="rotate(135 64 64)">
          <circle cx="64" cy="64" r={R} fill="none" stroke="var(--track)" strokeWidth="10" strokeLinecap="round" strokeDasharray={`${ARC} ${CIRC}`} />
          <circle
            cx="64"
            cy="64"
            r={R}
            fill="none"
            stroke={high ? "var(--status-critical)" : "var(--series-manual)"}
            strokeWidth="10"
            strokeLinecap="round"
            strokeDasharray={`${(ARC * value) / 100} ${CIRC}`}
          />
        </g>
        <text x="64" y="66" textAnchor="middle" fontSize="26" fontWeight="600" fill="var(--text-primary)">
          {pct(value)}
        </text>
        <text x="64" y="86" textAnchor="middle" fontSize="10" fill="var(--text-secondary)">
          used
        </text>
      </svg>
      {high && (
        <p className="text-sm" role="status">
          <span aria-hidden>⚠</span> Near the limit
        </p>
      )}
      <p className="text-sm">
        {reset ? (
          <>
            Resets in <strong>{countdown(reset, now)}</strong> <span className="muted">· {localReset(reset)}</span>
          </>
        ) : (
          <span className="muted">Reset time not reported yet</span>
        )}
      </p>
      <p className="mt-1 text-xs muted">
        {bucket.source} · {ago(bucket.age_seconds)}
      </p>
    </section>
  );
}
