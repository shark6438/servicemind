"use client";

import useSWR from "swr";
import { PageHeader } from "@/components/ui/page-header";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { GateBadge } from "@/components/ui/status";
import { publicRequest } from "@/lib/api";
import { releaseStatusSchema } from "@/lib/contracts";
import { formatDate, formatPercent } from "@/lib/format";

export default function EvaluationPage() {
  const status = useSWR("/release-status.json", (url) => publicRequest(url, releaseStatusSchema), { revalidateOnFocus: false });
  if (status.isLoading) return <section className="page"><LoadingState label="正在读取验收快照" /></section>;
  if (status.error || !status.data) return <section className="page"><ErrorState error={status.error} retry={() => void status.mutate()} /></section>;
  const data = status.data;
  const ragMetrics = [
    ["前 5 项召回率", data.rag.recall_at_5, .85],
    ["前 10 项召回率", data.rag.recall_at_10, .9],
    ["前 10 项平均倒数排名", data.rag.mrr_at_10, .75],
    ["前 10 项归一化折损累计增益", data.rag.ndcg_at_10, .8],
  ] as const;
  const gates = [
    ["项目结构", `${data.structure.checks_passed}/${data.structure.checks_total} 项`, data.structure.status],
    ["记忆与上下文治理", data.phase5.tests_passed === null ? "无测试数据" : `${data.phase5.tests_passed} 项测试`, data.phase5.status],
    ["工具与执行治理", data.phase6.tests_passed === null ? "无测试数据" : `${data.phase6.tests_passed} 项测试`, data.phase6.status],
    ["运行环境", data.runtime.health_ok === null ? "未检查" : data.runtime.health_ok ? "服务正常" : "服务异常", data.runtime.status],
  ] as const;

  return (
    <section className="page">
      <PageHeader eyebrow="发布管理" title="质量与发布状态" description={`展示仓库验收产物的脱敏快照，生成于 ${formatDate(data.generated_at)}；不代表实时生产遥测。`} actions={<GateBadge status={data.release_decision} />} />
      <section className="data-section">
        <div className="section-title"><div><h2>工程门禁</h2><p>当前冻结快照</p></div></div>
        <div className="table-wrap"><table className="gate-table"><thead><tr><th>范围</th><th>验收数据</th><th>结论</th></tr></thead><tbody>{gates.map(([name, evidence, gateStatus]) => <tr key={name}><td>{name}</td><td>{evidence}</td><td><GateBadge status={gateStatus} /></td></tr>)}</tbody></table></div>
      </section>
      <section className="data-section">
        <div className="section-title"><div><h2>知识检索质量</h2><p>{data.rag.scope}</p></div><GateBadge status={data.rag.status} /></div>
        <div className="table-wrap"><table className="metric-table"><thead><tr><th>指标</th><th>当前值</th><th>参考门槛</th><th>差距</th></tr></thead><tbody>{ragMetrics.map(([label, value, target]) => <tr key={label}><td>{label}</td><td className="mono">{formatPercent(value)}</td><td className="mono">{formatPercent(target, 0)}</td><td className="mono text-danger">{value === null ? "未测" : `${((value - target) * 100).toFixed(1)} 个百分点`}</td></tr>)}</tbody></table></div>
      </section>
      <section className="caveats"><h2>发布前待关闭事项</h2><ul>{data.caveats.map((item) => <li key={item}>{item}</li>)}</ul></section>
    </section>
  );
}
