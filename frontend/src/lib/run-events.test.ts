import { describe, expect, it } from "vitest";
import { mergeRunEvents, readEventFrames } from "@/lib/run-events";

const encode = new TextEncoder();
function stream(chunks: Uint8Array[]) {
  return new ReadableStream<Uint8Array>({ start(controller) {
    chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close();
  } });
}

describe("run event stream", () => {
  it("parses comments, CRLF, split UTF-8, multiline data and reconnect hints", async () => {
    const bytes = encode.encode(': heartbeat\r\n\r\nid: 2\r\nevent: analysis.completed\r\ndata: {"text":\r\ndata: "完成"}\r\n\r\nretry: 1000\n\nevent: end\ndata: {}\n\n');
    const frames = [];
    for await (const frame of readEventFrames(stream([...bytes].map((byte) => Uint8Array.of(byte))), new AbortController().signal)) frames.push(frame);
    expect(frames).toEqual([
      { event: "analysis.completed", id: "2", data: '{"text":\n"完成"}', retry: undefined },
      { event: "message", id: undefined, data: "", retry: 1000 },
      { event: "end", id: undefined, data: "{}", retry: undefined },
    ]);
  });

  it("does not commit a partial event at EOF", async () => {
    const frames = [];
    for await (const frame of readEventFrames(stream([encode.encode('id: 9\ndata: {"sequence":9}')]), new AbortController().signal)) frames.push(frame);
    expect(frames).toEqual([]);
  });

  it("bounds malformed frames rather than buffering without limit", async () => {
    const read = async () => {
      for await (const frame of readEventFrames(stream([encode.encode(`data: ${"x".repeat(1_048_577)}`)]), new AbortController().signal)) void frame;
    };
    await expect(read()).rejects.toThrow("大小限制");
  });

  it("cancels an outstanding read when its subscription is aborted", async () => {
    let cancelled = false;
    const controller = new AbortController();
    const frames = readEventFrames(new ReadableStream({ cancel() { cancelled = true; } }), controller.signal);
    const reading = frames.next();
    controller.abort();
    expect(await reading).toEqual({ value: undefined, done: true });
    expect(cancelled).toBe(true);
  });

  it("merges overlapping snapshot and streamed events once in sequence order", () => {
    const row = (sequence: number) => ({ sequence, type: "analysis.completed", payload: {}, created_at: "2026-11-05T00:00:00Z" });
    expect(mergeRunEvents([row(3), row(1)], row(3)).map((event) => event.sequence)).toEqual([1, 3]);
    expect(mergeRunEvents([row(3), row(1)], row(2)).map((event) => event.sequence)).toEqual([1, 2, 3]);
  });
});
