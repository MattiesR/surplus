import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CreateProject } from "../api/client";
import { Link, navigate } from "../lib/router";

interface GoalDraft {
  title: string;
  acceptance: string; // one criterion per line
}

const STEPS = ["Name", "Description", "Goals", "Folder", "Create"] as const;

export function slugify(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 63)
      .replace(/-+$/g, "") || "project"
  );
}

/** New-project wizard (plan §11): name → description → goals → folder → checks. Writes the .surplus files. */
export function NewProject() {
  const qc = useQueryClient();
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [goals, setGoals] = useState<GoalDraft[]>([{ title: "", acceptance: "" }]);
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [path, setPath] = useState("");
  const [gitInit, setGitInit] = useState(true);
  const [testCommand, setTestCommand] = useState("");
  const system = useQuery({ queryKey: ["system"], queryFn: api.system });

  const assist = useMutation({
    mutationFn: () =>
      api.assist(
        name,
        description,
        goals.map((g) => g.title).filter((t) => t.trim()),
      ),
    onSuccess: (res) =>
      setGoals(res.goals.map((g) => ({ title: g.title, acceptance: (g.acceptance ?? []).join("\n") }))),
  });

  const create = useMutation({
    mutationFn: () => {
      const body: CreateProject = {
        name,
        description,
        goals: goals
          .filter((g) => g.title.trim())
          .map((g) => ({
            title: g.title.trim(),
            acceptance: g.acceptance
              .split("\n")
              .map((l) => l.replace(/^\s*[-*]\s*/, "").trim())
              .filter(Boolean),
          })),
        test_command: testCommand.trim() || null,
        priority: 2,
        model: "sonnet",
        constraints: [],
        mode,
        path: mode === "existing" ? path.trim() : null,
        git_init: gitInit,
      };
      return api.createProject(body);
    },
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${encodeURIComponent(p.id)}`);
    },
  });

  const canNext = [name.trim().length > 0, true, true, mode === "new" || path.trim().startsWith("/"), true][step];
  const slug = slugify(name);
  const updateGoal = (i: number, patch: Partial<GoalDraft>) =>
    setGoals((gs) => gs.map((g, j) => (j === i ? { ...g, ...patch } : g)));

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <p className="text-xs muted">
        <Link href="/projects">Projects</Link> /
      </p>
      <h1 className="text-lg font-semibold">New project</h1>
      <ol className="flex flex-wrap gap-2 text-xs" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li
            key={s}
            aria-current={i === step ? "step" : undefined}
            className="rounded px-2 py-0.5"
            style={{
              background: i === step ? "var(--surface-2)" : "transparent",
              color: i <= step ? "var(--text-primary)" : "var(--text-secondary)",
            }}
          >
            {i + 1}. {s}
          </li>
        ))}
      </ol>

      <form
        className="card space-y-3 p-4 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          if (step < STEPS.length - 1 && canNext) setStep(step + 1);
        }}
      >
        {step === 0 && (
          <label className="flex flex-col text-xs muted">
            Project name
            <input className="field mt-1 text-base" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
            {name && <span className="mt-1">id: {slug}</span>}
          </label>
        )}

        {step === 1 && (
          <label className="flex flex-col text-xs muted">
            One paragraph: what is it, and for whom?
            <textarea
              className="field mt-1 min-h-32"
              autoFocus
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
        )}

        {step === 2 && (
          <div className="space-y-3">
            <p className="text-xs muted">Goals in order of importance, each with acceptance criteria (one per line).</p>
            {goals.map((g, i) => (
              <fieldset key={i} className="space-y-1 rounded-md border p-3" style={{ borderColor: "var(--border)" }}>
                <legend className="px-1 text-xs muted">G{i + 1}</legend>
                <input
                  className="field w-full"
                  aria-label={`Goal ${i + 1} title`}
                  placeholder="Goal"
                  value={g.title}
                  onChange={(e) => updateGoal(i, { title: e.target.value })}
                />
                <textarea
                  className="field min-h-16 w-full"
                  aria-label={`Goal ${i + 1} acceptance criteria`}
                  placeholder="Acceptance criteria, one per line"
                  value={g.acceptance}
                  onChange={(e) => updateGoal(i, { acceptance: e.target.value })}
                />
                {goals.length > 1 && (
                  <button type="button" className="btn" onClick={() => setGoals((gs) => gs.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                )}
              </fieldset>
            ))}
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="btn" onClick={() => setGoals((gs) => [...gs, { title: "", acceptance: "" }])}>
                Add goal
              </button>
              <button type="button" className="btn" onClick={() => assist.mutate()} disabled={assist.isPending || !name.trim()}>
                {assist.isPending ? "Asking Claude…" : "Help me write acceptance criteria"}
              </button>
              <span className="text-xs muted">Uses one small Claude (Haiku) call from your subscription.</span>
            </div>
            {assist.error && (
              <p role="alert" className="text-xs">
                ▲ {assist.error.message}
              </p>
            )}
          </div>
        )}

        {step === 3 && (
          <div className="space-y-3">
            <label className="flex items-start gap-2">
              <input type="radio" name="mode" checked={mode === "new"} onChange={() => setMode("new")} />
              <span>
                New folder <code>{system.data ? `${system.data.projects_root}/${slug}` : slug}</code>
                <span className="block text-xs muted">Created with git init.</span>
              </span>
            </label>
            <label className="flex items-start gap-2">
              <input type="radio" name="mode" checked={mode === "existing"} onChange={() => setMode("existing")} />
              <span>Existing folder</span>
            </label>
            {mode === "existing" && (
              <div className="ml-6 space-y-2">
                <input
                  className="field w-full font-mono"
                  aria-label="Absolute path of the existing folder"
                  placeholder="/home/you/code/my-repo"
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                />
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={gitInit} onChange={(e) => setGitInit(e.target.checked)} />
                  Run git init if it is not a git repository yet
                </label>
                <p className="text-xs muted">
                  Agents get write access to this folder only (inside a container), and work on branches. Home, system and
                  configuration folders are refused.
                </p>
              </div>
            )}
            <label className="flex flex-col text-xs muted">
              Test command (optional, run after each agent run)
              <input
                className="field mt-1 font-mono"
                value={testCommand}
                onChange={(e) => setTestCommand(e.target.value)}
                placeholder="uv run pytest -q"
              />
            </label>
          </div>
        )}

        {step === 4 && (
          <div className="space-y-2">
            <p>
              <strong>{name}</strong> <span className="muted">({slug})</span>
            </p>
            <p className="text-xs muted">
              {goals.filter((g) => g.title.trim()).length} goal(s) · folder:{" "}
              {mode === "new" ? `${system.data?.projects_root ?? "…"}/${slug} (new)` : path}
            </p>
            <p className="text-xs">
              <span aria-hidden style={{ color: system.data?.runner_image ? "var(--status-good)" : "var(--status-warning)" }}>
                {system.data?.runner_image ? "●" : "▲"}
              </span>{" "}
              Container: {system.data?.detail ?? "checking…"}
            </p>
            {create.error && (
              <p role="alert">
                <span aria-hidden style={{ color: "var(--status-critical)" }}>
                  ▲
                </span>{" "}
                {create.error.message}
              </p>
            )}
          </div>
        )}

        <div className="flex gap-2 pt-2">
          <button type="button" className="btn" onClick={() => setStep(step - 1)} disabled={step === 0}>
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button type="submit" className="btn ml-auto" disabled={!canNext}>
              Next
            </button>
          ) : (
            <button type="button" className="btn ml-auto" onClick={() => create.mutate()} disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create project"}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
