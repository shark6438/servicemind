"use client";

import { useEffect, useRef } from "react";
import { useApi } from "@/hooks/use-api";
import { ApiError, apiEventStream } from "@/lib/api";
import { runDetailSchema, runEventSchema, runTimelineSchema } from "@/lib/contracts";
import { mergeRunEvents, readEventFrames, waitForReconnect } from "@/lib/run-events";
import { useAuth } from "@/providers/auth-provider";

export function useLiveRun(runId: string) {
  const { authenticated, getToken } = useAuth();
  // SWR remains the snapshot authority and fallback when streaming is unavailable.
  const run = useApi(`/v1/servicemind/runs/${runId}`, runDetailSchema, { refreshInterval: 10000 });
  const timeline = useApi(`/v1/servicemind/runs/${runId}/timeline`, runTimelineSchema, { refreshInterval: 10000 });
  const cursor = useRef({ runId, sequence: 0 });
  const active = run.data?.status === "pending" || run.data?.status === "running";
  const refreshRun = run.mutate;
  const refreshTimeline = timeline.mutate;

  useEffect(() => {
    if (cursor.current.runId !== runId) cursor.current = { runId, sequence: 0 };
    for (const event of timeline.data ?? []) cursor.current.sequence = Math.max(cursor.current.sequence, event.sequence);
  }, [runId, timeline.data]);

  useEffect(() => {
    if (!authenticated || !active) return;
    const controller = new AbortController();
    const { signal } = controller;
    let retryMilliseconds = 1000;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const refreshSoon = () => {
      refreshTimer ??= setTimeout(() => {
        refreshTimer = undefined;
        if (!signal.aborted) void refreshRun().catch(() => undefined);
      }, 250);
    };

    async function follow() {
      while (!signal.aborted) {
        try {
          const response = await apiEventStream(`/v1/servicemind/runs/${runId}/events`, getToken, signal, cursor.current.sequence);
          for await (const frame of readEventFrames(response.body!, signal)) {
            if (signal.aborted) return;
            if (frame.retry !== undefined) retryMilliseconds = Math.max(250, Math.min(frame.retry, 10000));
            if (frame.event === "end") {
              await Promise.allSettled([refreshRun(), refreshTimeline()]);
              return;
            }
            if (!frame.data) continue;
            const event = runEventSchema.parse(JSON.parse(frame.data));
            if (frame.id !== String(event.sequence)) throw new Error("运行事件序号不一致");
            if (event.sequence <= cursor.current.sequence) continue;
            await refreshTimeline((current) => mergeRunEvents(current, event), { revalidate: false });
            if (signal.aborted || cursor.current.runId !== runId) return;
            cursor.current.sequence = Math.max(cursor.current.sequence, event.sequence);
            refreshSoon();
          }
        } catch (cause) {
          if (signal.aborted) return;
          // Authentication/permission failures are surfaced by the snapshot API.
          // Repeatedly reconnecting cannot repair them.
          if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) return;
          retryMilliseconds = Math.min(retryMilliseconds * 2, 10000);
        }
        await waitForReconnect(retryMilliseconds, signal);
      }
    }
    void follow();
    return () => { controller.abort(); clearTimeout(refreshTimer); };
  }, [runId, authenticated, active, getToken, refreshRun, refreshTimeline]);

  return { run, timeline };
}
