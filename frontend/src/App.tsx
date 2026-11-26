import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type RunLogEntry } from "./api/client";
import { Link, match, navigate, usePath, useShortcuts } from "./lib/router";
import { Dashboard } from "./pages/Dashboard";
import { NewProject } from "./pages/NewProject";
import { ProjectDetailPage } from "./pages/ProjectDetail";
import { Projects } from "./pages/Projects";
import { appendRunLog, RunDetailPage } from "./pages/RunDetail";

const EVENTS = [
  "usage.updated",
  "limits.updated",
  "reset.shifted",
  "source.error",
  "project.changed",
  "run.changed",
  "run.reported",
  "run.log",
  "dispatcher.updated",
  "project.paused",
];

/** Live updates: the API pushes events over SSE and each one refetches the queries it affects. */
function useLiveUpdates(): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    for (const type of EVENTS) {
      es.addEventListener(type, (ev) => {
        const data = JSON.parse((ev as MessageEvent).data) as Record<string, unknown>;
        if (type === "project.changed") {
          const id = data.id as string | undefined;
          void qc.invalidateQueries({ queryKey: ["projects"] });
          if (id) void qc.invalidateQueries({ queryKey: ["project", id] });
        } else if (type === "dispatcher.updated" || type === "project.paused") {
          void qc.invalidateQueries({ queryKey: ["control"] });
          void qc.invalidateQueries({ queryKey: ["projects"] });
          void qc.invalidateQueries({ queryKey: ["plan"] });
        } else if (type === "run.log") {
          appendRunLog(qc, Number(data.id), data.entry as RunLogEntry);
        } else if (type === "run.changed" || type === "run.reported") {
          void qc.invalidateQueries({ queryKey: ["runs"] });
          void qc.invalidateQueries({ queryKey: ["run", Number(data.id)] });
          if (type === "run.reported" && typeof data.project_id === "string") {
            void qc.invalidateQueries({ queryKey: ["project", data.project_id] });
          }
        } else {
          void qc.invalidateQueries({ queryKey: ["status"] });
          void qc.invalidateQueries({ queryKey: ["week"] });
          void qc.invalidateQueries({ queryKey: ["plan"] });
          void qc.invalidateQueries({ queryKey: ["control"] });
        }
      });
    }
    return () => es.close();
  }, [qc]);
  return connected;
}

function NavLink({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <Link
      href={href}
      className="rounded-md px-2.5 py-1 text-sm"
      aria-current={active ? "page" : undefined}
      style={active ? { background: "var(--surface-2)" } : { color: "var(--text-secondary)" }}
    >
      {label}
    </Link>
  );
}

export function App() {
  const path = usePath();
  const live = useLiveUpdates();
  // A cheap authenticated call decides between the app and the sign-in hint.
  const auth = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 60_000 });
  const shortcuts = useMemo(
    () => ({ "g d": () => navigate("/"), "g p": () => navigate("/projects"), "n p": () => navigate("/projects/new") }),
    [],
  );
  useShortcuts(shortcuts);

  if (auth.error instanceof ApiError && auth.error.status === 401) {
    return (
      <main className="mx-auto max-w-xl p-8">
        <h1 className="text-xl font-semibold">Surplus</h1>
        <p className="mt-2 muted">
          Not signed in. Open the link that <code>surplus serve</code> printed (it ends in <code>/auth?token=…</code>).
        </p>
      </main>
    );
  }

  let page: React.ReactNode;
  let params: Record<string, string> | null;
  if (path === "/") page = <Dashboard />;
  else if (match("/projects", path)) page = <Projects />;
  else if (match("/projects/new", path)) page = <NewProject />;
  else if ((params = match("/projects/:id/:tab?", path)))
    page = <ProjectDetailPage id={params.id!} tab={params.tab ?? "overview"} />;
  else if ((params = match("/runs/:id", path))) page = <RunDetailPage id={Number(params.id)} />;
  else
    page = (
      <p className="card p-4 text-sm">
        Page not found. <Link href="/">Go to the dashboard</Link>
      </p>
    );

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center gap-3">
        <Link href="/" className="text-xl font-semibold tracking-tight">
          Surplus
        </Link>
        <nav aria-label="Main" className="flex gap-1">
          <NavLink href="/" label="Dashboard" active={path === "/"} />
          <NavLink href="/projects" label="Projects" active={path.startsWith("/projects")} />
        </nav>
        <span className="ml-auto text-xs muted" role="status" title="Shortcuts: g d · g p · n p">
          <span aria-hidden style={{ color: live ? "var(--status-good)" : "var(--status-warning)" }}>
            ●
          </span>{" "}
          {live ? "live" : "reconnecting"}
        </span>
      </header>
      <main>{page}</main>
      <footer className="pb-4 text-xs muted">
        Surplus {auth.data?.version} · shortcuts: <kbd>g d</kbd> dashboard · <kbd>g p</kbd> projects · <kbd>n p</kbd>{" "}
        new project
      </footer>
    </div>
  );
}
