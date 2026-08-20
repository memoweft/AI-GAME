// ---------------------------------------------------------------------------
// Gateway event → user-readable projection (Phase 6, contract §13).
//
// Contract invariants:
//  * A projection line is derived ONLY from the fields present in the actual
//    Runtime event payload — it must never fabricate a Stage, Fact, or
//    completion state that does not exist in the Runtime.
//  * Unknown event types degrade to a neutral, honest line
//    (`任务事件：<type>`) instead of inventing meaning.
//  * WeChat/Hermes consume these user-readable lines, never raw clicks or ADB
//    logs — so the text stays at the "what happened" level.
// ---------------------------------------------------------------------------

export interface GatewayProgressInput {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  /** ISO timestamp when known (backfilled events); stream frames omit it. */
  createdAt?: string;
}

export interface GatewayProgressEntry extends GatewayProgressInput {
  text: string;
}

const MAX_TEXT_LENGTH = 120;

function stringPayload(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function truncate(text: string): string {
  return text.length > MAX_TEXT_LENGTH ? `${text.slice(0, MAX_TEXT_LENGTH - 1)}…` : text;
}

/**
 * Render one Runtime event as a user-readable progress line.
 *
 * Every branch reads only payload fields the kernel actually emits for that
 * event type (verified against runtime_kernel/kernel.py); when a field is
 * absent the line falls back to a generic phrasing rather than inventing one.
 */
export function projectProgressEvent(input: GatewayProgressInput): GatewayProgressEntry {
  return { ...input, text: describeEvent(input.type, input.payload) };
}

function describeEvent(type: string, payload: Record<string, unknown>): string {
  switch (type) {
    case 'TaskCreated': {
      const goal = stringPayload(payload, 'goal');
      return goal ? `任务已创建：${truncate(goal)}` : '任务已创建';
    }
    case 'StageCreated': {
      const ordinal = payload['ordinal'];
      return typeof ordinal === 'number' ? `计划新增阶段 #${ordinal}` : '计划新增阶段';
    }
    case 'StageStarted': {
      const objective = stringPayload(payload, 'objective');
      return objective ? `开始阶段：${truncate(objective)}` : '开始执行阶段';
    }
    case 'StageCompleted': {
      const objective = stringPayload(payload, 'objective');
      return objective ? `阶段完成：${truncate(objective)}` : '阶段完成';
    }
    case 'UserMessageReceived': {
      const text = stringPayload(payload, 'text');
      return text ? `收到用户消息：${truncate(text)}` : '收到用户消息';
    }
    case 'ObservationReceived': {
      return '已获取设备画面';
    }
    case 'ActionProposed': {
      const actionType = stringPayload(payload, 'type');
      const expected = stringPayload(payload, 'expected_outcome');
      if (actionType && expected) {
        return `模型提出操作（${actionType}）：${truncate(expected)}`;
      }
      if (actionType) {
        return `模型提出操作（${actionType}）`;
      }
      return '模型提出下一步操作';
    }
    case 'ActionExecuted': {
      const accepted = payload['accepted'];
      if (accepted === false) {
        return '操作执行被设备拒绝';
      }
      return '已执行操作';
    }
    case 'ActionVerified': {
      const verdict = stringPayload(payload, 'verdict');
      if (verdict === 'SUCCESS') {
        return '操作结果验证成功';
      }
      if (verdict === 'FAIL') {
        return '操作结果验证失败';
      }
      if (verdict) {
        return `操作结果验证：${verdict}`;
      }
      return '操作结果已验证';
    }
    case 'FactAdded': {
      const key = stringPayload(payload, 'key');
      return key ? `记录事实：${truncate(key)}` : '记录事实';
    }
    case 'CheckpointCreated': {
      const reason = stringPayload(payload, 'reason');
      return reason ? `创建检查点（${truncate(reason)}）` : '创建检查点';
    }
    case 'TaskPaused': {
      return '任务已暂停';
    }
    case 'TaskResumed': {
      return '任务已恢复';
    }
    case 'TaskCancelled': {
      return '任务已取消';
    }
    case 'UserTakeover': {
      return '用户接管中，任务已暂停';
    }
    default:
      // Unknown type: stay honest, never invent meaning.
      return `任务事件：${type}`;
  }
}

const PAUSE_FAMILY_EVENTS = new Set(['TaskPaused', 'TaskResumed', 'UserTakeover']);

/**
 * Decide whether the task is currently in user takeover.
 *
 * The kernel maps BOTH pause and takeover to PAUSED, so the projection
 * distinguishes them by the most recent pause-family event: takeover is in
 * effect only when the latest pause/resume/takeover event is a UserTakeover.
 * Never fabricates a takeover state from anything else.
 */
export function isUserTakeover(
  events: ReadonlyArray<{ type: string; sequence: number }>,
): boolean {
  let latest: { type: string; sequence: number } | null = null;
  for (const event of events) {
    if (!PAUSE_FAMILY_EVENTS.has(event.type)) {
      continue;
    }
    if (latest === null || event.sequence > latest.sequence) {
      latest = event;
    }
  }
  return latest !== null && latest.type === 'UserTakeover';
}
