import { CircleCheck, CircleDashed, CirclePause, CircleX, ShieldAlert } from "lucide-react";
import type { RunStatus } from "@/lib/contracts";

const labels: Record<RunStatus, string> = { pending: "待执行", running: "运行中", waiting_approval: "待审批", waiting_review: "待复核", succeeded: "已完成", failed: "失败", cancelled: "已取消" };
export function StatusBadge({ status }: { status: RunStatus | string }) {
  const Icon = status === "succeeded" ? CircleCheck : status === "failed" || status === "cancelled" ? CircleX : status === "waiting_approval" || status === "waiting_review" ? ShieldAlert : status === "running" ? CircleDashed : CirclePause;
  return <span className={`status status--${status}`}><Icon aria-hidden="true" size={13} />{labels[status as RunStatus] ?? status}</span>;
}
export function GateBadge({ status }: { status: string }) { const normalized = status.toUpperCase(); return <span className={`gate gate--${normalized.toLowerCase()}`}>{normalized}</span>; }
