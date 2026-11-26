import type { components } from "../api/schema";

export type PlanState = components["schemas"]["PlanOut"];
export type PlanWindow = components["schemas"]["WindowOut"];

export interface TimelineRow {
  key: string;
  label: string; // the window's start, local time
  you: number; // pp of the 5-hour window predicted for the user (P90)
  agents: number; // pp planned for agents
  capacity: number;
  current: boolean;
  start: string;
  end: string;
  reason: string;
}

const round1 = (v: number) => Math.round(v * 10) / 10;

export function windowLabel(start: Date, current: boolean): string {
  if (current) return "now";
  return start.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** One bar per 5-hour window until the weekly reset. */
export function timelineRows(windows: PlanWindow[]): TimelineRow[] {
  return windows.map((w) => ({
    key: w.start,
    label: windowLabel(new Date(w.start), w.current),
    you: round1(w.manual_p90),
    agents: round1(w.allocation),
    capacity: round1(w.capacity),
    current: w.current,
    start: w.start,
    end: w.end,
    reason: w.reason,
  }));
}

/** The planned agent share of the week, as text for the stat row. */
export function weeklySummary(p: PlanState): string {
  if (!p.weekly) return "";
  const planned = round1(p.weekly.planned);
  const surplus = round1(p.weekly.surplus);
  return planned >= surplus - 0.05
    ? `${surplus}% of the week planned for agents`
    : `${planned}% of ${surplus}% planned (the windows are full)`;
}
