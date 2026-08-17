import type {
  ApprovalStatus,
  Blocker,
  EventLevel,
  RunStatus,
  RuntimeStatus,
  TargetStatus,
  WorkflowStatus,
} from './types';

export type StatusTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';
export type StatusIcon = 'check' | 'clock' | 'play' | 'pause' | 'warning' | 'error' | 'help';

export interface StatusMeta {
  label: string;
  detail: string;
  tone: StatusTone;
  icon: StatusIcon;
}

const RUN_STATUS: Record<RunStatus, StatusMeta> = {
  draft: { label: '草稿', detail: '尚未提交执行', tone: 'neutral', icon: 'clock' },
  queued: { label: '排队中', detail: '等待可用执行资源', tone: 'info', icon: 'clock' },
  running: { label: '运行中', detail: '执行器正在处理任务', tone: 'info', icon: 'play' },
  awaiting_approval: {
    label: '等待审批',
    detail: '需要人工确认后才能继续',
    tone: 'warning',
    icon: 'warning',
  },
  paused: { label: '已暂停', detail: '任务不会继续执行', tone: 'warning', icon: 'pause' },
  completed: { label: '已完成', detail: '任务已完成', tone: 'success', icon: 'check' },
  failed: { label: '执行失败', detail: '执行过程遇到错误', tone: 'danger', icon: 'error' },
  cancelled: { label: '已取消', detail: '任务已由用户取消', tone: 'neutral', icon: 'error' },
  blocked: { label: '已阻塞', detail: '存在必须先处理的问题', tone: 'danger', icon: 'warning' },
};

const APPROVAL_STATUS: Record<ApprovalStatus, StatusMeta> = {
  pending: { label: '待审批', detail: '等待人工决定', tone: 'warning', icon: 'clock' },
  approved: { label: '已批准', detail: '审批已通过', tone: 'success', icon: 'check' },
  rejected: { label: '已拒绝', detail: '审批未通过', tone: 'danger', icon: 'error' },
  withdrawn: { label: '已撤回', detail: '关联任务已取消', tone: 'neutral', icon: 'error' },
};

const TARGET_STATUS: Record<TargetStatus, StatusMeta> = {
  ready: { label: '可用', detail: '目标设备已连接', tone: 'success', icon: 'check' },
  offline: { label: '离线', detail: '当前无法连接目标设备', tone: 'neutral', icon: 'error' },
  unauthorized: {
    label: '待授权',
    detail: '请先在目标设备上完成授权',
    tone: 'warning',
    icon: 'warning',
  },
  unknown: { label: '状态未知', detail: '尚未确认设备连接状态', tone: 'neutral', icon: 'help' },
};

const WORKFLOW_STATUS: Record<WorkflowStatus, StatusMeta> = {
  available: { label: '可用', detail: '可以用于创建任务', tone: 'success', icon: 'check' },
  external: { label: '已接入', detail: '请在独立工作台中使用此本地应用', tone: 'success', icon: 'check' },
  future: { label: '待接入', detail: '此工作流将在后续版本接入', tone: 'neutral', icon: 'clock' },
  disabled: { label: '已停用', detail: '此工作流当前不可创建任务', tone: 'warning', icon: 'pause' },
  not_configured: { label: '未配置', detail: '此工作流缺少必要配置', tone: 'warning', icon: 'warning' },
};

const RUNTIME_STATUS: Record<RuntimeStatus, StatusMeta> = {
  ready: { label: '已就绪', detail: '服务可以接收请求', tone: 'success', icon: 'check' },
  starting: { label: '启动中', detail: '服务正在准备', tone: 'info', icon: 'clock' },
  paused: { label: '已暂停', detail: '服务当前不会继续执行', tone: 'warning', icon: 'pause' },
  stopped: { label: '已停止', detail: '组件已配置，但当前未运行', tone: 'neutral', icon: 'pause' },
  not_configured: {
    label: '未接入',
    detail: '此组件尚未配置',
    tone: 'neutral',
    icon: 'help',
  },
  unavailable: { label: '不可用', detail: '当前无法连接此组件', tone: 'danger', icon: 'error' },
  error: { label: '异常', detail: '组件报告错误', tone: 'danger', icon: 'error' },
  unknown: { label: '状态未知', detail: '尚未取得组件状态', tone: 'neutral', icon: 'help' },
};

const EVENT_LEVEL: Record<EventLevel, StatusMeta> = {
  info: { label: '信息', detail: '普通运行信息', tone: 'info', icon: 'help' },
  success: { label: '成功', detail: '操作已完成', tone: 'success', icon: 'check' },
  warning: { label: '注意', detail: '需要留意的情况', tone: 'warning', icon: 'warning' },
  error: { label: '错误', detail: '操作遇到错误', tone: 'danger', icon: 'error' },
  debug: { label: '调试', detail: '调试信息', tone: 'neutral', icon: 'help' },
};

const UNKNOWN: StatusMeta = {
  label: '未知状态',
  detail: '服务返回了暂不认识的状态',
  tone: 'neutral',
  icon: 'help',
};

export function runStatusMeta(status: string, blockers: Blocker[] = []): StatusMeta {
  const waitsForWorkflowQueue = blockers.some((blocker) =>
    ['workflow_executor_not_connected', 'executor_not_configured'].includes(blocker.code),
  );
  if (status === 'queued' && waitsForWorkflowQueue) {
    return {
      label: '已保存 · 等待任务队列',
      detail: '传统任务不会自动运行；对话执行中的本地 GUI 通道不受影响。',
      tone: 'warning',
      icon: 'pause',
    };
  }
  return RUN_STATUS[status as RunStatus] ?? UNKNOWN;
}

export function approvalStatusMeta(status: string): StatusMeta {
  return APPROVAL_STATUS[status as ApprovalStatus] ?? UNKNOWN;
}

export function targetStatusMeta(status: string): StatusMeta {
  return TARGET_STATUS[status as TargetStatus] ?? UNKNOWN;
}

export function workflowStatusMeta(status: string): StatusMeta {
  return WORKFLOW_STATUS[status as WorkflowStatus] ?? UNKNOWN;
}

export function runtimeStatusMeta(status?: string | null): StatusMeta {
  return RUNTIME_STATUS[(status || 'unknown') as RuntimeStatus] ?? UNKNOWN;
}

export function eventLevelMeta(level?: string | null): StatusMeta {
  return EVENT_LEVEL[(level || 'info') as EventLevel] ?? EVENT_LEVEL.info;
}
