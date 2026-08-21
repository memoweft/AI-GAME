import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GoalWorkspace } from '../../frontend/src/components/GoalWorkspace';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function goal(overrides: Record<string, unknown> = {}) {
  return {
    id: 'goal-1',
    original_goal: '打开设置查看电池后回桌面',
    goal_specification: {
      revision: 1,
      original_goal: '打开设置查看电池后回桌面',
      normalized_intent: { classification: 'pending_u3' },
      success_criteria: [],
    },
    execution_status: 'WAITING_CONFIGURATION',
    control_state: 'AUTOMATED',
    active_stage: null,
    binding: {
      kind: 'mobile_task_compat', state: 'WAITING_CONFIGURATION',
      task_id: null, target_id: null, completion_gate: 'pending_u3',
    },
    environment_state: {
      state: 'WAITING_CONFIGURATION',
      facts: [{ capability: 'android.discovery', state: 'WAITING_CONFIGURATION', detail: '未找到设备' }],
      selected_target_id: null,
      target_options: [],
    },
    waiting_reason: { code: 'android_target_not_ready', message: '请启动或连接设备后重试。' },
    error: null,
    result_summary: null,
    completion_assessment: null,
    completion_history: [],
    verified_facts: [],
    uncompleted_items: [],
    created_at: '2026-08-20T00:00:00Z',
    updated_at: '2026-08-20T00:00:01Z',
    terminal_at: null,
    ...overrides,
  };
}

describe('U2 统一目标入口', () => {
  it('创建请求只发送目标和幂等键，不发送设备、模型或运行时字段', async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | null = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v2/goals?limit=100')) return json({ items: [], count: 0 });
      if (url.endsWith('/api/v2/goals') && init?.method === 'POST') {
        requestBody = JSON.parse(String(init.body));
        return json(goal());
      }
      if (url.endsWith('/api/v2/goals/goal-1')) return json(goal());
      throw new Error(`Unexpected ${url}`);
    }));

    render(<GoalWorkspace />);
    await screen.findByText('还没有目标');
    await user.type(screen.getByRole('textbox', { name: '目标' }), '打开设置查看电池后回桌面');
    await user.click(screen.getByRole('button', { name: '开始执行' }));

    await screen.findByText('等待环境就绪');
    expect(Object.keys(requestBody ?? {}).sort()).toEqual(['goal', 'idempotency_key']);
    expect(document.body).not.toHaveTextContent(/skill_id|profile|runtime mode|device serial/i);
  });

  it('等待门禁使用一句人话说明并可在同一 GoalRun 上重试', async () => {
    const user = userEvent.setup();
    const ready = goal({
      execution_status: 'RUNNING',
      waiting_reason: null,
      binding: {
        kind: 'mobile_task_compat', state: 'BOUND', task_id: 'mobile-1',
        target_id: 'adb:one', completion_gate: 'pending_u3',
      },
      environment_state: {
        state: 'READY', facts: [], selected_target_id: 'adb:one', target_options: [],
      },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v2/goals?limit=100')) return json({ items: [goal()], count: 1 });
      if (url.endsWith('/preflight/retry') && init?.method === 'POST') return json(ready);
      if (url.endsWith('/api/v2/goals/goal-1')) return json(ready);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<GoalWorkspace />);
    expect((await screen.findAllByText('请启动或连接设备后重试。')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: '重新检查' }));
    expect((await screen.findAllByText('正在操作')).length).toBeGreaterThan(0);
    expect(screen.getByText('设备 one')).toBeInTheDocument();
  });

  it('多个目标只显示普通设备选择门，不自动猜测', async () => {
    const user = userEvent.setup();
    const waiting = goal({
      execution_status: 'WAITING_EXTERNAL',
      waiting_reason: { code: 'TARGET_SELECTION_REQUIRED', message: '发现多个可用设备，请选择这次目标使用哪一台。' },
      environment_state: {
        state: 'WAITING_EXTERNAL', facts: [], selected_target_id: null,
        target_options: [
          { target_id: 'adb:one', name: '手机', connection: 'one' },
          { target_id: 'adb:two', name: '平板', connection: 'two' },
        ],
      },
    });
    const selected = goal({
      execution_status: 'RUNNING', waiting_reason: null,
      binding: { kind: 'mobile_task_compat', state: 'BOUND', task_id: 'mobile-1', target_id: 'adb:two', completion_gate: 'pending_u3' },
      environment_state: { state: 'READY', facts: [], selected_target_id: 'adb:two', target_options: [] },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v2/goals?limit=100')) return json({ items: [waiting], count: 1 });
      if (url.endsWith('/preflight/selection') && init?.method === 'POST') return json(selected);
      if (url.endsWith('/api/v2/goals/goal-1')) return json(selected);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<GoalWorkspace />);
    expect(await screen.findByText('请选择这次使用的设备')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /平板/ }));
    expect(await screen.findByText('设备 two')).toBeInTheDocument();
  });

  it('刷新后从持久列表恢复同一个活动 GoalRun，并诚实显示候选完成', async () => {
    window.localStorage.setItem('ai-game.active-goal-id', 'goal-2');
    const candidate = goal({
      id: 'goal-2', original_goal: '查看电池并回桌面',
      execution_status: 'CANDIDATE_COMPLETE',
      waiting_reason: null,
      result_summary: '兼容执行器已完成其生成的计划；原始目标尚待独立完成验证。',
      uncompleted_items: ['原始目标尚未经过独立 Goal Completion Verifier 验证'],
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/v2/goals?limit=100')) {
        return json({ items: [goal({ id: 'goal-1' }), candidate], count: 2 });
      }
      throw new Error(`Unexpected ${String(input)}`);
    }));

    render(<GoalWorkspace />);
    expect(await screen.findByText('执行计划已结束')).toBeInTheDocument();
    expect(screen.getByText('原始目标尚未经过独立 Goal Completion Verifier 验证')).toBeInTheDocument();
    expect(window.localStorage.getItem('ai-game.active-goal-id')).toBe('goal-2');
  });

  it('完成后显示独立验证状态和已验证事实', async () => {
    const completed = goal({
      execution_status: 'COMPLETED',
      waiting_reason: null,
      result_summary: '电量84%，正在充电，已返回桌面。',
      binding: {
        kind: 'mobile_task_compat', state: 'BOUND', task_id: 'mobile-1',
        target_id: 'adb:one', completion_gate: 'verified',
      },
      completion_assessment: {
        revision: 1, specification_revision: 2, source_task_id: 'mobile-1',
        verdict: 'verified', criteria: [], verified_facts: ['电量84%', '已返回桌面'],
        result_summary: '电量84%，正在充电，已返回桌面。', created_at: '2026-08-21T00:00:00Z',
      },
      completion_history: [],
      verified_facts: ['电量84%', '已返回桌面'],
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/v2/goals?limit=100')) {
        return json({ items: [completed], count: 1 });
      }
      throw new Error(`Unexpected ${String(input)}`);
    }));

    render(<GoalWorkspace />);
    expect(await screen.findByText('已独立验证')).toBeInTheDocument();
    expect(screen.getByText('已验证结果')).toBeInTheDocument();
    expect(screen.getByText('电量84%')).toBeInTheDocument();
    expect(screen.getByText('已返回桌面')).toBeInTheDocument();
  });
});
