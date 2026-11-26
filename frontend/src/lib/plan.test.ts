import { describe, expect, it } from "vitest";
import { timelineRows, weeklySummary, type PlanState, type PlanWindow } from "./plan";

const w = (start: string, current: boolean, manual: number, alloc: number): PlanWindow => ({
  start,
  end: start,
  current,
  manual_p90: manual,
  capacity: 60,
  allocation: alloc,
  reason: "r",
});

describe("timelineRows", () => {
  it("labels the current window 'now' and rounds", () => {
    const rows = timelineRows([w("2026-11-26T10:00:00Z", true, 12.345, 4.04), w("2026-11-26T13:00:00Z", false, 0, 9)]);
    expect(rows[0]).toMatchObject({ label: "now", you: 12.3, agents: 4, current: true });
    expect(rows[1]?.label).not.toBe("now");
  });
});

describe("weeklySummary", () => {
  const base = { available: true, unavailable: null } as PlanState;
  it("says when the whole surplus is planned", () => {
    const p = { ...base, weekly: { free: 70, manual_future: 10, reserve: 15, surplus: 45, planned: 45, reasons: [] } };
    expect(weeklySummary(p)).toBe("45% of the week planned for agents");
  });
  it("says when the windows cannot take it all", () => {
    const p = { ...base, weekly: { free: 70, manual_future: 10, reserve: 15, surplus: 45, planned: 20, reasons: [] } };
    expect(weeklySummary(p)).toContain("20% of 45%");
  });
});
