export function text(value: unknown, fallback = "—") {
  const normalized = String(value ?? "").trim();
  return normalized || fallback;
}

export function statusLabel(status: unknown) {
  const labels: Record<string, string> = {
    active: "进行中",
    pending: "待处理",
    in_progress: "推进中",
    blocked: "受阻",
    running: "运行中",
    planned: "已计划",
    reading: "阅读中",
    triaged: "已筛选",
    inbox: "收件箱",
    promoted: "已晋升",
    applied: "已应用",
    processed: "已处理",
    dismissed: "已忽略",
    daily_report: "日报",
    weekly_report: "周报",
    rejected: "已拒绝",
    expired: "已过期",
    ready: "就绪",
    available: "可用",
    configured: "已配置",
    ok: "正常",
    healthy: "健康",
    pass: "通过",
    attention: "需处理",
    not_configured: "未配置",
    unavailable: "不可用",
    partial: "部分就绪",
    failed: "失败",
    interrupted: "已中断",
    stopped: "已停止",
    not_run: "尚未运行",
    demo: "演示",
  };
  return labels[text(status, "")] ?? text(status);
}
