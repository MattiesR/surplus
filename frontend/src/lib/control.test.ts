import { describe, expect, it } from "vitest";
import { controlHeadline, type ControlState } from "./control";

const base: ControlState = {
  paused: false,
  paused_until: null,
  paused_indefinitely: false,
  pause_reason: "",
  user_active: false,
  activity_reason: null,
  last_seen: null,
  auto_start: false,
  reason: "no fresh reset times",
  checked_at: "2026-11-26T10:00:00Z",
  breaker_paused: [],
  auto_enabled: true,
};

describe("controlHeadline", () => {
  const now = new Date("2026-11-26T10:00:00Z");
  it("puts a pause first", () => {
    expect(controlHeadline({ ...base, paused: true, paused_indefinitely: true, user_active: true }, now)).toEqual({
      tone: "paused",
      text: "Paused until you resume",
    });
    const timed = controlHeadline({ ...base, paused: true, paused_until: "2026-11-26T11:00:00Z" }, now);
    expect(timed.tone).toBe("paused");
    expect(timed.text).toMatch(/^Paused until \S/);
  });
  it("says when automatic runs are off", () => {
    expect(controlHeadline({ ...base, auto_enabled: false }, now).text).toBe("Automatic runs are off");
  });
  it("shows the kill switch when the user is active", () => {
    expect(controlHeadline({ ...base, user_active: true }, now).tone).toBe("warning");
  });
  it("is calm otherwise, and only promises agent work when a start is allowed", () => {
    expect(controlHeadline(base, now)).toEqual({ tone: "ok", text: "Not paused, and you are idle" });
    expect(controlHeadline({ ...base, auto_start: true }, now).text).toContain("may use spare capacity");
  });
});
