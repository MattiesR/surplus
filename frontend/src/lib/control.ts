import type { components } from "../api/schema";

export type ControlState = components["schemas"]["ControlOut"];
export type Tone = "ok" | "warning" | "paused";

/** One headline for the control card: pause beats the kill switch beats the dispatcher's reason. */
export function controlHeadline(c: ControlState, now: Date = new Date()): { tone: Tone; text: string } {
  if (c.paused) {
    if (c.paused_indefinitely) return { tone: "paused", text: "Paused until you resume" };
    const until = c.paused_until ? new Date(c.paused_until) : null;
    const when = until
      ? until.toDateString() === now.toDateString()
        ? until.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : until.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })
      : "";
    return { tone: "paused", text: `Paused until ${when}`.trim() };
  }
  if (c.user_active) return { tone: "warning", text: "You are active: automatic runs hold back" };
  if (c.auto_start) return { tone: "ok", text: "Agents may use spare capacity now" };
  return { tone: "ok", text: "Not paused, and you are idle" };
}

export const PAUSE_CHOICES: { label: string; duration: string | null }[] = [
  { label: "Pause 1 h", duration: "1h" },
  { label: "Pause 4 h", duration: "4h" },
  { label: "Pause until tomorrow", duration: "16h" },
  { label: "Pause until I resume", duration: null },
];
