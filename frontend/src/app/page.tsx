"use client";
import { Activity, ArrowUpRight, CheckCircle2, ServerCog, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { CreateRunForm } from "@/components/runs/create-run-form";
import { RunTable } from "@/components/runs/run-table";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { glpiHealthSchema, runListSchema } from "@/lib/contracts";
import { useApi } from "@/hooks/use-api";

export default function Home() {
  const runs = useApi("/v1/servicemind/runs?limit=8", runListSchema, { refreshInterval: 15000 });
  const glpi = useApi("/v1/servicemind/glpi/health", glpiHealthSchema, { refreshInterval: 60000 });
  const items = runs.data?.items ?? [];
  const waiting = items.filter((run) => run.status === "waiting_approval" || run.status === "waiting_review").length;
  const active = items.filter((run) => run.status === "pending" || run.status === "running").length;
  const succeeded = items.filter((run) => run.status === "succeeded").length;
  return <section className="page">
    <header className="hero"><div><p className="eyebrow">运营态势 / LIVE</p><h1>从工单到行动，<br /><span>沿证据链推进。</span></h1><p>当前视图只展示本租户可见的真实运行。写操作必须经过策略、Reviewer 与人工批准。</p></div><div className="hero-stamp"><span>{new Date().toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}</span><small>控制平面在线</small></div></header>
    <div className="dashboard-grid"><CreateRunForm /><div className="signal-board" aria-label="运行态势"><article><Activity aria-hidden="true" /><span>正在推进</span><strong>{active.toString().padStart(2, "0")}</strong><small>最近 8 次运行</small></article><article><ShieldAlert aria-hidden="true" /><span>等待人工</span><strong>{waiting.toString().padStart(2, "0")}</strong><small>审批与复核</small></article><article><CheckCircle2 aria-hidden="true" /><span>已验证完成</span><strong>{succeeded.toString().padStart(2, "0")}</strong><small>最终状态已落账</small></article><article className={glpi.error ? "signal-off" : ""}><ServerCog aria-hidden="true" /><span>GLPI 连接</span><strong>{glpi.data?.authenticated ? "OK" : glpi.isLoading ? "…" : "ERR"}</strong><small>{glpi.data ? `实体 ${glpi.data.entity_id} · API ${glpi.data.api_version}` : "实时探测"}</small></article></div></div>
    <section className="ledger-section"><div className="section-title"><div><p className="section-kicker">近期运行</p><h2>租户运行账本</h2></div><Link className="text-link" href="/runs">查看全部 <ArrowUpRight aria-hidden="true" /></Link></div>{runs.isLoading ? <LoadingState /> : runs.error ? <ErrorState error={runs.error} retry={() => void runs.mutate()} /> : items.length ? <RunTable runs={items} /> : <EmptyState title="还没有运行记录" description="使用上方表单从一张真实 GLPI 工单启动第一次受控调查。" />}</section>
  </section>;
}
