import { useQuery } from "@tanstack/react-query";
import { api, type ProjectCard } from "../api/client";
import { RunStateBadge } from "../components/Runs";
import { since } from "../lib/format";
import { Link } from "../lib/router";

const STATUS_STYLE: Record<string, { icon: string; color: string }> = {
  active: { icon: "●", color: "var(--status-good)" },
  paused: { icon: "Ⅱ", color: "var(--text-secondary)" },
  archived: { icon: "▪", color: "var(--text-secondary)" },
  error: { icon: "▲", color: "var(--status-warning)" },
};

function Card({ p }: { p: ProjectCard }) {
  const st = STATUS_STYLE[p.missing ? "error" : p.status] ?? STATUS_STYLE.error!;
  return (
    <li>
      <Link href={`/projects/${encodeURIComponent(p.id)}`} className="card block h-full p-4 hover:border-[var(--text-secondary)]">
        <div className="flex items-start gap-2">
          <h2 className="font-medium">{p.name}</h2>
          <span className="ml-auto rounded px-1.5 text-xs" style={{ background: "var(--surface-2)" }} title="Priority (1 = highest)">
            P{p.priority}
          </span>
        </div>
        <p className="mt-1 text-xs muted">
          <span aria-hidden style={{ color: st.color }}>
            {st.icon}
          </span>{" "}
          {p.missing ? "folder missing" : p.status} · {p.tasks_open} open · {p.tasks_done} done
          {p.tasks_proposed > 0 && ` · ${p.tasks_proposed} proposed`}
        </p>
        <p className="mt-1 truncate text-xs muted" title={p.path}>
          {p.path}
        </p>
        <p className="mt-2 flex flex-wrap items-center gap-1 text-xs muted">
          Last run:{" "}
          {p.last_run_id != null && p.last_run_state ? (
            <>
              r{p.last_run_id} · <RunStateBadge run={{ state: p.last_run_state, resolution: p.last_run_resolution ?? null }} />
              {p.last_run_at && <span>· {since(p.last_run_at)}</span>}
            </>
          ) : (
            "never"
          )}
        </p>
        {p.open_questions > 0 && (
          <span className="mt-2 inline-block rounded px-1.5 text-xs" style={{ background: "var(--surface-2)" }}>
            {p.open_questions} open question{p.open_questions === 1 ? "" : "s"}
          </span>
        )}
        {p.errors.length > 0 && (
          <p className="mt-2 text-xs" role="note">
            <span aria-hidden style={{ color: "var(--status-warning)" }}>
              ▲
            </span>{" "}
            {p.errors[0]!.doc}.md{p.errors[0]!.line ? ` line ${p.errors[0]!.line}` : ""}: {p.errors[0]!.message}
          </p>
        )}
      </Link>
    </li>
  );
}

export function Projects() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  return (
    <div className="space-y-4">
      <div className="flex items-center">
        <h1 className="text-lg font-semibold">Projects</h1>
        <Link href="/projects/new" className="btn ml-auto">
          New project
        </Link>
      </div>
      {projects.data && projects.data.length === 0 && (
        <div className="card p-6 text-sm">
          <p>No projects yet.</p>
          <p className="mt-1 muted">
            Create one with the wizard, or add a folder with a <code>.surplus/project.md</code> to your projects folder.
          </p>
        </div>
      )}
      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {projects.data?.map((p) => <Card key={p.id} p={p} />)}
      </ul>
    </div>
  );
}
