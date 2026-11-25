import { describe, expect, it } from "vitest";
import { bucketLabel, countdown, cumulative, since } from "./format";

describe("countdown", () => {
  const now = new Date("2026-11-25T20:00:00Z");
  it("formats days, hours and minutes", () => {
    expect(countdown(new Date("2026-11-26T00:30:00Z"), now)).toBe("4h 30m");
    expect(countdown(new Date("2026-12-02T19:00:00Z"), now)).toBe("6d 23h");
    expect(countdown(new Date("2026-11-25T20:12:30Z"), now)).toBe("12m");
    expect(countdown(new Date("2026-11-25T19:00:00Z"), now)).toBe("now");
  });
});

describe("bucketLabel", () => {
  it("names known and model-scoped buckets", () => {
    expect(bucketLabel("five_hour")).toBe("5-hour window");
    expect(bucketLabel("weekly_model:fable")).toBe("Weekly · fable");
    expect(bucketLabel("brand_new")).toBe("brand new");
  });
});

describe("cumulative", () => {
  it("accumulates each series", () => {
    const out = cumulative([
      { start: "2026-11-25T19:00:00Z", manual: 1, invisible: 0, agent: 0 },
      { start: "2026-11-25T20:00:00Z", manual: 0.5, invisible: 2, agent: 1 },
    ]);
    expect(out[1]).toMatchObject({ manual: 1.5, invisible: 2, agent: 1 });
  });
});

describe("since", () => {
  const now = new Date("2026-11-25T20:00:00Z");
  it("is relative for the last day", () => {
    expect(since("2026-11-25T19:55:00Z", now)).toBe("5 min ago");
    expect(since("2026-11-25T20:00:30Z", now)).toBe("just now");
  });
  it("is a date for older timestamps", () => {
    expect(since("2026-11-20T10:00:00Z", now)).toMatch(/2026/);
  });
});
