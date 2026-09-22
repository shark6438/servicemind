"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { apiRequest } from "@/lib/api";
import { auditPageSchema, type AuditEvent } from "@/lib/contracts";
import { formatDate, readableLabel, shortId } from "@/lib/format";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/providers/auth-provider";

function AuditLedger() {
  const search = useSearchParams(); const { getToken } = useAuth();
  const runId = search.get("run_id"); const base = runId ? `&run_id=${encodeURIComponent(runId)}` : "";
  const audit = useApi(`/v1/servicemind/audit-events?limit=50${base}`, auditPageSchema, { refreshInterval: 15000 });
  const [extra, setExtra] = useState<AuditEvent[]>([]); const [cursor, setCursor] = useState<{ at: string; id: string } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false); const [pageError, setPageError] = useState<string | null>(null);
  const initialCursor = audit.data?.next_after_created_at && audit.data.next_after_event_id ? { at: audit.data.next_after_created_at, id: audit.data.next_after_event_id } : null;
  const events = [...(audit.data?.items ?? []), ...extra];
  async function loadMore() {
    const next = cursor ?? initialCursor; if (!next) return;
    setLoadingMore(true); setPageError(null);
    try {
      const params = new URLSearchParams({ limit: "50", after_created_at: next.at, after_event_id: next.id });
      if (runId) params.set("run_id", runId);
      const page = await apiRequest(`/v1/servicemind/audit-events?${params}`, getToken, auditPageSchema);
      setExtra((current) => [...current, ...page.items]);
      setCursor(page.next_after_created_at && page.next_after_event_id ? { at: page.next_after_created_at, id: page.next_after_event_id } : null);
    } catch (cause) { setPageError(cause instanceof Error ? cause.message : "下一页读取失败"); }
    finally { setLoadingMore(false); }
  }
  const hasMore = cursor !== null || (extra.length === 0 && initialCursor !== null);
  return <><PageHeader eyebrow="治理与合规" title="审计记录" description={runId ? `仅显示运行 ${shortId(runId)} 的治理事件。` : "按时间倒序查看当前租户的审批、复核和运行治理记录。"} actions={<span className="readonly-label">只读</span>} />{audit.isLoading ? <LoadingState /> : audit.error ? <ErrorState error={audit.error} retry={() => void audit.mutate()} /> : events.length ? <><ol className="audit-list">{events.map((event) => <li key={event.id}><article><header><div><strong>{readableLabel(event.event_type)}</strong><small>{readableLabel(event.resource_type)} · {shortId(event.resource_id)}</small></div><time>{formatDate(event.created_at)}</time></header><dl><div><dt>操作者</dt><dd>{shortId(event.actor_id)}</dd></div>{event.run_id && <div><dt>运行</dt><dd>{shortId(event.run_id)}</dd></div>}</dl>{Object.keys(event.payload).length > 0 && <details><summary>查看审计载荷</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</article></li>)}</ol>{pageError && <p className="inline-error" role="alert">{pageError}</p>}{hasMore && <div className="load-more"><button className="button button--secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在读取" : "加载更早记录"}</button></div>}</> : <EmptyState title="尚无审计记录" description="审批、取消、人工复核等治理动作发生后会追加到这里。" />}</>;
}

export default function AuditPage() { return <section className="page"><Suspense fallback={<LoadingState />}><AuditLedger /></Suspense></section>; }
