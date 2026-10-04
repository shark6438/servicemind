"use client";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";
import { TaskDag } from "@/components/runs/task-dag";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { StatusBadge } from "@/components/ui/status";
import { apiRequest } from "@/lib/api";
import { runDetailSchema, runTimelineSchema, type RunDetail } from "@/lib/contracts";
import { formatDate, readableLabel, safeJson, shortId } from "@/lib/format";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/providers/auth-provider";

type Obj = Record<string, unknown>;
function object(value: unknown): Obj | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Obj : null; }
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function text(value: unknown, fallback = "—"): string { return typeof value === "string" && value ? value : fallback; }

type Decision = "approved" | "rejected" | "continue" | "stop";

function HumanDecision({ runId, mode, action, onDone }: {
  runId: string;
  mode: "approval" | "review";
  action?: NonNullable<RunDetail["action_intent"]>;
  onDone: () => void;
}) {
  const { roles, getToken } = useAuth();
  const [comment, setComment] = useState("");
  const [pending, setPending] = useState(false);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const allowed = roles.has("approver");

  if (!allowed) {
    return <section className="decision-box decision-box--readonly"><p className="section-kicker">人工决定</p><h2>等待审批人处理</h2><p className="decision-intro">当前身份可以查看证据与待处理动作，不能提交{mode === "approval" ? "写入审批" : "复核决定"}。</p></section>;
  }

  function requestDecision(next: Decision, button: HTMLButtonElement) {
    trigger.current = button;
    setDecision(next);
    setError(null);
    requestAnimationFrame(() => {
      dialog.current?.showModal();
      dialog.current?.focus();
      dialog.current?.scrollTo({ top: 0 });
    });
  }

  async function submitDecision() {
    if (!decision || pending) return;
    setPending(true);
    setError(null);
    const path = mode === "approval" ? `/v1/servicemind/runs/${runId}/approval` : `/v1/servicemind/runs/${runId}/review-resolution`;
    const body = mode === "approval"
      ? { decision, expected_action_hash: action?.action_hash, comment }
      : { decision, comment };
    try {
      await apiRequest(path, getToken, runDetailSchema, { method: "POST", body: JSON.stringify(body) });
      dialog.current?.close();
      onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交失败，请核对运行状态后重试。");
    } finally {
      setPending(false);
    }
  }

  const decisionLabel = decision === "approved" ? "批准并继续" : decision === "rejected" ? "拒绝动作" : decision === "continue" ? "接受并收敛" : "停止运行";
  return (
    <section className="decision-box">
      <p className="section-kicker">人工决定</p>
      <h2>{mode === "approval" ? "核对冻结动作并决策" : "处理复核升级"}</h2>
      <p className="decision-intro">{mode === "approval" ? "提交前核对目标、风险与动作摘要。" : "复核证据后决定继续或停止。"}</p>
      <div className="decision-actions">
        {mode === "approval" ? <>
          <button className="button button--danger" disabled={pending} onClick={(event) => requestDecision("rejected", event.currentTarget)}>拒绝</button>
          <button className="button button--primary" disabled={pending} onClick={(event) => requestDecision("approved", event.currentTarget)}>批准并继续</button>
        </> : <>
          <button className="button button--danger" disabled={pending} onClick={(event) => requestDecision("stop", event.currentTarget)}>停止运行</button>
          <button className="button button--primary" disabled={pending} onClick={(event) => requestDecision("continue", event.currentTarget)}>接受并收敛</button>
        </>}
      </div>
      <dialog ref={dialog} tabIndex={-1} className="decision-dialog" aria-labelledby="decision-dialog-title" onClose={() => { setDecision(null); trigger.current?.focus(); }} onCancel={(event) => { if (pending) event.preventDefault(); }}>
        <div className="decision-dialog-head"><p className="section-kicker">最终核对</p><h3 id="decision-dialog-title">{decisionLabel}</h3><p>此决定会写入审计记录；过期动作会由服务端拒绝。</p></div>
        {mode === "approval" && action && <dl className="decision-review-list">
          <div><dt>动作</dt><dd>{readableLabel(action.action_type)}</dd></div>
          <div><dt>目标工单</dt><dd>#{action.target_id}</dd></div>
          <div><dt>风险</dt><dd>{readableLabel(action.risk_level)}</dd></div>
          <div><dt>动作摘要</dt><dd className="hash">{action.action_hash}</dd></div>
        </dl>}
        {mode === "approval" && action && <div className="decision-preview"><strong>预演说明</strong><div className="decision-preview-text" role="region" aria-label="完整预演说明，可滚动" tabIndex={0}><p>{action.dry_run_preview ?? "没有预演说明，请返回核对动作参数。"}</p></div></div>}
        <label>决定说明<textarea name={`${mode}_comment`} autoComplete="off" rows={3} maxLength={1000} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="写下本次决定的依据…" /></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="decision-dialog-actions"><button className="button button--secondary" disabled={pending} onClick={() => dialog.current?.close()}>返回核对</button><button className={decision === "rejected" || decision === "stop" ? "button button--danger" : "button button--primary"} disabled={pending} onClick={() => void submitDecision()}>{pending ? "正在提交…" : `确认${decisionLabel}`}</button></div>
      </dialog>
    </section>
  );
}

export function RunDetailView({ runId }: { runId: string }) {
  const run = useApi(`/v1/servicemind/runs/${runId}`, runDetailSchema, { refreshInterval: 10000 });
  const timeline = useApi(`/v1/servicemind/runs/${runId}/timeline`, runTimelineSchema, { refreshInterval: 10000 });
  if (run.isLoading) return <section className="page"><LoadingState /></section>;
  if (run.error || !run.data) return <section className="page"><ErrorState error={run.error} retry={() => void run.mutate()} /></section>;
  const data = run.data; const result = data.result ?? {}; const plan = object(result.task_plan);
  const tasks = list(plan?.tasks).map(object).filter((item): item is Obj => item !== null).map((task) => ({
    id: text(task.task_id), type: text(task.task_type), agent: text(task.agent),
    status: text(task.status), dependsOn: list(task.depends_on).filter((value): value is string => typeof value === "string"),
  }));
  const evidenceRoot = object(result.evidence); const evidence = list(evidenceRoot?.items ?? result.evidence).map(object).filter((item): item is Obj => item !== null);
  const analysis = object(result.analysis); const review = object(result.review); const reviewFeedback = text(review?.feedback); const trajectory = list(result.trajectory).filter((item): item is string => typeof item === "string");
  return <section className="page"><Link className="back-link" href="/runs"><ArrowLeft />返回运行记录</Link><PageHeader eyebrow={`运行 ${shortId(data.id)} · 工单 #${data.ticket_id}`} title={data.goal} description={`创建于 ${formatDate(data.created_at)} · 最近更新 ${formatDate(data.updated_at)}`} actions={<StatusBadge status={data.status} />} />
    {data.error && <div className="callout callout--error"><AlertTriangle /><div><strong>运行失败</strong><p>{data.error}</p></div></div>}
    <div className="detail-grid"><div className="detail-main">
      {tasks.length > 0 && <section className="panel"><div className="section-title"><div><h2>任务依赖图</h2><p>并行任务位于同一行，连线表示实际依赖</p></div><span className="section-count">{tasks.length} 个任务</span></div><TaskDag tasks={tasks} /></section>}
      <section className="panel"><div className="section-title"><div><h2>证据记录</h2><p>本次运行实际使用的证据与来源</p></div></div>{evidence.length ? <div className="evidence-list">{evidence.map((item, index) => { const provenance = object(item.provenance); return <article key={text(item.evidence_id, String(index))}><header><span>{readableLabel(text(item.source_type))}</span><code>{text(item.evidence_id)}</code></header><p>{text(item.content)}</p><footer><span>{text(item.source_ref)}</span><span>{readableLabel(text(provenance?.provider))} · {readableLabel(text(provenance?.retrieval_method))}</span>{typeof item.confidence === "number" && <span>置信度 {(item.confidence * 100).toFixed(0)}%</span>}</footer></article>; })}</div> : <EmptyState title="暂无可展示证据" description="运行尚未完成证据汇合，或以明确弃答结束。" />}</section>
      {analysis && <section className="panel analysis-panel"><div className="section-title"><div><h2>分析与建议</h2><p>基于已列明证据生成</p></div></div><p className="reasoning">{text(analysis.reasoning_summary)}</p><dl className="analysis-facts"><div><dt>分类</dt><dd>{readableLabel(text(analysis.classification))}</dd></div><div><dt>优先级</dt><dd>P{text(analysis.priority)}</dd></div><div><dt>建议组</dt><dd>{text(analysis.recommended_group)}</dd></div><div><dt>置信度</dt><dd>{typeof analysis.confidence === "number" ? `${(analysis.confidence * 100).toFixed(0)}%` : "—"}</dd></div></dl><div className="recommendations"><article><small>问题管理建议</small><p>{text(analysis.problem_recommendation)}</p></article><article><small>变更管理建议</small><p>{text(analysis.change_recommendation)}</p></article></div></section>}
      <details className="panel trace-disclosure"><summary><span><strong>完整执行轨迹</strong><small>按实际发生顺序记录</small></span><span className="trace-count">{trajectory.length} 个节点</span></summary>{trajectory.length ? <ol className="trajectory">{trajectory.map((step, index) => <li key={`${step}-${index}`}><span>{index + 1}</span><strong>{readableLabel(step)}</strong></li>)}</ol> : <EmptyState title="轨迹尚未形成" description="运行开始推进后，各处理阶段会出现在这里。" />}</details>
    </div><aside className="detail-aside"><section className="panel compact"><h2>控制信息</h2><dl className="fact-list"><div><dt>发起者</dt><dd>{shortId(data.user_id)}</dd></div><div><dt>模式</dt><dd>{data.request_write ? "允许提出写操作建议" : "只读分析"}</dd></div><div><dt>最终状态验证</dt><dd>{result.final_state_verified === true ? "已验证" : "未完成"}</dd></div><div><dt>控制权</dt><dd>{readableLabel(text(result.control_owner))}</dd></div></dl></section>
      {review && <section className="panel compact"><h2>复核结论</h2><div className={`review-verdict ${review.decision === "passed" ? "review-verdict--passed" : review.decision === "reject" || review.decision === "abstain" ? "review-verdict--stopped" : "review-verdict--attention"}`}><strong>{readableLabel(text(review.decision))}</strong></div>{reviewFeedback.length > 220 ? <><p className="review-excerpt">{reviewFeedback.slice(0, 180).trimEnd()}…</p><details className="review-feedback"><summary>阅读完整评审说明</summary><p>{reviewFeedback}</p></details></> : <p className="review-excerpt">{reviewFeedback}</p>}<dl className="fact-list"><div><dt>风险</dt><dd>{readableLabel(text(review.risk_level))}</dd></div><div><dt>策略版本</dt><dd>{text(review.policy_version)}</dd></div><div><dt>结论置信度</dt><dd>{typeof review.confidence === "number" ? `${(review.confidence * 100).toFixed(0)}%` : "—"}</dd></div></dl></section>}
      {data.action_intent && <section className="panel compact action-card"><h2>待执行动作</h2><strong>{readableLabel(data.action_intent.action_type)}</strong><p>{data.action_intent.dry_run_preview ?? "没有预演说明"}</p><dl className="fact-list"><div><dt>目标</dt><dd>#{data.action_intent.target_id}</dd></div><div><dt>风险</dt><dd>{readableLabel(data.action_intent.risk_level)}</dd></div><div><dt>动作摘要</dt><dd className="hash">{shortId(data.action_intent.action_hash)}</dd></div></dl><details><summary>查看参数</summary><pre>{safeJson(data.action_intent.arguments)}</pre></details></section>}
      {data.status === "waiting_approval" && data.action_intent && <HumanDecision runId={data.id} mode="approval" action={data.action_intent} onDone={() => { void run.mutate(); void timeline.mutate(); }} />}
      {data.status === "waiting_review" && <HumanDecision runId={data.id} mode="review" onDone={() => { void run.mutate(); void timeline.mutate(); }} />}
      <section className="panel compact"><h2>运行事件</h2>{timeline.error ? <ErrorState error={timeline.error} retry={() => void timeline.mutate()} /> : timeline.data?.length ? <ol className="mini-timeline">{timeline.data.map((event) => <li key={event.sequence}><span>{event.sequence}</span><div><strong>{readableLabel(event.type)}</strong><small>{formatDate(event.created_at)}</small></div></li>)}</ol> : <p className="muted">暂无事件。</p>}<Link className="text-link" href={`/audit?run_id=${data.id}`}>查看完整审计记录</Link></section>
    </aside></div>
  </section>;
}
