import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { useLiveRun } from "@/hooks/use-live-run";

const auth = vi.hoisted(() => ({ authenticated: true, getToken: vi.fn(async () => "test-token") }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => auth }));

const RUN_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_ID = "22222222-2222-4222-8222-222222222222";
const NOW = "2026-11-05T00:00:00Z";
const row = (sequence: number) => ({ sequence, type: "analysis.completed", payload: {}, created_at: NOW });
const run = (id: string) => ({ id, tenant_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", user_id: "alice",
  ticket_id: 42, goal: "Investigate", request_write: false, status: "running", created_at: NOW, updated_at: NOW,
  result: null, error: null, action_intent: null });

function setup() {
  const streams: { controller: ReadableStreamDefaultController<Uint8Array>; signal: AbortSignal; headers: Headers }[] = [];
  const fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/events")) {
      let controller!: ReadableStreamDefaultController<Uint8Array>;
      const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
      streams.push({ controller, signal: init!.signal as AbortSignal, headers: new Headers(init?.headers) });
      return new Response(body, { headers: { "content-type": "text/event-stream" } });
    }
    const id = url.includes(OTHER_ID) ? OTHER_ID : RUN_ID;
    return new Response(JSON.stringify(url.endsWith("/timeline") ? [row(1)] : run(id)), { headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: { children: React.ReactNode }) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
  const send = (index: number, sequence: number) => streams[index].controller.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: analysis.completed\ndata: ${JSON.stringify(row(sequence))}\n\n`));
  return { streams, fetch, wrapper, send };
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("live run subscription", () => {
  it("uses bearer auth and merges events without duplicates, then aborts on unmount", async () => {
    const fx = setup();
    const { result, unmount } = renderHook(() => useLiveRun(RUN_ID), { wrapper: fx.wrapper });
    await waitFor(() => expect(fx.streams).toHaveLength(1));
    expect(fx.streams[0].headers.get("Authorization")).toBe("Bearer test-token");
    await act(async () => { fx.send(0, 2); fx.send(0, 2); });
    await waitFor(() => expect(result.current.timeline.data?.map((event) => event.sequence)).toEqual([1, 2]));
    unmount();
    expect(fx.streams[0].signal.aborted).toBe(true);
  });

  it("reconnects after a normal session close using the last committed event ID", async () => {
    const fx = setup();
    const { result, unmount } = renderHook(() => useLiveRun(RUN_ID), { wrapper: fx.wrapper });
    await waitFor(() => expect(fx.streams).toHaveLength(1));
    await act(async () => { fx.send(0, 2); });
    await waitFor(() => expect(result.current.timeline.data).toHaveLength(2));
    await act(async () => { fx.streams[0].controller.enqueue(new TextEncoder().encode("retry: 250\n\n")); fx.streams[0].controller.close(); });
    await waitFor(() => expect(fx.streams).toHaveLength(2));
    expect(fx.streams[1].headers.get("Last-Event-ID")).toBe("2");
    unmount();
  });

  it("aborts the previous run subscription and resets the cursor when the run changes", async () => {
    const fx = setup();
    const { rerender, unmount } = renderHook(({ id }) => useLiveRun(id), { initialProps: { id: RUN_ID }, wrapper: fx.wrapper });
    await waitFor(() => expect(fx.streams).toHaveLength(1));
    await act(async () => { fx.send(0, 8); });
    rerender({ id: OTHER_ID });
    await waitFor(() => expect(fx.streams).toHaveLength(2));
    expect(fx.streams[0].signal.aborted).toBe(true);
    expect(fx.streams[1].headers.get("Last-Event-ID")).not.toBe("8");
    unmount();
  });

  it("refreshes authoritative snapshots when the server ends an execution phase", async () => {
    const fx = setup();
    const { unmount } = renderHook(() => useLiveRun(RUN_ID), { wrapper: fx.wrapper });
    await waitFor(() => expect(fx.streams).toHaveLength(1));
    await act(async () => { fx.streams[0].controller.enqueue(new TextEncoder().encode("event: end\ndata: {}\n\n")); });
    await waitFor(() => {
      const timelineCalls = fx.fetch.mock.calls.filter(([url]) => String(url).endsWith("/timeline"));
      const runCalls = fx.fetch.mock.calls.filter(([url]) => String(url).endsWith(RUN_ID));
      expect(timelineCalls).toHaveLength(2);
      expect(runCalls).toHaveLength(2);
    });
    expect(fx.streams).toHaveLength(1);
    unmount();
  });
});
