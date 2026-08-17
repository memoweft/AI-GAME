import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MobileTaskWorkspace } from '../../frontend/src/components/MobileTaskWorkspace';
import type { Target } from '../../frontend/src/types';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function target(overrides: Partial<Target> = {}): Target {
  return {
    id: 'adb:emulator-5554',
    name: 'MuMu 模拟器',
    kind: 'android',
    status: 'ready',
    address: 'emulator-5554',
    detail: 'ADB 已连接。',
    capabilities: ['android_adb', 'screen_capture', 'touch_input'],
    source: 'adb',
    external_id: 'emulator-5554',
    details: { connection_type: 'emulator', adb_state: 'device', capabilities: ['android_adb', 'screen_capture', 'touch_input'] },
    discovered_at: '2026-08-10T00:00:00Z',
    last_seen_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    ...overrides,
  };
}

const singleReadyTarget = target();
const unavailableTarget = target({ id: 'adb:offline', name: '离线手机', status: 'offline', external_id: 'offline' });
const windowsTarget = target({ id: 'windows-local', name: '本机桌面', kind: 'windows', source: 'seed', external_id: null });

function mobileTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task-active',
    goal: '领取每日奖励',
    target_id: singleReadyTarget.id,
    skill_id: 'daily-reward',
    status: 'running',
    input_revision: 1,
    plan: {
      revision: 2,
      subgoals: [
        { index: 0, description: '打开活动入口', status: 'completed' },
        { index: 1, description: '领取奖励', status: 'active' },
      ],
    },
    active_subgoal_index: 1,
    strategy: '先打开侧边栏，再进入活动页',
    no_progress_count: 2,
    reflection_count: 1,
    attempt_count: 3,
    cancel_requested: false,
    verification_satisfied: false,
    detail: '正在核对奖励是否已经到账',
    error_code: null,
    skill_memory_version: 2,
    inputs: [{ revision: 1, content: '优先领取限时奖励', lifecycle: 'applied', client_request_id: 'input-existing', created_at: '2026-08-10T00:00:01Z', applied_at: '2026-08-10T00:00:02Z' }],
    attempts: [{
      id: 'attempt-3', sequence: 3, subgoal_index: 1, action_type: 'tap', transport_status: 'accepted',
      verification: { satisfied: false, progress: false, uncertain: false, evidence: '页面仍停留在活动列表' },
      action_arguments: { text: 'SECRET_ACTION_TEXT' }, created_at: '2026-08-10T00:00:03Z', finalized_at: '2026-08-10T00:00:04Z',
    }],
    reflections: [{ sequence: 1, previous_strategy: '直接点击奖励入口', strategy: '先打开侧边栏，再进入活动页', reason: '连续三次没有进展', consecutive_no_progress: 3, created_at: '2026-08-10T00:00:05Z' }],
    events: [{ sequence: 1, event_type: 'reflection_recorded', data: { raw: 'SECRET_EVENT_DATA' }, created_at: '2026-08-10T00:00:05Z' }],
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:05Z',
    finished_at: null,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('一句话开始', () => {
  it('composer 只询问目标与可用 Android 设备，单台设备自动选中且不发送 skill_id', async () => {
    const user = userEvent.setup();
    const bodies: Record<string, unknown>[] = [];
    let createAttempt = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method || 'GET';
      if (url.endsWith('/tasks?limit=100') && method === 'GET') return json({ items: [], count: 0 });
      if (url.endsWith('/tasks') && method === 'POST') {
        createAttempt += 1;
        const body = JSON.parse(String(init?.body));
        bodies.push(body);
        return createAttempt === 1
          ? json({ error: { code: 'temporarily_unavailable', message: '现在还不能开始，请稍后重试。' } }, 503)
          : json(mobileTask({ id: 'task-created', goal: body.goal, target_id: body.target_id, skill_id: null, status: 'queued', plan: null, active_subgoal_index: 0, attempts: [], reflections: [], events: [], inputs: [] }), 202);
      }
      if (url.endsWith('/tasks/task-created') && method === 'GET') return json(mobileTask({ id: 'task-created', goal: '领取游戏奖励', target_id: singleReadyTarget.id, skill_id: null, status: 'queued', plan: null, active_subgoal_index: 0, attempts: [], reflections: [], events: [], inputs: [] }));
      throw new Error(`Unexpected request: ${method} ${url}`);
    }));

    render(<MobileTaskWorkspace targets={[singleReadyTarget, unavailableTarget, windowsTarget]} />);
    await screen.findByText('还没有任务');
    expect(screen.getByRole('region', { name: '一句话开始' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '一句话告诉我，你想让手机完成什么' })).toBeInTheDocument();
    const device = screen.getByLabelText('运行设备');
    expect(device).toHaveValue(singleReadyTarget.id);
    expect(within(device).getAllByRole('option')).toHaveLength(1);
    expect(screen.queryByLabelText(/技能/)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/skill_id|profile|transition|reward|mode/i);

    await user.type(screen.getByLabelText('你想完成什么'), '领取游戏奖励');
    await user.click(screen.getByRole('button', { name: '开始' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('现在还不能开始');
    await user.click(screen.getByRole('button', { name: '重试同一请求' }));
    expect(await screen.findByRole('heading', { name: '领取游戏奖励' })).toBeInTheDocument();

    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual({ goal: '领取游戏奖励', client_request_id: expect.any(String), target_id: singleReadyTarget.id });
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it('多台可用 Android 设备时要求用户明确选择，不显示桌面或离线设备', async () => {
    const second = target({ id: 'adb:R58M1234AB', name: 'Galaxy 平板', external_id: 'R58M1234AB', details: { connection_type: 'usb' } });
    vi.stubGlobal('fetch', vi.fn(async () => json({ items: [], count: 0 })));
    const user = userEvent.setup();
    render(<MobileTaskWorkspace targets={[singleReadyTarget, second, unavailableTarget, windowsTarget]} />);

    await screen.findByText('还没有任务');
    const device = screen.getByLabelText('运行设备');
    expect(device).toHaveValue('');
    expect(within(device).getByRole('option', { name: '请选择设备' })).toBeInTheDocument();
    expect(within(device).getByRole('option', { name: /MuMu 模拟器/ })).toBeInTheDocument();
    expect(within(device).getByRole('option', { name: /Galaxy 平板/ })).toBeInTheDocument();
    expect(within(device).queryByRole('option', { name: /离线手机|本机桌面/ })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('你想完成什么'), '打开地图');
    expect(screen.getByRole('button', { name: '开始' })).toBeDisabled();
    await user.selectOptions(device, second.id);
    expect(screen.getByRole('button', { name: '开始' })).toBeEnabled();
  });

  it('运行页默认只显示人话进度与当前动作，计划、尝试、调整和事件收进技术详情', async () => {
    const task = mobileTask();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/tasks?limit=100')) return json({ items: [task], count: 1 });
      if (url.endsWith('/tasks/task-active')) return json(task);
      throw new Error(`Unexpected request: GET ${url}`);
    }));

    render(<MobileTaskWorkspace targets={[singleReadyTarget]} />);
    expect(await screen.findByRole('heading', { name: '领取每日奖励' })).toBeInTheDocument();
    const progress = screen.getByRole('region', { name: '任务当前进度' });
    expect(progress).toHaveTextContent('正在处理');
    expect(progress).toHaveTextContent('领取奖励');
    expect(progress).toHaveTextContent('当前动作');
    expect(progress).toHaveTextContent('点击屏幕');
    expect(progress).toHaveTextContent('动作已送到设备，正在根据新画面确认结果');
    expect(screen.getByRole('heading', { name: '中途补充' })).toBeInTheDocument();
    expect(screen.getByLabelText('补充要求')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送补充' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '停止任务' })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('TaskState');
    expect(document.body).not.toHaveTextContent('daily-reward');

    const details = screen.getByText('技术详情').closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    expect(within(details as HTMLElement).getByRole('heading', { name: '计划' })).toBeInTheDocument();
    expect(within(details as HTMLElement).getByRole('heading', { name: '最近尝试' })).toBeInTheDocument();
    expect(within(details as HTMLElement).getByRole('heading', { name: '调整记录' })).toBeInTheDocument();
    expect(within(details as HTMLElement).getByRole('heading', { name: '任务事件' })).toBeInTheDocument();
    expect(screen.queryByText('SECRET_ACTION_TEXT')).not.toBeInTheDocument();
    expect(screen.queryByText('SECRET_EVENT_DATA')).not.toBeInTheDocument();
  });

  it('轮询活动任务，并且完成只依据新的验证结果', async () => {
    const active = mobileTask({ attempts: [] });
    const completed = mobileTask({ status: 'completed', verification_satisfied: true, detail: '已经在新画面中确认奖励到账', finished_at: '2026-08-10T00:00:10Z' });
    let detailAttempt = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/tasks?limit=100')) return json({ items: [active], count: 1 });
      if (url.endsWith('/tasks/task-active')) {
        detailAttempt += 1;
        return json(detailAttempt === 1 ? active : completed);
      }
      throw new Error(`Unexpected request: GET ${url}`);
    }));

    render(<MobileTaskWorkspace targets={[singleReadyTarget]} />);
    expect((await screen.findAllByText('执行中')).length).toBeGreaterThan(0);
    await waitFor(() => expect(document.querySelector('.agent-status')).toHaveTextContent('已完成'), { timeout: 2500 });
    expect(await screen.findByText('已经在新画面中确认奖励到账')).toBeInTheDocument();
    expect(detailAttempt).toBeGreaterThanOrEqual(2);
  });

  it('中途补充与停止失败后分别复用原 request id 显式重试', async () => {
    const user = userEvent.setup();
    const inputBodies: Record<string, unknown>[] = [];
    const stopBodies: Record<string, unknown>[] = [];
    let inputAttempt = 0;
    let stopAttempt = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method || 'GET';
      if (url.endsWith('/tasks?limit=100')) return json({ items: [mobileTask()], count: 1 });
      if (url.endsWith('/tasks/task-active') && method === 'GET') return json(stopAttempt >= 2 ? mobileTask({ status: 'stopped', finished_at: '2026-08-10T00:00:09Z' }) : mobileTask());
      if (url.endsWith('/tasks/task-active/inputs') && method === 'POST') {
        inputAttempt += 1;
        const body = JSON.parse(String(init?.body));
        inputBodies.push(body);
        return inputAttempt === 1
          ? json({ error: { message: '补充要求暂未提交。' } }, 503)
          : json(mobileTask({ input_revision: 2, inputs: [...mobileTask().inputs, { revision: 2, content: body.content, lifecycle: 'accepted', client_request_id: body.client_request_id, created_at: '2026-08-10T00:00:06Z', applied_at: null }] }), 202);
      }
      if (url.endsWith('/tasks/task-active/stop') && method === 'POST') {
        stopAttempt += 1;
        const body = JSON.parse(String(init?.body));
        stopBodies.push(body);
        return stopAttempt === 1 ? json({ error: { message: '停止请求暂未提交。' } }, 503) : json(mobileTask({ status: 'stopping', cancel_requested: true }), 202);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    }));

    render(<MobileTaskWorkspace targets={[singleReadyTarget]} />);
    await screen.findByRole('heading', { name: '领取每日奖励' });
    await user.type(screen.getByLabelText('补充要求'), '先关闭弹窗再继续');
    await user.click(screen.getByRole('button', { name: '发送补充' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('补充要求暂未提交');
    await user.click(screen.getByRole('button', { name: '重试同一条补充' }));
    await waitFor(() => expect(inputBodies).toHaveLength(2));
    expect(inputBodies[1]).toEqual(inputBodies[0]);

    await user.click(screen.getByRole('button', { name: '停止任务' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('停止请求暂未提交');
    await user.click(screen.getByRole('button', { name: '重试停止请求' }));
    await waitFor(() => expect(stopBodies).toHaveLength(2));
    expect(stopBodies[1]).toEqual(stopBodies[0]);
    expect(stopBodies[0].client_request_id).not.toBe(inputBodies[0].client_request_id);
  });
});
