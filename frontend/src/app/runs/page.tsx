"use client";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { RunTable } from "@/components/runs/run-table";
import { runStatusLabels } from "@/components/ui/status";
import { apiRequest } from "@/lib/api";
import { runListSchema, runStatusSchema, type RunSummary } from "@/lib/contracts";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/providers/auth-provider";

function RunLedger() {
  const router = useRouter(); const search = useSearchParams(); const { getToken } = useAuth();
  const requestedStatus = search.get("status") ?? "";
  const status = runStatusSchema.safeParse(requestedStatus).success ? requestedStatus : "";
  const [extra, setExtra] = useState<RunSummary[]>([]); const [cursor, setCursor] = useState<{ at: string; id: string } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false); const [pageError, setPageError] = useState<string | null>(null);
  const query = status ? `?limit=50&status=${status}` : "?limit=50";
  const runs = useApi(`/v1/servicemind/runs${query}`, runListSchema, { refreshInterval: 15000 });
  const items = [...(runs.data?.items ?? []), ...extra];
  const initialCursor = runs.data?.next_after_created_at && runs.data.next_after_run_id ? { at: runs.data.next_after_created_at, id: runs.data.next_after_run_id } : null;
  async function loadMore() {
    const next = cursor ?? initialCursor; if (!next) return;
    setLoadingMore(true); setPageError(null);
    try {
      const params = new URLSearchParams({ limit: "50", after_created_at: next.at, after_run_id: next.id });
      if (status) params.set("status", status);
      const page = await apiRequest(`/v1/servicemind/runs?${params}`, getToken, runListSchema);
      setExtra((current) => [...current, ...page.items]);
      setCursor(page.next_after_created_at && page.next_after_run_id ? { at: page.next_after_created_at, id: page.next_after_run_id } : null);
    } catch (cause) { setPageError(cause instanceof Error ? cause.message : "下一页读取失败"); }
    finally { setLoadingMore(false); }
  }
  function changeStatus(next: string) { setExtra([]); setCursor(null); router.replace(next ? `/runs?status=${next}` : "/runs"); }
  const hasMore = cursor !== null || (extra.length === 0 && initialCursor !== null);
  return <><PageHeader eyebrow="运行管理" title="运行记录" description="查看当前租户的工单调查、执行状态和写操作权限。" actions={<label className="filter-label">状态筛选<select name="run_status" value={status} onChange={(event) => changeStatus(event.target.value)}><option value="">全部状态</option>{runStatusSchema.options.map((option) => <option key={option} value={option}>{runStatusLabels[option]}</option>)}</select></label>} />
    {runs.isLoading ? <LoadingState /> : runs.error ? <ErrorState error={runs.error} retry={() => void runs.mutate()} /> : items.length ? <><RunTable runs={items} />{pageError && <p className="inline-error" role="alert">{pageError}</p>}{hasMore && <div className="load-more"><button className="button button--secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在读取…" : "加载更早运行"}</button></div>}</> : <EmptyState title="当前筛选没有运行" description="更换状态条件，或回到工作台启动一条真实调查。" />}</>;
}

export default function RunsPage() { return <section className="page"><Suspense fallback={<LoadingState />}><RunLedger /></Suspense></section>; }
