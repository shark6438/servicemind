import type { z } from "zod";
import type { runEventSchema } from "@/lib/contracts";

export type RunEvent = z.infer<typeof runEventSchema>;
export type EventFrame = { event: string; id?: string; data: string; retry?: number };
const MAX_FRAME_CHARACTERS = 1_048_576;

/** Parse SSE incrementally, including split UTF-8, CRLF, comments and multiline data. */
export async function* readEventFrames(stream: ReadableStream<Uint8Array>, signal: AbortSignal): AsyncGenerator<EventFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let id: string | undefined;
  let retry: number | undefined;
  let data: string[] = [];
  let frameSize = 0;
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let newline: number;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        frameSize += line.length;
        if (frameSize > MAX_FRAME_CHARACTERS) throw new Error("运行事件超过大小限制");
        if (!line) {
          if (data.length || retry !== undefined) yield { event, id, data: data.join("\n"), retry };
          event = "message"; id = undefined; retry = undefined; data = []; frameSize = 0;
          continue;
        }
        if (line.startsWith(":")) continue;
        const separator = line.indexOf(":");
        const field = separator < 0 ? line : line.slice(0, separator);
        const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
        if (field === "event") event = value;
        else if (field === "id" && !value.includes("\0")) id = value;
        else if (field === "data") data.push(value);
        else if (field === "retry" && /^\d+$/.test(value)) retry = Number(value);
      }
      if (buffer.length + frameSize > MAX_FRAME_CHARACTERS) throw new Error("运行事件超过大小限制");
      // An incomplete frame at EOF was never committed as an SSE message. Its ID
      // must not advance the reconnect cursor.
      if (chunk.done) return;
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

/** Snapshot polling and streaming may overlap; sequence is the durable identity. */
export function mergeRunEvents(current: RunEvent[] | undefined, incoming: RunEvent): RunEvent[] {
  const events = new Map((current ?? []).map((event) => [event.sequence, event]));
  events.set(incoming.sequence, incoming);
  return [...events.values()].sort((left, right) => left.sequence - right.sequence);
}

export function waitForReconnect(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, milliseconds);
    signal.addEventListener("abort", done, { once: true });
  });
}
