"use client";
import { useState } from "react";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { apiRequest } from "@/lib/api";
import { memoryQueueSchema, memoryReviewItemSchema, type MemoryReviewItem } from "@/lib/contracts";
import { formatDate, readableLabel } from "@/lib/format";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/providers/auth-provider";

function ReviewCard({ item, onDone }: { item: MemoryReviewItem; onDone: () => void }) {
  const { getToken } = useAuth(); const [open, setOpen] = useState(false); const [reviewRef, setReviewRef] = useState("");
  const [comment, setComment] = useState(""); const [pending, setPending] = useState(false); const [error, setError] = useState<string | null>(null);
  async function decide(decision: "activate" | "reject") {
    const confirmed = window.confirm(
      decision === "activate"
        ? "确认激活这条记忆？系统会在后续运行中按其作用域提供该内容。"
        : "确认拒绝并撤销这条记忆？此决定会写入审计账本。",
    );
    if (!confirmed) return;
    setPending(true); setError(null);
    try { await apiRequest(`/v1/servicemind/memories/${item.memory_id}/review`, getToken, memoryReviewItemSchema, { method: "POST", body: JSON.stringify({ decision, expected_version: item.version, expected_content_hash: item.content_hash, review_ref: reviewRef, comment }) }); onDone(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "复核提交失败"); } finally { setPending(false); }
  }
  return <article className="memory-row"><header><div><h2>{item.subject_key}</h2><p className="memory-meta">{readableLabel(item.memory_type)} · {formatDate(item.created_at)} · 版本 {item.version}</p></div><button className="button button--secondary" onClick={() => setOpen((value) => !value)}>{open ? "收起" : "复核"}</button></header><p className="memory-content">{item.content}</p><dl className="memory-stats"><div><dt>置信度</dt><dd>{(item.confidence * 100).toFixed(0)}%</dd></div><div><dt>重要度</dt><dd>{(item.importance * 100).toFixed(0)}%</dd></div><div><dt>支撑情节</dt><dd>{item.supporting_episode_ids.length}</dd></div></dl><details><summary>查看审计快照</summary><dl className="snapshot"><div><dt>记忆编号</dt><dd>{item.memory_id}</dd></div><div><dt>内容摘要</dt><dd>{item.content_hash}</dd></div></dl></details>{open && <div className="decision-panel"><label>复核依据编号<input name={`review_ref_${item.memory_id}`} autoComplete="off" required minLength={3} maxLength={500} value={reviewRef} onChange={(e) => setReviewRef(e.target.value)} placeholder="例如 CAB-2026-0917" /></label><label>决定说明<textarea name={`review_comment_${item.memory_id}`} autoComplete="off" required minLength={3} maxLength={1000} rows={3} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="说明激活或拒绝的依据" /></label>{error && <p className="form-error" role="alert">{error}</p>}<div className="decision-actions"><button className="button button--danger" disabled={pending || reviewRef.length < 3 || comment.length < 3} onClick={() => void decide("reject")}>拒绝并撤销</button><button className="button button--primary" disabled={pending || reviewRef.length < 3 || comment.length < 3} onClick={() => void decide("activate")}>激活记忆</button></div></div>}</article>;
}

export default function MemoryPage() {
  const { getToken } = useAuth();
  const queue = useApi("/v1/servicemind/memories/review-queue?limit=50", memoryQueueSchema, { refreshInterval: 15000 });
  const [extra, setExtra] = useState<MemoryReviewItem[]>([]); const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<{ at: string; id: string } | null>(null); const [pageError, setPageError] = useState<string | null>(null);
  const initialCursor = queue.data?.next_after_created_at && queue.data.next_after_memory_id ? { at: queue.data.next_after_created_at, id: queue.data.next_after_memory_id } : null;
  const items = [...(queue.data?.items ?? []), ...extra]; const hasMore = cursor !== null || (extra.length === 0 && initialCursor !== null);
  async function loadMore() {
    const next = cursor ?? initialCursor; if (!next) return;
    setLoadingMore(true); setPageError(null);
    try {
      const params = new URLSearchParams({ limit: "50", after_created_at: next.at, after_memory_id: next.id });
      const page = await apiRequest(`/v1/servicemind/memories/review-queue?${params}`, getToken, memoryQueueSchema);
      setExtra((current) => [...current, ...page.items]);
      setCursor(page.next_after_created_at && page.next_after_memory_id ? { at: page.next_after_created_at, id: page.next_after_memory_id } : null);
    } catch (cause) { setPageError(cause instanceof Error ? cause.message : "下一页读取失败"); }
    finally { setLoadingMore(false); }
  }
  function refresh() { setExtra([]); setCursor(null); void queue.mutate(); }
  return <section className="page"><PageHeader eyebrow="记忆治理" title="记忆复核" description="只显示当前审批人有权处理、仍处于隔离状态的记忆；决定绑定版本和内容摘要。" actions={<div className="queue-total"><span>当前已载入</span><strong>{items.length || (queue.isLoading ? "—" : "0")}</strong></div>} />{queue.isLoading ? <LoadingState /> : queue.error ? <ErrorState error={queue.error} retry={() => void queue.mutate()} /> : items.length ? <><div className="memory-list">{items.map((item) => <ReviewCard key={item.memory_id} item={item} onDone={refresh} />)}</div>{pageError && <p className="inline-error" role="alert">{pageError}</p>}{hasMore && <div className="load-more"><button className="button button--secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在读取" : "加载更多待审记忆"}</button></div>}</> : <EmptyState title="记忆复核队列已清空" description="没有符合当前租户、实体与组权限范围的隔离记忆。" />}</section>;
}
