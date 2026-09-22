import type { RunStatus } from "@/lib/contracts";

export const runStatusLabels: Record<RunStatus, string> = {
  pending: "等待执行",
  running: "执行中",
  waiting_approval: "等待审批",
  waiting_review: "等待复核",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

const gateLabels: Record<string, string> = {
  PASS: "已通过",
  FAIL: "未通过",
  QUALITY_EXCEPTION_ACCEPTED: "附质量例外放行",
  DOMAIN_QUALITY_NOT_CERTIFIED: "领域质量尚未认证",
  PASS_ENGINEERING_WITH_NO_PRODUCTION_MEMORY_SAMPLE: "工程验证通过，缺少生产记忆样本",
  PASS_ENGINEERING_SUPPORTED_SURFACE: "支持范围内工程验证通过",
  NOT_EVALUATED: "尚未评估",
  NO_DATA: "暂无数据",
};

export function StatusBadge({ status }: { status: RunStatus | string }) {
  return <span className={`status status--${status}`}><span aria-hidden="true" className="status-dot" />{runStatusLabels[status as RunStatus] ?? "状态未知"}</span>;
}
export function GateBadge({ status }: { status: string }) {
  const normalized = status.toUpperCase();
  return <span className={`gate gate--${normalized.toLowerCase()}`}>{gateLabels[normalized] ?? "状态待确认"}</span>;
}
