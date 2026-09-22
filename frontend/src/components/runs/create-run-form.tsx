"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
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
  const canCreate = roles.has("analyst");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      const run = await apiRequest("/v1/servicemind/runs", getToken, runDetailSchema, {
        method: "POST",
        body: JSON.stringify({ ticket_id: Number(ticketId), goal, request_write: requestWrite }),
      });
      router.push(`/runs/${run.id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "运行创建失败");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="dispatch-form" onSubmit={(event) => void submit(event)}>
      <header><h2>新建工单调查</h2><p>系统将按证据检索、分析、复核和审批流程推进。</p></header>
      <label>GLPI 工单编号<input name="ticket_id" type="number" inputMode="numeric" autoComplete="off" min="1" required value={ticketId} onChange={(event) => setTicketId(event.target.value)} placeholder="例如 1042" /></label>
      <label>调查目标<textarea name="goal" autoComplete="off" required minLength={3} maxLength={2000} rows={4} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="说明需要确认的问题、期望输出和边界条件" /></label>
      <label className="switch-line"><input name="request_write" type="checkbox" checked={requestWrite} onChange={(event) => setRequestWrite(event.target.checked)} /><span><strong>允许生成写操作建议</strong><small>实际执行仍需策略校验和人工审批。</small></span></label>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="form-actions"><button className="button button--primary" disabled={!canCreate || pending}>{pending ? "正在创建调查" : canCreate ? "启动调查" : "当前角色无权创建"}</button></div>
    </form>
  );
}
