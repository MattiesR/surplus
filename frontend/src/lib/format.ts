/** "4h 12m", "2d 3h", "12m", "now" — compact countdown to a reset. */
export function countdown(target: Date, now: Date): string {
  const ms = target.getTime() - now.getTime();
  if (ms <= 0) return "now";
  const mins = Math.floor(ms / 60_000);
  const d = Math.floor(mins / 1440);
  const h = Math.floor((mins % 1440) / 60);
  const m = mins % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Local weekday + time, e.g. "Thu 01:30". */
export function localReset(target: Date, locale?: string): string {
  const day = target.toLocaleDateString(locale, { weekday: "short" });
  const time = target.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${day} ${time}`;
}

export function ago(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function pct(value: number, digits = 0): string {
  return `${value.toFixed(digits)}%`;
}

export const BUCKET_LABELS: Record<string, string> = {
  five_hour: "5-hour window",
  seven_day: "Weekly · all models",
  seven_day_opus: "Weekly · Opus",
  seven_day_sonnet: "Weekly · Sonnet",
};

export function bucketLabel(bucket: string): string {
  if (BUCKET_LABELS[bucket]) return BUCKET_LABELS[bucket];
  if (bucket.startsWith("weekly_model:")) return `Weekly · ${bucket.slice(13)}`;
  return bucket.replaceAll("_", " ");
}

export interface CumulativePoint {
  t: number;
  manual: number;
  invisible: number;
  agent: number;
}

/** Running totals, so the stacked area shows how the weekly limit filled up. */
export function cumulative(points: { start: string; manual: number; invisible: number; agent: number }[]): CumulativePoint[] {
  let m = 0;
  let i = 0;
  let a = 0;
  return points.map((p) => {
    m += p.manual;
    i += p.invisible;
    a += p.agent;
    return { t: new Date(p.start).getTime(), manual: m, invisible: i, agent: a };
  });
}

/** "5 min ago" for recent timestamps, a local date and time for older ones. */
export function since(iso: string, now: Date = new Date()): string {
  const seconds = (now.getTime() - new Date(iso).getTime()) / 1000;
  if (seconds < 0) return "just now";
  if (seconds < 86_400) return ago(seconds);
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
