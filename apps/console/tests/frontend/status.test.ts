import { describe, expect, it } from 'vitest';
import {
  approvalStatusMeta,
  runStatusMeta,
  runtimeStatusMeta,
  targetStatusMeta,
  workflowStatusMeta,
} from '../../frontend/src/status';

describe('中文状态映射', () => {
  it('区分传统任务队列未接入与真正的 GUI 执行器状态', () => {
    const saved = runStatusMeta('queued', [
      { code: 'workflow_executor_not_connected', message: '传统任务队列尚未接入' },
    ]);

    expect(saved.label).toBe('已保存 · 等待任务队列');
    expect(saved.detail).toContain('本地 GUI 通道不受影响');
    expect(saved.tone).toBe('warning');
    expect(runStatusMeta('running').label).toBe('运行中');
    expect(runStatusMeta('completed').label).toBe('已完成');
  });

  it('为设备、审批、工作流和运行组件提供文字与图标语义', () => {
    expect(targetStatusMeta('unauthorized').label).toBe('待授权');
    expect(approvalStatusMeta('withdrawn')).toMatchObject({
      label: '已撤回',
      tone: 'neutral',
    });
    expect(workflowStatusMeta('future')).toMatchObject({
      label: '待接入',
      icon: 'clock',
    });
    expect(workflowStatusMeta('external')).toMatchObject({
      label: '已接入',
      detail: expect.stringContaining('独立工作台'),
      tone: 'success',
    });
    expect(runtimeStatusMeta('stopped')).toMatchObject({
      label: '已停止',
      detail: '组件已配置，但当前未运行',
    });
  });

  it('对后端新增的未知状态保持诚实', () => {
    expect(runStatusMeta('brand_new_status').label).toBe('未知状态');
    expect(targetStatusMeta('brand_new_status').detail).toContain('暂不认识');
  });
});
