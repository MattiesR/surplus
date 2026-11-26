import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type InboxQuestion } from "../api/client";
import { since } from "../lib/format";
import { Link } from "../lib/router";

const key = (q: InboxQuestion) => `${q.project_id}/${q.run_id}/${q.question_id}`;

function QuestionCard({ q }: { q: InboxQuestion }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const answer = useMutation({
    mutationFn: (value: string | null) =>
      api.answer({ project_id: q.project_id, run_id: q.run_id, question_id: q.question_id, answer: value }),
    onSuccess: (data) => {
      qc.setQueryData(["inbox"], data);
      void qc.invalidateQueries({ queryKey: ["project", q.project_id] });
    },
  });
  const id = `q-${key(q).replace(/[^a-zA-Z0-9]/g, "-")}`;
  return (
    <li className="card space-y-2 p-4" aria-labelledby={id}>
      <div className="text-xs muted">
        {q.project_name} · {q.task_id ? `${q.task_id} ${q.task_title ?? ""}` : "no task"} ·{" "}
        <Link href={`/runs/${q.run_id}`}>run r{q.run_id}</Link> · {since(q.asked_at)}
      </div>
      <p id={id} className="font-medium">
        {q.question_id}. {q.text}
      </p>
      {q.options.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label={`Options for ${q.question_id}`}>
          {q.options.map((o) => (
            <button key={o} className="btn" disabled={answer.isPending} onClick={() => answer.mutate(o)}>
              {o}
              {q.recommended && o.startsWith(q.recommended) ? " (recommended)" : ""}
            </button>
          ))}
        </div>
      )}
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) answer.mutate(text.trim());
        }}
      >
        <label className="sr-only" htmlFor={`${id}-text`}>
          Your answer
        </label>
        <input
          id={`${id}-text`}
          className="field min-w-0 flex-1"
          placeholder={q.options.length > 0 ? "Or write your own answer" : "Your answer"}
          value={text}
          maxLength={2000}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn" type="submit" disabled={answer.isPending || !text.trim()}>
          Answer
        </button>
        <button className="btn" type="button" disabled={answer.isPending} onClick={() => answer.mutate(null)}>
          Skip
        </button>
      </form>
      {answer.error && (
        <p className="text-sm" role="alert">
          <span aria-hidden>▲</span> {answer.error.message}
        </p>
      )}
    </li>
  );
}

/** Questions agents asked, across all projects. Answers go into each project's decisions.md. */
export function InboxPage() {
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: api.inbox, refetchInterval: 60_000 });
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Inbox</h1>
        <p className="text-sm muted">
          Questions agents asked. Your answer is added to the project's decisions.md, and the next run of the task
          sees it in its prompt. Skip lets the agent decide.
        </p>
      </div>
      {inbox.error && (
        <p className="card p-3 text-sm" role="alert">
          <span aria-hidden>▲</span> {inbox.error.message}
        </p>
      )}
      {inbox.data && inbox.data.length === 0 && <p className="card p-4 text-sm muted">No open questions.</p>}
      <ul className="space-y-3">{inbox.data?.map((q) => <QuestionCard key={key(q)} q={q} />)}</ul>
    </div>
  );
}
