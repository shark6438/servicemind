const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date);
}

export function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function formatPercent(value: number | null, digits = 1): string {
  return value === null ? "未测" : `${(value * 100).toFixed(digits)}%`;
}

export function readableLabel(value: string): string {
  const labels: Record<string, string> = {
    analysis: "分析",
    action: "动作生成",
    approved: "已批准",
    approval: "审批",
    critical: "严重",
    cancelled: "已取消",
    completed: "已完成",
    continue: "继续执行",
    context: "上下文整理",
    data: "工单数据",
    deterministic_fallback: "确定性回退",
    episodic: "情节记忆",
    escalate: "转人工处理",
    failed: "失败",
    general: "通用",
    high: "高",
    incident: "事件",
    knowledge: "知识检索",
    llm: "语言模型",
    low: "低",
    medium: "中",
    network: "网络",
    pending: "等待执行",
    planning: "任务规划",
    passed: "已通过",
    procedural: "程序记忆",
    reject: "拒绝",
    rejected: "已拒绝",
    retrieve_more: "补充检索",
    research: "资料检索",
    review: "复核",
    reviewer: "复核",
    running: "执行中",
    semantic: "语义记忆",
    stop: "停止执行",
    supervisor: "调度",
    succeeded: "已完成",
    vpn: "虚拟专用网络",
    working: "工作记忆",
    waiting_approval: "等待审批",
    waiting_review: "等待复核",
    run_created: "运行已创建",
    run_started: "运行已开始",
    run_completed: "运行已完成",
    run_failed: "运行失败",
    run_cancelled: "运行已取消",
    approval_requested: "已发起审批",
    approval_decided: "审批已处理",
    review_requested: "已发起复核",
    review_resolved: "复核已处理",
    action_approved: "动作已批准",
    glpi_followup_created: "工单跟进已创建",
    memory_expired: "记忆已过期",
    memory_revoked: "记忆已撤销",
    memory_superseded: "记忆已被新版替代",
    run_recovered: "运行已恢复",
    webhook_accepted: "外部事件已接收",
    append_ticket_followup: "追加工单跟进",
    retrieve_knowledge: "检索知识",
    retrieve_more_knowledge: "补充检索知识",
    get_ticket: "读取工单",
    retrieve_more_data: "补充读取工单数据",
    analyze_ticket: "分析工单",
    review_analysis: "复核分析",
    propose_followup: "生成跟进建议",
    glpi_followup: "工单跟进",
    webhook: "外部事件",
    memory: "记忆",
    run: "运行",
  };
  if (labels[value]) return labels[value];
  return value
    .split(/[._-]+/)
    .map((part) => labels[part] ?? part)
    .join(" · ");
}

export function safeJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
