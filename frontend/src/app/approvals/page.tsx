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
  return <section className="page"><PageHeader eyebrow="人工处理" title="审批中心" description="核对运行证据、复核结论和已冻结的动作摘要后作出决定。" /><div className="callout callout--warning"><ShieldAlert aria-hidden="true" /><div><strong>批准后系统才会继续执行</strong><p>服务端会再次校验角色、租户范围、动作摘要和运行状态，过期页面不能覆盖已变化的内容。</p></div></div>{approvals.isLoading || reviews.isLoading ? <LoadingState /> : error ? <ErrorState error={error} retry={() => { void approvals.mutate(); void reviews.mutate(); }} /> : items.length ? <div className="approval-list">{items.map((run) => <Link href={`/runs/${run.id}`} className="approval-row" key={run.id}><div><small>GLPI #{run.ticket_id} · {shortId(run.id)}</small><strong>{run.goal}</strong></div><StatusBadge status={run.status} /><time>{formatDate(run.updated_at)}</time><ArrowUpRight aria-hidden="true" /></Link>)}</div> : <EmptyState title="人工队列已清空" description="当前没有等待审批或复核处理的运行。" />}</section>;
}
