"use client";

import { ArrowUpRight, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status";
import { apiRequest } from "@/lib/api";
import { runListSchema, type RunSummary } from "@/lib/contracts";
import { formatDate, shortId } from "@/lib/format";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/providers/auth-provider";

type QueueType = "approval" | "review";
const queueConfig = {
  approval: {
    status: "waiting_approval",
    label: "待审批",
    guidance: "先核对证据与冻结动作，再决定是否允许写入。",
    empty: "当前没有等待批准的动作。",
  },
  review: {
    status: "waiting_review",
    label: "待复核",
    guidance: "检查升级原因与证据，再决定继续或停止。",
    empty: "当前没有等待人工复核的运行。",
  },
} as const;

function Queue({ type }: { type: QueueType }) {
  const { getToken } = useAuth();
  const config = queueConfig[type];
  const [extra, setExtra] = useState<RunSummary[]>([]);
  const [cursor, setCursor] = useState<{ at: string; id: string } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const queue = useApi(`/v1/servicemind/runs?limit=50&status=${config.status}`, runListSchema, { refreshInterval: 10000 });
  const items = [...(queue.data?.items ?? []), ...extra];
  const firstCursor = queue.data?.next_after_created_at && queue.data.next_after_run_id
    ? { at: queue.data.next_after_created_at, id: queue.data.next_after_run_id } : null;
  const hasMore = cursor !== null || (extra.length === 0 && firstCursor !== null);

  async function loadMore() {
    const next = cursor ?? firstCursor;
    if (!next) return;
    setLoadingMore(true);
    setPageError(null);
    try {
      const params = new URLSearchParams({
        limit: "50", status: config.status,
        after_created_at: next.at, after_run_id: next.id,
      });
      const page = await apiRequest(`/v1/servicemind/runs?${params}`, getToken, runListSchema);
      setExtra((current) => [...current, ...page.items]);
      setCursor(page.next_after_created_at && page.next_after_run_id
        ? { at: page.next_after_created_at, id: page.next_after_run_id } : null);
    } catch (cause) {
      setPageError(cause instanceof Error ? cause.message : "读取失败，请重试。");
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <>
      <div className="queue-toolbar"><nav className="queue-tabs" aria-label="审批队列类型">
        <Link href="/approvals" aria-current={type === "approval" ? "page" : undefined}>待审批</Link>
        <Link href="/approvals?view=review" aria-current={type === "review" ? "page" : undefined}>待复核</Link>
      </nav><span className="queue-loaded">当前载入 {items.length} 条</span></div>
      <div className="callout callout--warning"><ShieldAlert aria-hidden="true" /><div><strong>{config.label}事项</strong><p>{config.guidance}服务端会重新校验角色、租户与运行状态。</p></div></div>
      {queue.isLoading ? <LoadingState /> : queue.error ? <ErrorState error={queue.error} retry={() => void queue.mutate()} /> : items.length ? <>
        <div className="approval-list">{items.map((run) => <Link href={`/runs/${run.id}`} className="approval-row" key={run.id}>
          <div><small>GLPI #{run.ticket_id} · {shortId(run.id)}</small><strong>{run.goal}</strong></div>
          <StatusBadge status={run.status} /><time>{formatDate(run.updated_at)}</time><ArrowUpRight aria-hidden="true" />
        </Link>)}</div>
        {pageError && <p className="inline-error" role="alert">{pageError}</p>}
        {hasMore && <div className="load-more"><button className="button button--secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在读取…" : `加载更早的${config.label}事项`}</button></div>}
      </> : <EmptyState title={`${config.label}队列已清空`} description={config.empty} />}
    </>
  );
}

function ApprovalQueues() {
  const search = useSearchParams();
  const type: QueueType = search.get("view") === "review" ? "review" : "approval";
  return <Queue key={type} type={type} />;
}

export default function ApprovalsPage() {
  return <section className="page"><PageHeader eyebrow="人工处理" title="审批中心" description="将写入审批与复核升级分开处理，每项决定都回到运行详情核对。" /><Suspense fallback={<LoadingState />}><ApprovalQueues /></Suspense></section>;
}
