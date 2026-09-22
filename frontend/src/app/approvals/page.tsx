"use client";
import { ArrowUpRight, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status";
import { runListSchema } from "@/lib/contracts";
import { formatDate, shortId } from "@/lib/format";
import { useApi } from "@/hooks/use-api";

export default function ApprovalsPage() {
  const approvals = useApi("/v1/servicemind/runs?limit=100&status=waiting_approval", runListSchema, { refreshInterval: 10000 });
  const reviews = useApi("/v1/servicemind/runs?limit=100&status=waiting_review", runListSchema, { refreshInterval: 10000 });
  const items = [...(approvals.data?.items ?? []), ...(reviews.data?.items ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const error = approvals.error ?? reviews.error;
  return <section className="page"><PageHeader eyebrow="人工门禁 / Exact snapshot" title="需要人承担的决定" description="审批不会自动发生。打开运行，核对证据、Reviewer 结论与冻结的 action hash 后再作决定。" /><div className="callout callout--warning"><ShieldAlert aria-hidden="true" /><div><strong>批准意味着承担执行责任</strong><p>服务端会再次校验角色、租户范围、动作哈希和运行状态。页面上的旧快照不能覆盖已变化的意图。</p></div></div>{approvals.isLoading || reviews.isLoading ? <LoadingState /> : error ? <ErrorState error={error} retry={() => { void approvals.mutate(); void reviews.mutate(); }} /> : items.length ? <div className="approval-list">{items.map((run) => <Link href={`/runs/${run.id}`} className="approval-row" key={run.id}><div><small>GLPI #{run.ticket_id} · {shortId(run.id)}</small><strong>{run.goal}</strong></div><StatusBadge status={run.status} /><time>{formatDate(run.updated_at)}</time><ArrowUpRight aria-hidden="true" /></Link>)}</div> : <EmptyState title="人工队列已清空" description="当前没有等待审批或 Reviewer 升级处理的运行。" />}</section>;
}
