"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { apiRequest } from "@/lib/api";
import { runDetailSchema } from "@/lib/contracts";
import { useAuth } from "@/providers/auth-provider";

export function CreateRunForm() {
  const { roles, getToken } = useAuth();
  const router = useRouter();
  const [ticketId, setTicketId] = useState("");
  const [goal, setGoal] = useState("");
  const [requestWrite, setRequestWrite] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submission = useRef<{ body: string; key: string } | null>(null);
  const submitting = useRef(false);
  const canCreate = roles.has("analyst");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canCreate || submitting.current) return;
    submitting.current = true;
    setError(null);
    setPending(true);
    let navigating = false;
    try {
      const body = JSON.stringify({ ticket_id: Number(ticketId), goal, request_write: requestWrite });
      // A response can be lost after the server commits. Retrying the unchanged
      // intent must retain its key instead of creating another paid Agent run.
      if (submission.current?.body !== body) submission.current = { body, key: crypto.randomUUID() };
      const run = await apiRequest("/v1/servicemind/runs", getToken, runDetailSchema, {
        method: "POST",
        headers: { "Idempotency-Key": submission.current.key },
        body,
      });
      router.push(`/runs/${run.id}`);
      navigating = true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "运行创建失败");
    } finally {
      // Keep the form locked until navigation unmounts it. A second click during
      // route loading must not turn a successful submission into a new intent.
      if (!navigating) { submitting.current = false; setPending(false); }
    }
  }

  function submitWithShortcut(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !pending) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <form className="conversation-composer" onSubmit={(event) => void submit(event)}>
      <header><h2>向运维智能体提问</h2><p>描述需要处理的问题。提交后将创建一条可追踪的智能体运行。</p></header>
      <label className="conversation-prompt"><span>你的问题</span><textarea name="goal" autoComplete="off" required minLength={3} maxLength={2000} rows={5} value={goal} onChange={(event) => setGoal(event.target.value)} onKeyDown={submitWithShortcut} placeholder="例如：分析该工单的 VPN 连接失败原因，给出已验证的处置建议和下一步。" /></label>
      <div className="conversation-options">
        <label>关联 GLPI 工单<input name="ticket_id" type="number" inputMode="numeric" autoComplete="off" min="1" required value={ticketId} onChange={(event) => setTicketId(event.target.value)} placeholder="例如 1042" /></label>
        <label className="switch-line"><input name="request_write" type="checkbox" checked={requestWrite} onChange={(event) => setRequestWrite(event.target.checked)} /><span><strong>允许生成写操作建议</strong><small>实际执行仍需策略校验和人工审批。</small></span></label>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <footer><p>仅处理当前租户中你有权限访问的工单。按 Ctrl 或 ⌘ 加 Enter 可直接发送。</p><button className="button button--primary" disabled={!canCreate || pending}>{pending ? "正在发送" : canCreate ? "发送给智能体" : "当前角色无权发起运行"}</button></footer>
    </form>
  );
}
