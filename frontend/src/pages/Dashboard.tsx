import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { ControlCard } from "../components/Control";
import { Gauge } from "../components/Gauge";
import { PlanTimeline } from "../components/PlanTimeline";
import { CalibrateCard, SourcesCard, SplitCard } from "../components/Panels";
import { UsageChart } from "../components/UsageChart";

function useNow(intervalMs = 30_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function Dashboard() {
  const qc = useQueryClient();
  const now = useNow();
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 60_000 });
  const week = useQuery({ queryKey: ["week"], queryFn: api.week, refetchInterval: 5 * 60_000 });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["status"] });
    void qc.invalidateQueries({ queryKey: ["week"] });
    void qc.invalidateQueries({ queryKey: ["plan"] });
    void qc.invalidateQueries({ queryKey: ["control"] });
  };
  const probe = useMutation({ mutationFn: api.probe, onSettled: refresh });
  const manual = useMutation({
    mutationFn: ({ bucket, value }: { bucket: string; value: number }) => api.manual(bucket, value),
    onSettled: refresh,
  });

  const s = status.data;
  const weekly = s?.buckets.find((b) => b.bucket === "seven_day");
  const weekEnd = weekly?.next_reset_at ? new Date(weekly.next_reset_at).getTime() : undefined;
  const weekStart = weekEnd !== undefined ? weekEnd - 7 * 24 * 3600 * 1000 : undefined;

  return (
    <div className="space-y-4">
      <div className="flex items-center">
        <h1 className="sr-only">Dashboard</h1>
        <button className="btn ml-auto" onClick={() => probe.mutate()} disabled={probe.isPending}>
          {probe.isPending ? "Reading limits…" : "Read limits now"}
        </button>
      </div>

      {probe.error && (
        <p className="card p-3 text-sm" role="alert">
          <span aria-hidden>▲</span> Probe failed: {probe.error.message}
        </p>
      )}
      {s && !s.resets_fresh && (
        <p className="card p-3 text-sm" role="alert">
          <span aria-hidden style={{ color: "var(--status-warning)" }}>
            ▲
          </span>{" "}
          No fresh reset time from Claude in the last hour. Agent runs stay paused until one arrives.
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {s?.buckets.map((b) => <Gauge key={b.bucket} bucket={b} now={now} />)}
        {s && s.buckets.length === 0 && (
          <p className="card p-4 text-sm muted sm:col-span-2">No limit readings yet. Click “Read limits now”.</p>
        )}
      </div>

      <UsageChart points={week.data ?? []} weekStart={weekStart} weekEnd={weekEnd} />

      <PlanTimeline />

      <div className="grid gap-4 md:grid-cols-3">
        <div className="md:col-span-2">
          <SplitCard split={s?.week_split ?? null} />
        </div>
        <ControlCard now={now} />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <SourcesCard sources={s?.sources ?? []} />
        <CalibrateCard busy={manual.isPending} onSubmit={(bucket, value) => manual.mutate({ bucket, value })} />
      </div>
    </div>
  );
}
