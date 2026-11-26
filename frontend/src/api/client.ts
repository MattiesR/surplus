import type { components } from "./schema";

type S = components["schemas"];
export type Status = S["StatusOut"];
export type Bucket = S["BucketOut"];
export type SeriesPoint = S["SeriesPointOut"];
export type Source = S["SourceOut"];
export type ProjectCard = S["ProjectCard"];
export type ProjectDetail = S["ProjectDetail"];
export type Task = S["TaskOut"];
export type CreateProject = S["CreateProjectIn"];
export type GoalIn = S["GoalIn"];
export type SettingsPatch = S["SettingsIn"];
export type TaskPatch = S["TaskPatch"];
export type TaskIn = S["TaskIn"];
export type SystemInfo = S["SystemOut"];
export type Run = S["RunOut"];
export type RunDetail = S["RunDetailOut"];
export type RunLogEntry = S["LogEntryOut"];
export type RunReport = S["RunReport"];
export type Preflight = S["PreflightOut"];
export type RunIn = S["RunIn"];
export type Effort = NonNullable<RunIn["effort"]>;
export const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
export type Control = S["ControlOut"];
export type PlanOut = S["PlanOut"];
export type Preferences = S["PreferencesOut"];
export type InboxQuestion = S["QuestionOut"];
export type DocName = "project" | "goals" | "backlog" | "decisions";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public line: number | null = null,
    public currentHash: string | null = null,
    public problems: string[] = [],
  ) {
    super(message);
  }
  get isConflict(): boolean {
    return this.status === 409;
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)surplus_csrf=([^;]+)/);
  return match?.[1] ?? "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers = new Headers(init.headers);
  if (method !== "GET") headers.set("x-csrf-token", csrfToken());
  if (init.body) headers.set("content-type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    // FastAPI puts structured errors (e.g. run preflight problems) inside `detail`.
    const nested = body.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? body.detail : null;
    const problems: string[] = nested?.problems ?? body.problems ?? [];
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d: { msg?: string }) => d.msg).join("; ")
      : nested
        ? [String(nested.detail ?? "request failed"), ...problems].join(": ")
        : String(body.detail ?? res.statusText);
    throw new ApiError(res.status, detail, body.line ?? null, body.current_hash ?? null, problems);
  }
  return (await res.json()) as T;
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
const enc = encodeURIComponent;

export const api = {
  status: () => request<Status>("/api/status"),
  week: () => request<SeriesPoint[]>("/api/usage/week"),
  probe: () => request<{ readings: number }>("/api/probe", { method: "POST" }),
  manual: (bucket: string, used_pct: number) => request<Bucket>("/api/limits/manual", json("POST", { bucket, used_pct })),

  projects: () => request<ProjectCard[]>("/api/projects"),
  project: (id: string) => request<ProjectDetail>(`/api/projects/${enc(id)}`),
  createProject: (body: CreateProject) => request<ProjectDetail>("/api/projects", json("POST", body)),
  writeDoc: (id: string, doc: DocName, content: string, base_hash: string) =>
    request<{ hash: string }>(`/api/projects/${enc(id)}/docs/${doc}`, json("PUT", { content, base_hash })),
  updateSettings: (id: string, body: SettingsPatch) =>
    request<{ hash: string }>(`/api/projects/${enc(id)}/settings`, json("PATCH", body)),
  addTask: (id: string, body: TaskIn) => request<{ hash: string }>(`/api/projects/${enc(id)}/backlog`, json("POST", body)),
  updateTask: (id: string, taskId: string, body: TaskPatch) =>
    request<{ hash: string }>(`/api/projects/${enc(id)}/backlog/${enc(taskId)}`, json("PATCH", body)),
  deleteTask: (id: string, taskId: string, base_hash: string) =>
    request<{ hash: string }>(`/api/projects/${enc(id)}/backlog/${enc(taskId)}?base_hash=${enc(base_hash)}`, {
      method: "DELETE",
    }),
  reorderTasks: (id: string, ids: string[], base_hash: string) =>
    request<{ hash: string }>(`/api/projects/${enc(id)}/backlog/reorder`, json("POST", { ids, base_hash })),
  assist: (name: string, description: string, goals: string[]) =>
    request<{ goals: GoalIn[] }>("/api/assist/acceptance", json("POST", { name, description, goals })),
  system: () => request<SystemInfo>("/api/system"),

  runs: (projectId?: string) => request<Run[]>(`/api/runs${projectId ? `?project_id=${enc(projectId)}` : ""}`),
  run: (id: number) => request<RunDetail>(`/api/runs/${id}`),
  runLog: (id: number, after = 0) => request<RunLogEntry[]>(`/api/runs/${id}/log?after=${after}`),
  runDiff: (id: number) => request<{ text: string; truncated: boolean }>(`/api/runs/${id}/diff`),
  preflight: (body: RunIn) => request<Preflight>("/api/runs/preflight", json("POST", body)),
  startRun: (body: RunIn) => request<Run>("/api/runs", json("POST", body)),
  continueRun: (id: number, maxFiveHourPct: number) =>
    request<Run>(`/api/runs/${id}/continue`, json("POST", { max_five_hour_pct: maxFiveHourPct })),
  plan: () => request<PlanOut>("/api/plan"),
  preferences: () => request<Preferences>("/api/preferences"),
  setPreferences: (body: S["PreferencesIn"]) => request<Preferences>("/api/preferences", json("PUT", body)),
  inbox: () => request<InboxQuestion[]>("/api/inbox"),
  answer: (body: S["AnswerIn"]) => request<InboxQuestion[]>("/api/inbox/answer", json("POST", body)),
  runPrompt: (id: number) => request<{ text: string }>(`/api/runs/${id}/prompt`),
  control: () => request<Control>("/api/control"),
  pause: (duration: string | null, reason = "") =>
    request<Control>("/api/control/pause", json("POST", duration ? { duration, reason } : { reason })),
  resume: () => request<Control>("/api/control/resume", { method: "POST" }),
  setAuto: (enabled: boolean) => request<Control>("/api/control/auto", json("POST", { enabled })),
  runAction: (id: number, action: "stop" | "cancel" | "merge" | "discard") =>
    request<Run>(`/api/runs/${id}/${action}`, { method: "POST" }),
};
