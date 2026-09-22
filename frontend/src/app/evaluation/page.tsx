"use client";
import { AlertOctagon, CheckCircle2, Gauge, Server } from "lucide-react";
import useSWR from "swr";
import { PageHeader } from "@/components/ui/page-header";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { GateBadge } from "@/components/ui/status";
import { publicRequest } from "@/lib/api";
import { releaseStatusSchema } from "@/lib/contracts";
import { formatDate, formatPercent } from "@/lib/format";

export default function EvaluationPage() {
  const status = useSWR("/release-status.json", (url) => publicRequest(url, releaseStatusSchema), { revalidateOnFocus: false });
  if (status.isLoading) return <section className="page"><LoadingState label="正在读取冻结的验收快照" /></section>;
  if (status.error || !status.data) return <section className="page"><ErrorState error={status.error} retry={() => void status.mutate()} /></section>;
  const data = status.data;
  const ragMetrics = [["Recall@5", data.rag.recall_at_5, .85], ["Recall@10", data.rag.recall_at_10, .9], ["MRR@10", data.rag.mrr_at_10, .75], ["NDCG@10", data.rag.ndcg_at_10, .8]] as const;
  return <section className="page"><PageHeader eyebrow="Release evidence / Frozen snapshot" title="质量门禁不粉饰未闭合项" description={`此页来自仓库验收产物的脱敏快照，生成于 ${formatDate(data.generated_at)}。它不是实时生产遥测。`} actions={<GateBadge status={data.release_decision} />} />
    <div className="quality-strip"><article><CheckCircle2 aria-hidden="true" /><span>工程结构</span><strong>{data.structure.checks_passed}/{data.structure.checks_total}</strong><GateBadge status={data.structure.status} /></article><article><Gauge aria-hidden="true" /><span>Phase 5</span><strong>{data.phase5.tests_passed ?? "—"}</strong><GateBadge status={data.phase5.status} /></article><article><CheckCircle2 aria-hidden="true" /><span>Phase 6</span><strong>{data.phase6.tests_passed ?? "—"}</strong><GateBadge status={data.phase6.status} /></article><article><Server aria-hidden="true" /><span>运行环境</span><strong>{data.runtime.health_ok === null ? "—" : data.runtime.health_ok ? "HEALTHY" : "DOWN"}</strong><GateBadge status={data.runtime.status} /></article></div>
    <section className="rag-panel"><div className="rag-copy"><p className="section-kicker">RAG / 外部银标代理集</p><h2>指标仍低于租户发布门槛</h2><p>{data.rag.scope}</p><GateBadge status={data.rag.status} /></div><div className="metric-bars">{ragMetrics.map(([label, value, target]) => <div className="metric" key={label}><div><span>{label}</span><strong>{formatPercent(value)}</strong></div><div className="bar" aria-label={`${label} ${formatPercent(value)}，门槛 ${formatPercent(target)}`}><span style={{ width: `${Math.min((value ?? 0) * 100, 100)}%` }} /><i style={{ left: `${target * 100}%` }} /></div><small>发布门槛 {formatPercent(target, 0)}</small></div>)}</div></section>
    <section className="caveats"><div className="section-title"><div><p className="section-kicker">诚实边界</p><h2>发布前仍需正视</h2></div><AlertOctagon aria-hidden="true" /></div><ul>{data.caveats.map((item) => <li key={item}>{item}</li>)}</ul></section>
  </section>;
}
