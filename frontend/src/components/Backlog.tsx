import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, type FormEvent, useState } from "react";
import { api, ApiError, type ProjectDetail, type Task } from "../api/client";
import { RunNow } from "./Runs";

const SIZES = ["", "S", "M", "L"] as const;

/** Backlog tab: every action edits .surplus/backlog.md through the API, guarded by the file's hash. */
export function Backlog({ project }: { project: ProjectDetail }) {
  const qc = useQueryClient();
  const hash = project.docs.backlog!.hash;
  const tasks = project.backlog ?? [];
  const [dragId, setDragId] = useState<string | null>(null);
  const [runFor, setRunFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const done = () => {
    void qc.invalidateQueries({ queryKey: ["project", project.id] });
    void qc.invalidateQueries({ queryKey: ["projects"] });
  };
  const run = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => setError(null),
    onError: (e) =>
      setError(
        e instanceof ApiError && e.isConflict
          ? "backlog.md changed on disk; the list has been refreshed. Try again."
          : (e as Error).message,
      ),
    onSettled: done,
  });

  const move = (id: string, delta: number) => {
    const ids = tasks.map((t) => t.id);
    const i = ids.indexOf(id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j]!, ids[i]!];
    run.mutate(() => api.reorderTasks(project.id, ids, hash));
  };

  const dropOn = (targetId: string) => (e: DragEvent) => {
    e.preventDefault();
    if (!dragId || dragId === targetId) return;
    const ids = tasks.map((t) => t.id).filter((x) => x !== dragId);
    ids.splice(ids.indexOf(targetId), 0, dragId);
    setDragId(null);
    run.mutate(() => api.reorderTasks(project.id, ids, hash));
  };

  if (project.backlog === null) {
    const err = project.errors.find((e) => e.doc === "backlog");
    return (
      <p className="card p-4 text-sm" role="alert">
        backlog.md has an error{err?.line ? ` on line ${err.line}` : ""}: {err?.message}. Fix it in the “Edit as text” view.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <p className="text-sm" role="alert">
          <span aria-hidden style={{ color: "var(--status-critical)" }}>
            ▲
          </span>{" "}
          {error}
        </p>
      )}
      <ol className="card divide-y" style={{ borderColor: "var(--border)" }} aria-label="Backlog, in priority order">
        {tasks.length === 0 && <li className="p-4 text-sm muted">The backlog is empty. Add the first task below.</li>}
        {tasks.map((t, i) => (
          <TaskRow
            key={t.id}
            task={t}
            first={i === 0}
            last={i === tasks.length - 1}
            busy={run.isPending}
            dragging={dragId === t.id}
            onDragStart={() => setDragId(t.id)}
            onDrop={dropOn(t.id)}
            onMove={(d) => move(t.id, d)}
            runPanel={runFor === t.id ? <RunNow projectId={project.id} task={t} onClose={() => setRunFor(null)} /> : null}
            onRun={() => setRunFor(runFor === t.id ? null : t.id)}
            onToggle={() => run.mutate(() => api.updateTask(project.id, t.id, { base_hash: hash, done: !t.done }))}
            onAccept={() => run.mutate(() => api.updateTask(project.id, t.id, { base_hash: hash, proposed: false }))}
            onDelete={() => {
              if (confirm(`Delete ${t.id} “${t.title}” from backlog.md?`)) {
                run.mutate(() => api.deleteTask(project.id, t.id, hash));
              }
            }}
          />
        ))}
      </ol>
      <AddTask disabled={run.isPending} onAdd={(body) => run.mutate(() => api.addTask(project.id, { ...body, base_hash: hash }))} />
    </div>
  );
}

function TaskRow(props: {
  task: Task;
  first: boolean;
  last: boolean;
  busy: boolean;
  dragging: boolean;
  onDragStart: () => void;
  onDrop: (e: DragEvent) => void;
  onMove: (delta: number) => void;
  onRun: () => void;
  runPanel: React.ReactNode;
  onToggle: () => void;
  onAccept: () => void;
  onDelete: () => void;
}) {
  const { task: t } = props;
  const proposed = t.flags.includes("proposed");
  const runnable = !t.done && !proposed && !t.flags.includes("blocked");
  return (
    <li
      className="flex items-start gap-3 p-3"
      style={{ borderColor: "var(--border)", opacity: props.dragging ? 0.4 : 1 }}
      draggable
      onDragStart={props.onDragStart}
      onDragOver={(e) => e.preventDefault()}
      onDrop={props.onDrop}
    >
      <span className="cursor-grab select-none pt-0.5 muted" aria-hidden title="Drag to reorder">
        ⋮⋮
      </span>
      <input
        type="checkbox"
        className="mt-1"
        checked={t.done}
        disabled={props.busy}
        onChange={props.onToggle}
        aria-label={`Mark ${t.id} ${t.done ? "not done" : "done"}`}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-xs muted tabular-nums">{t.id}</span>
          <span className={t.done ? "line-through muted" : ""}>{t.title}</span>
          <span className="rounded px-1 text-xs" style={{ background: "var(--surface-2)" }}>
            P{t.priority}
          </span>
          {t.size && (
            <span className="rounded px-1 text-xs" style={{ background: "var(--surface-2)" }}>
              ~{t.size}
            </span>
          )}
          {t.flags.map((f) => (
            <span key={f} className="rounded border px-1 text-xs" style={{ borderColor: "var(--border)" }}>
              {f}
            </span>
          ))}
        </div>
        {t.acceptance && <p className="mt-0.5 text-xs muted">Acceptance: {t.acceptance}</p>}
        {t.notes.length > 0 && <pre className="mt-1 whitespace-pre-wrap text-xs muted">{t.notes.join("\n")}</pre>}
        {props.runPanel}
      </div>
      <div className="flex shrink-0 gap-1">
        {runnable && (
          <button
            className="btn"
            onClick={props.onRun}
            aria-expanded={props.runPanel != null}
            title="Start an agent run on this task"
          >
            Run
          </button>
        )}
        {proposed && (
          <button className="btn" onClick={props.onAccept} disabled={props.busy} title="Accept the agent's suggestion">
            Accept
          </button>
        )}
        <button className="btn" onClick={() => props.onMove(-1)} disabled={props.busy || props.first} aria-label={`Move ${t.id} up`}>
          ↑
        </button>
        <button className="btn" onClick={() => props.onMove(1)} disabled={props.busy || props.last} aria-label={`Move ${t.id} down`}>
          ↓
        </button>
        <button className="btn" onClick={props.onDelete} disabled={props.busy} aria-label={`Delete ${t.id}`}>
          ✕
        </button>
      </div>
    </li>
  );
}

function AddTask({
  onAdd,
  disabled,
}: {
  onAdd: (t: { title: string; priority: number; size: "S" | "M" | "L" | null; acceptance: string | null }) => void;
  disabled: boolean;
}) {
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState(2);
  const [size, setSize] = useState<(typeof SIZES)[number]>("");
  const [acceptance, setAcceptance] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    onAdd({ title, priority, size: size || null, acceptance: acceptance.trim() || null });
    setTitle("");
    setAcceptance("");
  };
  return (
    <form className="card flex flex-wrap items-end gap-2 p-3" onSubmit={submit} aria-label="Add a task">
      <label className="flex min-w-48 flex-1 flex-col text-xs muted">
        New task
        <input className="field mt-1" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="What should be built?" />
      </label>
      <label className="flex flex-col text-xs muted">
        Priority
        <select className="field mt-1" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
          {[0, 1, 2, 3].map((p) => (
            <option key={p} value={p}>
              P{p}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col text-xs muted">
        Size
        <select className="field mt-1" value={size} onChange={(e) => setSize(e.target.value as (typeof SIZES)[number])}>
          {SIZES.map((s) => (
            <option key={s} value={s}>
              {s ? `~${s}` : "—"}
            </option>
          ))}
        </select>
      </label>
      <label className="flex min-w-48 flex-1 flex-col text-xs muted">
        Acceptance (optional)
        <input className="field mt-1" value={acceptance} onChange={(e) => setAcceptance(e.target.value)} placeholder="How do we know it is done?" />
      </label>
      <button className="btn" type="submit" disabled={disabled || !title.trim()}>
        Add
      </button>
    </form>
  );
}
