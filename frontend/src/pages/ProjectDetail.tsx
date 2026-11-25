import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { api, ApiError, type ProjectDetail } from "../api/client";
import { Backlog } from "../components/Backlog";
import { DocEditor } from "../components/DocEditor";
import { Markdown } from "../components/Markdown";
import { RunsList } from "../components/Runs";
import { Link } from "../lib/router";

const TABS = [
  ["overview", "Overview"],
  ["goals", "Goals"],
  ["backlog", "Backlog"],
  ["decisions", "Decisions"],
  ["runs", "Runs"],
  ["settings", "Settings"],
] as const;

export function ProjectDetailPage({ id, tab }: { id: string; tab: string }) {
  const project = useQuery({ queryKey: ["project", id], queryFn: () => api.project(id) });
  if (project.error) {
    const notFound = project.error instanceof ApiError && project.error.status === 404;
    return (
      <p className="card p-4 text-sm" role="alert">
        {notFound ? `No project “${id}”.` : project.error.message} <Link href="/projects">Back to projects</Link>
      </p>
    );
  }
  const p = project.data;
  if (!p) return <p className="text-sm muted">Loading…</p>;

  return (
    <div className="space-y-4">
      <div>
        <p className="text-xs muted">
          <Link href="/projects">Projects</Link> /
        </p>
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-lg font-semibold">{p.name}</h1>
          <span className="text-xs muted">
            {p.status} · priority {p.priority} · {p.model}
          </span>
          <span className="ml-auto truncate text-xs muted" title={p.path}>
            {p.path}
          </span>
        </div>
      </div>

      {p.errors.map((e) => (
        <p key={`${e.doc}-${e.message}`} className="card p-3 text-sm" role="alert">
          <span aria-hidden style={{ color: "var(--status-warning)" }}>
            ▲
          </span>{" "}
          <strong>{e.doc}.md</strong>
          {e.line ? ` line ${e.line}` : ""}: {e.message}
        </p>
      ))}

      <nav aria-label="Project sections" className="flex flex-wrap gap-1 border-b" style={{ borderColor: "var(--border)" }}>
        {TABS.map(([key, label]) => (
          <Link
            key={key}
            href={`/projects/${encodeURIComponent(p.id)}/${key}`}
            aria-current={tab === key ? "page" : undefined}
            className="-mb-px border-b-2 px-3 py-1.5 text-sm"
            style={{
              borderColor: tab === key ? "var(--series-manual)" : "transparent",
              color: tab === key ? "var(--text-primary)" : "var(--text-secondary)",
            }}
          >
            {label}
            {key === "backlog" && p.backlog && ` (${p.backlog.filter((t) => !t.done).length})`}
          </Link>
        ))}
      </nav>

      <TabBody p={p} tab={tab} />
    </div>
  );
}

function TabBody({ p, tab }: { p: ProjectDetail; tab: string }) {
  const [raw, setRaw] = useState(false);
  useEffect(() => setRaw(false), [tab]);
  const docs = p.docs;

  switch (tab) {
    case "goals":
      return (
        <section className="space-y-3" aria-label="Goals">
          <GoalsSummary p={p} />
          <DocEditor
            projectId={p.id}
            doc="goals"
            content={docs.goals!.content}
            hash={docs.goals!.hash}
            hint="Each goal: “## G1. Title”, then “Acceptance:” and a bullet list."
          />
        </section>
      );
    case "backlog":
      return (
        <section className="space-y-3" aria-label="Backlog">
          <div className="flex">
            <button className="btn ml-auto" onClick={() => setRaw((r) => !r)} aria-pressed={raw}>
              {raw ? "List view" : "Edit as text"}
            </button>
          </div>
          {raw ? (
            <DocEditor
              projectId={p.id}
              doc="backlog"
              content={docs.backlog!.content}
              hash={docs.backlog!.hash}
              hint="- [ ] T-001 (P1, ~S) Title — acceptance: …"
            />
          ) : (
            <Backlog project={p} />
          )}
        </section>
      );
    case "decisions":
      return (
        <section className="space-y-3" aria-label="Decisions">
          {p.decisions.length === 0 && (
            <p className="card p-4 text-sm muted">
              No decisions yet. When an agent asks a question and you answer it, the answer is added here (append-only).
            </p>
          )}
          <ol className="space-y-2">
            {p.decisions.map((d, i) => (
              <li key={i} className="card p-3 text-sm">
                <p className="text-xs muted">{d.heading}</p>
                {d.question ? (
                  <>
                    <p className="mt-1">
                      <span className="muted">Q:</span> {d.question}
                    </p>
                    <p>
                      <span className="muted">A:</span> {d.answer}
                    </p>
                  </>
                ) : (
                  <Markdown>{d.body}</Markdown>
                )}
              </li>
            ))}
          </ol>
        </section>
      );
    case "runs":
      return (
        <section aria-label="Runs">
          <RunsList projectId={p.id} />
        </section>
      );
    case "settings":
      return <SettingsForm key={p.docs.project!.hash} p={p} />;
    default:
      return (
        <section className="grid gap-4 md:grid-cols-3" aria-label="Overview">
          <div className="card p-4 md:col-span-2">
            <Markdown>{p.description_md}</Markdown>
            <details className="mt-4">
              <summary className="cursor-pointer text-xs muted">Edit project.md</summary>
              <div className="mt-2">
                <DocEditor projectId={p.id} doc="project" content={docs.project!.content} hash={docs.project!.hash} />
              </div>
            </details>
          </div>
          <div className="space-y-4">
            <GoalsSummary p={p} />
            <div className="card p-4 text-sm">
              <h2 className="mb-1 text-sm font-medium muted">Backlog</h2>
              <p>
                {p.backlog?.filter((t) => !t.done).length ?? 0} open · {p.backlog?.filter((t) => t.done).length ?? 0} done
              </p>
              <p className="mt-1 text-xs muted">Test command: {p.test_command ?? "none"}</p>
            </div>
          </div>
        </section>
      );
  }
}

function GoalsSummary({ p }: { p: ProjectDetail }) {
  return (
    <div className="card p-4 text-sm">
      <h2 className="mb-2 text-sm font-medium muted">Goals</h2>
      {p.goals.length === 0 && <p className="muted">No goals yet.</p>}
      <ol className="space-y-2">
        {p.goals.map((g) => (
          <li key={g.id}>
            <span className="text-xs muted">{g.id}</span> {g.title}
            {g.acceptance.length > 0 && (
              <ul className="ml-4 list-disc text-xs muted">
                {g.acceptance.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function SettingsForm({ p }: { p: ProjectDetail }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({
    name: p.name,
    status: p.status,
    priority: p.priority,
    model: p.model,
    share: Math.round(p.max_share_of_surplus * 100),
    test_command: p.test_command ?? "",
  });
  const save = useMutation({
    mutationFn: () =>
      api.updateSettings(p.id, {
        base_hash: p.docs.project!.hash,
        name: form.name,
        status: form.status as "active" | "paused" | "archived",
        priority: form.priority,
        model: form.model as "haiku" | "sonnet" | "opus",
        max_share_of_surplus: form.share / 100,
        test_command: form.test_command.trim() || null,
        clear_test_command: !form.test_command.trim(),
      }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["project", p.id] });
      void qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) => setForm((f) => ({ ...f, [k]: v }));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  const err = save.error instanceof ApiError ? save.error : null;

  return (
    <form className="card max-w-xl space-y-3 p-4 text-sm" onSubmit={submit} aria-label="Project settings">
      <label className="flex flex-col text-xs muted">
        Name
        <input className="field mt-1" value={form.name} onChange={(e) => set("name", e.target.value)} />
      </label>
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col text-xs muted">
          Status
          <select className="field mt-1" value={form.status} onChange={(e) => set("status", e.target.value)}>
            <option value="active">active</option>
            <option value="paused">paused</option>
            <option value="archived">archived</option>
          </select>
        </label>
        <label className="flex flex-col text-xs muted">
          Priority (1 = highest)
          <select className="field mt-1" value={form.priority} onChange={(e) => set("priority", Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs muted">
          Default model
          <select className="field mt-1" value={form.model} onChange={(e) => set("model", e.target.value)}>
            <option value="haiku">haiku</option>
            <option value="sonnet">sonnet</option>
            <option value="opus">opus (only when the budget allows)</option>
          </select>
        </label>
      </div>
      <label className="flex flex-col text-xs muted">
        Max share of surplus capacity: {form.share}%
        <input
          type="range"
          min={10}
          max={100}
          step={5}
          value={form.share}
          onChange={(e) => set("share", Number(e.target.value))}
          className="mt-1"
        />
      </label>
      <label className="flex flex-col text-xs muted">
        Test command (run after each agent run)
        <input
          className="field mt-1 font-mono"
          value={form.test_command}
          onChange={(e) => set("test_command", e.target.value)}
          placeholder="uv run pytest -q"
        />
      </label>
      <div className="flex items-center gap-2">
        <button className="btn" type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save settings"}
        </button>
        {save.isSuccess && <span className="text-xs muted">Saved to .surplus/project.md</span>}
      </div>
      {err && (
        <p role="alert">
          <span aria-hidden style={{ color: "var(--status-critical)" }}>
            ▲
          </span>{" "}
          {err.isConflict ? "project.md changed on disk; reload the page and try again." : err.message}
        </p>
      )}
    </form>
  );
}
