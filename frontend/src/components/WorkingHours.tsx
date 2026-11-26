import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";

/** Typical working hours: the predictor's cold-start template (ADR 0015). */
export function WorkingHoursCard() {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["preferences"], queryFn: api.preferences });
  const [draft, setDraft] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (value: string | null) => api.setPreferences({ working_hours: value }),
    onSuccess: (data) => {
      qc.setQueryData(["preferences"], data);
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["plan"] });
    },
  });
  const current = prefs.data?.working_hours ?? "";
  const value = draft ?? current;
  return (
    <section className="card p-4" aria-labelledby="hours-title">
      <h2 id="hours-title" className="mb-2 text-sm font-medium muted">
        Your working hours
      </h2>
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(value.trim() || null);
        }}
      >
        <label className="block text-xs muted">
          When you usually use Claude ({prefs.data?.timezone ?? "local time"})
          <input
            className="field mt-1 block w-full"
            value={value}
            maxLength={200}
            placeholder="mon-fri 09:00-18:00"
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
        <div className="flex items-center gap-2">
          <button className="btn" type="submit" disabled={save.isPending || value === current}>
            Save
          </button>
          {current && (
            <button className="btn" type="button" disabled={save.isPending} onClick={() => save.mutate(null)}>
              Remove
            </button>
          )}
        </div>
      </form>
      <p className="mt-2 text-xs muted">
        {prefs.data?.working_hours_text
          ? `Template: ${prefs.data.working_hours_text}. `
          : "No template: until Surplus has learned your pattern, it assumes you may work at any time. "}
        Used only for the first two weeks; your real usage always wins.
      </p>
      {save.error && (
        <p className="mt-2 text-sm" role="alert">
          <span aria-hidden>▲</span> {save.error.message}
        </p>
      )}
    </section>
  );
}
