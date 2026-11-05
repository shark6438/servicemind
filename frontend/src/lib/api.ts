import type { ZodType } from "zod";
import { publicConfig } from "@/lib/config";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function parseError(response: Response): Promise<string> {
  const fallback = `请求失败（${response.status}）`;
  try {
    const payload = (await response.json()) as { detail?: unknown };
    return typeof payload.detail === "string" ? payload.detail : fallback;
  } catch { return fallback; }
}

export async function apiRequest<T>(path: string, getToken: () => Promise<string>, schema: ZodType<T>, init: RequestInit = {}): Promise<T> {
  const token = await getToken();
  const response = await fetch(`${publicConfig.apiUrl}${path}`, {
    ...init, cache: "no-store",
    headers: { Accept: "application/json", Authorization: `Bearer ${token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  return schema.parse(await response.json());
}

export async function apiEventStream(path: string, getToken: () => Promise<string>, signal: AbortSignal, lastEventId: number): Promise<Response> {
  const token = await getToken();
  signal.throwIfAborted();
  const response = await fetch(`${publicConfig.apiUrl}${path}`, {
    signal, cache: "no-store",
    headers: { Accept: "text/event-stream", Authorization: `Bearer ${token}`,
      ...(lastEventId > 0 ? { "Last-Event-ID": String(lastEventId) } : {}) },
  });
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new Error("运行事件流格式无效");
  }
  return response;
}

export async function publicRequest<T>(url: string, schema: ZodType<T>): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  return schema.parse(await response.json());
}
