"use client";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiRequest } from "@/lib/api";
import { runDetailSchema } from "@/lib/contracts";
import { useAuth } from "@/providers/auth-provider";

export function CreateRunForm() {
  const { roles, getToken } = useAuth(); const router = useRouter();
  const [ticketId, setTicketId] = useState(""); const [goal, setGoal] = useState("");
  const [requestWrite, setRequestWrite] = useState(false); const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canCreate = roles.has("analyst");
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(null); setPending(true);
    try {
      const run = await apiRequest("/v1/servicemind/runs", getToken, runDetailSchema, { method: "POST", body: JSON.stringify({ ticket_id: Number(ticketId), goal, request_write: requestWrite }) });
      router.push(`/runs/${run.id}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "运行创建失败"); }
    finally { setPending(false); }
  }
  return <form className="dispatch-form" onSubmit={(event) => void submit(event)}><div className="dispatch-heading"><div><p className="section-kicker">新建受控运行</p><h2>从一张工单开始调查</h2></div><ShieldCheck aria-hidden="true" /></div><label>GLPI 工单编号<input name="ticket_id" type="number" inputMode="numeric" autoComplete="off" min="1" required value={ticketId} onChange={(e) => setTicketId(e.target.value)} placeholder="例如 1042…" /></label><label>调查目标<textarea name="goal" autoComplete="off" required minLength={3} maxLength={2000} rows={5} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="描述要确认的问题、期望输出和边界条件…" /></label><label className="switch-line"><input name="request_write" type="checkbox" checked={requestWrite} onChange={(e) => setRequestWrite(e.target.checked)} /><span><strong>允许提出写操作意图</strong><small>任何高风险执行仍需人工审批，勾选不会绕过门禁。</small></span></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="button button--primary button--wide" disabled={!canCreate || pending}>{pending ? "Agent 正在调查…" : canCreate ? <>启动调查 <ArrowRight aria-hidden="true" /></> : "需要 analyst 角色"}</button></form>;
}
