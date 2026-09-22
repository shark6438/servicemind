"use client";

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

  return (
    <section className="page">
      <header className="workspace-header">
        <div><h1>工作台</h1><p>向运维智能体提问，查看当前租户的运行与待办。</p></div>
        <span className="updated-at">数据每 15 秒更新</span>
      </header>
      <div className="dashboard-grid">
        <CreateRunForm />
        <section className="summary-panel" aria-labelledby="summary-title">
          <header><h2 id="summary-title">运行概览</h2><span>最近 8 条</span></header>
          <dl className="summary-list">
            <div><dt>正在执行</dt><dd>{active}</dd></div>
            <div><dt>等待人工处理</dt><dd>{waiting}</dd></div>
            <div><dt>已验证完成</dt><dd>{succeeded}</dd></div>
            <div><dt>GLPI 连接</dt><dd className={glpi.error ? "text-danger" : "text-success"}>{glpi.data?.authenticated ? "正常" : glpi.isLoading ? "检查中" : "异常"}</dd></div>
          </dl>
          <p className="summary-note">{glpi.data ? `当前实体 ${glpi.data.entity_id}，接口版本 ${glpi.data.api_version}` : "正在读取服务状态"}</p>
        </section>
      </div>
      <section className="ledger-section">
        <div className="section-title"><div><h2>最近运行</h2><p>按更新时间倒序显示</p></div><Link className="text-link" href="/runs">查看全部运行</Link></div>
        {runs.isLoading ? <LoadingState /> : runs.error ? <ErrorState error={runs.error} retry={() => void runs.mutate()} /> : items.length ? <RunTable runs={items} /> : <EmptyState title="还没有运行记录" description="在上方描述问题并关联工单，开始第一条智能体运行。" />}
      </section>
    </section>
  );
}
