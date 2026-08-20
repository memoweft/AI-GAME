import { describe, expect, it } from 'vitest';
import {
  isUserTakeover,
  projectProgressEvent,
} from '../../frontend/src/gateway/projection';

function project(type: string, payload: Record<string, unknown> = {}): string {
  return projectProgressEvent({ sequence: 1, type, payload }).text;
}

describe('Gateway 事件投影（§13）', () => {
  it('把已知 Runtime 事件映射为用户可读文本', () => {
    expect(project('TaskCreated', { goal: '领取奖励' })).toBe('任务已创建：领取奖励');
    expect(project('TaskCreated', {})).toBe('任务已创建');
    expect(project('StageCreated', { ordinal: 2 })).toBe('计划新增阶段 #2');
    expect(project('StageCreated', {})).toBe('计划新增阶段');
    expect(project('StageStarted', { objective: '打开活动页' })).toBe('开始阶段：打开活动页');
    expect(project('StageStarted', {})).toBe('开始执行阶段');
    expect(project('StageCompleted', { objective: '打开活动页' })).toBe('阶段完成：打开活动页');
    // Kernel 的 StageCompleted 载荷没有 objective：绝不虚构阶段名。
    expect(project('StageCompleted', { stage_id: 'st-1', status: 'completed', evidence_refs: [] })).toBe('阶段完成');
    expect(project('UserMessageReceived', { text: '先别点确认' })).toBe('收到用户消息：先别点确认');
    expect(project('ObservationReceived', {})).toBe('已获取设备画面');
    expect(project('ActionProposed', { type: 'tap', expected_outcome: '进入活动页' })).toBe('模型提出操作（tap）：进入活动页');
    expect(project('ActionProposed', { type: 'tap' })).toBe('模型提出操作（tap）');
    expect(project('ActionProposed', {})).toBe('模型提出下一步操作');
    expect(project('ActionExecuted', { accepted: true })).toBe('已执行操作');
    expect(project('ActionExecuted', { accepted: false })).toBe('操作执行被设备拒绝');
    expect(project('ActionVerified', { verdict: 'SUCCESS' })).toBe('操作结果验证成功');
    expect(project('ActionVerified', { verdict: 'FAIL' })).toBe('操作结果验证失败');
    expect(project('ActionVerified', { verdict: 'UNCERTAIN' })).toBe('操作结果验证：UNCERTAIN');
    expect(project('ActionVerified', {})).toBe('操作结果已验证');
    expect(project('FactAdded', { key: 'reward_amount' })).toBe('记录事实：reward_amount');
    expect(project('CheckpointCreated', { reason: '阶段切换' })).toBe('创建检查点（阶段切换）');
    expect(project('TaskPaused', {})).toBe('任务已暂停');
    expect(project('TaskResumed', {})).toBe('任务已恢复');
    expect(project('TaskCancelled', {})).toBe('任务已取消');
    expect(project('UserTakeover', {})).toBe('用户接管中，任务已暂停');
  });

  it('未知事件类型诚实降级，不制造含义', () => {
    expect(project('MysteryEvent', {})).toBe('任务事件：MysteryEvent');
  });

  it('长载荷字段截断到 120 字符并加省略号', () => {
    const longGoal = '长'.repeat(200);
    const text = project('TaskCreated', { goal: longGoal });
    expect(text).toBe(`任务已创建：${'长'.repeat(119)}…`);
    expect(text.endsWith('…')).toBe(true);
  });

  it('透传 sequence/type/payload/createdAt', () => {
    const entry = projectProgressEvent({
      sequence: 7,
      type: 'TaskPaused',
      payload: {},
      createdAt: '2026-08-20T00:00:00Z',
    });
    expect(entry).toEqual({
      sequence: 7,
      type: 'TaskPaused',
      payload: {},
      createdAt: '2026-08-20T00:00:00Z',
      text: '任务已暂停',
    });
  });

  it('仅当最新暂停族事件是 UserTakeover 时才判定用户接管', () => {
    const pause = (sequence: number) => ({ type: 'TaskPaused', sequence });
    const resume = (sequence: number) => ({ type: 'TaskResumed', sequence });
    const takeover = (sequence: number) => ({ type: 'UserTakeover', sequence });

    expect(isUserTakeover([])).toBe(false);
    expect(isUserTakeover([pause(3)])).toBe(false);
    expect(isUserTakeover([takeover(5)])).toBe(true);
    expect(isUserTakeover([takeover(1), resume(2)])).toBe(false);
    expect(isUserTakeover([resume(1), takeover(2), pause(3)])).toBe(false);
    // 乱序到达：按 sequence 取最新，而不是按数组顺序。
    expect(isUserTakeover([takeover(2), pause(1)])).toBe(true);
    // 非暂停族事件不参与判定。
    expect(isUserTakeover([{ type: 'ActionExecuted', sequence: 99 }, takeover(3)])).toBe(true);
  });
});
