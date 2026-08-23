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
    resume_execution_status: null,
    active_stage: null,
    binding: {
      kind: 'mobile_task_compat', state: 'WAITING_CONFIGURATION',
      task_id: null, target_id: null, completion_gate: 'pending_u3',
    },
    binding_plan: null,
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
    notifications: [],
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

describe('U8 长期目标投影', () => {
  it('自动事件等待不冒充人工门禁，并显示候选通知和统一控制', async () => {
    const user = userEvent.setup();
    let controlAction: string | null = null;
    const longLived = goal({
      original_goal: '通过已显式绑定的专用适配器持续处理候选，直到我停止',
      execution_status: 'WAITING_EXTERNAL',
      waiting_reason: {
        code: 'TIME_OR_INBOUND_EVENT',
        message: '长期目标正在等待下一次定时唤醒或授权入站事件。',
      },
      binding: {
        kind: 'application_runtime', state: 'BOUND', task_id: 'application-1',
        target_id: 'owner-binding:v1:test', completion_gate: 'continuous_external_outcome',
      },
      binding_plan: {
        revision: 1,
        route_kind: 'explicit_external_application',
        binding_kind: 'application_runtime',
        capability_ids: ['long_lived.wait', 'application_profile.soul-reply-v1'],
        owner_kind: 'external_owner',
        owner_binding_ref: 'owner-binding:v1:test',
        profile_id: 'soul-reply-v1',
        classification: 'long_lived_application_goal',
        rationale: 'explicit specialized adapter selected for this frozen plan',
        created_at: '2026-08-23T08:00:00Z',
      },
      notifications: [{
        id: 'candidate-1', kind: 'candidate',
        summary: '发现一个可继续了解的候选；目标继续运行。',
        evidence_refs: ['candidate-evidence-1'], source_event_sequence: 3,
        created_at: '2026-08-23T08:00:00Z',
      }],
      environment_state: {
        state: 'READY', facts: [], selected_target_id: 'owner-binding:v1:test', target_options: [],
      },
    });
    const paused = goal({
      ...longLived,
      execution_status: 'RUNNING',
      control_state: 'PAUSED',
      waiting_reason: null,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v2/goals?limit=100')) {
        return json({ items: [longLived], count: 1 });
      }
      if (url.endsWith('/controls') && init?.method === 'POST') {
        controlAction = JSON.parse(String(init.body)).action;
        return json(paused);
      }
      if (url.endsWith('/api/v2/goals/goal-1')) return json(longLived);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<GoalWorkspace />);
    expect(await screen.findByText('等待下一次事件')).toBeInTheDocument();
    expect(screen.getByText('正在等待事件，不需要你操作')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新检查' })).not.toBeInTheDocument();
    expect(screen.getByText(/发现一个可继续了解的候选/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '暂停' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接管' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '暂停' }));
    await waitFor(() => expect(controlAction).toBe('pause'));
    expect(await screen.findByRole('button', { name: '继续' })).toBeInTheDocument();
  });

  it('内部长期移动组合未就绪时不冒充 owner 或人工门禁且仍可停止', async () => {
    const user = userEvent.setup();
    let controlAction: string | null = null;
    const unbound = goal({
      original_goal: '持续认识适合长期相处的人，直到我停止',
      execution_status: 'WAITING_CONFIGURATION',
      waiting_reason: {
        code: 'long_lived_mobile_runtime_not_composed',
        message: '长期移动目标已冻结通用能力计划，但内部长期运行和设备周期尚未组合。',
      },
      binding: {
        kind: 'long_lived_mobile_composition', state: 'WAITING_CONFIGURATION', task_id: null,
        target_id: null, completion_gate: 'continuous_mobile_goal_pending_runtime',
      },
      binding_plan: {
        revision: 1,
        route_kind: 'long_lived_mobile_application',
        binding_kind: 'long_lived_mobile_composition',
        capability_ids: ['long_lived.wait', 'android.observe', 'android.action', 'local.goal_verification', 'experience.record'],
        owner_kind: null,
        owner_binding_ref: null,
        profile_id: null,
        classification: 'long_lived_application_goal',
        rationale: 'long-lived mobile goal requires bounded RuntimeKernel cycles',
        created_at: '2026-08-23T08:00:00Z',
      },
      environment_state: {
        state: 'WAITING_CONFIGURATION',
        facts: [{
          capability: 'long_lived.mobile_application', state: 'WAITING_CONFIGURATION',
          detail: 'ApplicationRuntime 与 RuntimeKernel 尚未组合。',
        }],
        selected_target_id: null,
        target_options: [],
      },
    });
    const cancelled = goal({
      ...unbound,
      execution_status: 'CANCELLED',
      waiting_reason: null,
      terminal_at: '2026-08-23T08:00:02Z',
      binding: {
        kind: 'long_lived_mobile_composition', state: 'CANCELLED', task_id: null,
        target_id: null, completion_gate: 'continuous_mobile_goal_pending_runtime',
      },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v2/goals?limit=100')) {
        return json({ items: [unbound], count: 1 });
      }
      if (url.endsWith('/api/v2/goals/goal-1/controls') && init?.method === 'POST') {
        controlAction = JSON.parse(String(init.body)).action;
        return json(cancelled);
      }
      if (url.endsWith('/api/v2/goals/goal-1')) return json(unbound);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<GoalWorkspace />);

    expect(await screen.findByRole('button', { name: '停止' })).toBeEnabled();
    expect(screen.getByText('内部长期移动能力尚未组合')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新检查' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '暂停' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '接管' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '停止' }));
    await waitFor(() => expect(controlAction).toBe('stop'));
    expect(await screen.findByText('已停止')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '停止' })).not.toBeInTheDocument();
  });
});
