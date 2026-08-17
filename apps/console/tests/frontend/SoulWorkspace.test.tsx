import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SoulWorkspace } from '../../frontend/src/components/SoulWorkspace';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const PRIVATE_VALUES = {
  identity: 'PRIVATE_IDENTITY_SHOULD_NOT_RENDER',
  body: 'PRIVATE_MESSAGE_BODY_SHOULD_NOT_RENDER',
  screenshot: 'PRIVATE_SCREENSHOT_SHOULD_NOT_RENDER',
  prompt: 'PRIVATE_PROMPT_SHOULD_NOT_RENDER',
};

function applicationInstance(overrides: Record<string, unknown> = {}) {
  return {
    id: 'soul-instance-active',
    profile_id: 'soul-reply-v1',
    status: 'running',
    revision: 7,
    degraded: false,
    hard_risk: false,
    error_code: null,
    memory_version: 4,
    input_count: 2,
    intent_count: 5,
    outcome_count: 4,
    event_count: 18,
    intents: [
      {
        id: 'intent-5',
        cycle: 5,
        revision: 7,
        phase: 'dispatched',
        hard_risk: false,
        created_at: '2026-08-10T01:00:01Z',
        finalized_at: '2026-08-10T01:00:02Z',
        ...PRIVATE_VALUES,
      },
    ],
    outcomes: [
      { cycle: 1, status: 'confirmed_success', hard_risk: false, terminal: false, created_at: '2026-08-10T00:10:00Z' },
      { cycle: 2, status: 'confirmed_failure', hard_risk: false, terminal: false, created_at: '2026-08-10T00:20:00Z' },
      { cycle: 3, status: 'unconfirmed', hard_risk: false, terminal: false, created_at: '2026-08-10T00:30:00Z' },
      { cycle: 4, status: 'uncertain', hard_risk: true, terminal: false, created_at: '2026-08-10T00:40:00Z', ...PRIVATE_VALUES },
    ],
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T01:00:03Z',
    finished_at: null,
    wake_at: null,
    ...PRIVATE_VALUES,
    ...overrides,
  };
}

const schedulerRunning = {
  profile_id: 'soul-reply-v1',
  state: 'running',
  desired_state: 'running',
  effective_state: 'running',
  controller_matches: true,
  code: 'scheduler_running',
  observed_at: '2026-08-10T01:00:04Z',
  ...PRIVATE_VALUES,
};

interface ApiOptions {
  items?: Record<string, unknown>[];
  list?: (attempt: number) => Response | Promise<Response>;
  inspect?: (id: string, attempt: number) => Response | Promise<Response>;
  start?: (body: Record<string, unknown>, attempt: number) => Response | Promise<Response>;
  command?: (id: string, body: Record<string, unknown>, attempt: number) => Response | Promise<Response>;
  scheduler?: (attempt: number) => Response | Promise<Response>;
}

function installApplicationApi(options: ApiOptions = {}) {
  const calls: Array<{ url: string; method: string; body?: Record<string, unknown> }> = [];
  let listAttempt = 0;
  let inspectAttempt = 0;
  let startAttempt = 0;
  let commandAttempt = 0;
  let schedulerAttempt = 0;
  let current = (options.items?.[0] ?? applicationInstance()) as Record<string, unknown>;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    calls.push({ url, method, body });

    if (url.endsWith('/application-profiles/soul-reply-v1/scheduler') && method === 'GET') {
      schedulerAttempt += 1;
      return options.scheduler?.(schedulerAttempt) ?? json(schedulerRunning);
    }

    if (url.endsWith('/application-instances?limit=100') && method === 'GET') {
      listAttempt += 1;
      return options.list?.(listAttempt) ?? json({
        items: options.items ?? [current],
        count: (options.items ?? [current]).length,
      });
    }

    if (url.endsWith('/application-instances') && method === 'POST') {
      startAttempt += 1;
      const response = options.start?.(body ?? {}, startAttempt);
      if (response) return response;
      current = applicationInstance({ id: 'soul-instance-new', status: 'queued', revision: 0 });
      return json(current, 202);
    }

    const commandMatch = url.match(/\/application-instances\/([^/?]+)\/commands$/);
    if (commandMatch && method === 'POST') {
      commandAttempt += 1;
      const id = decodeURIComponent(commandMatch[1]);
      const response = options.command?.(id, body ?? {}, commandAttempt);
      if (response) return response;
      const command = body?.command;
      const status = command === 'Pause'
        ? 'paused'
        : command === 'Resume'
          ? 'running'
          : command === 'Stop'
            ? 'stopping'
            : 'running';
      current = applicationInstance({ id, status, revision: command === 'Input' ? 8 : 7 });
      return json(current, 202);
    }

    const inspectMatch = url.match(/\/application-instances\/([^/?]+)$/);
    if (inspectMatch && method === 'GET') {
      inspectAttempt += 1;
      const id = decodeURIComponent(inspectMatch[1]);
      return options.inspect?.(id, inspectAttempt) ?? json({ ...current, id });
    }

    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls };
}

describe('Soul Application 工作区', () => {
  it('清晰分开全天匹配/即时开场与回复/学习，并只展示两者的安全投影', async () => {
    const { calls } = installApplicationApi();
    render(<SoulWorkspace />);

    const workspace = await screen.findByRole('region', { name: 'Soul 应用' });
    const scheduler = within(workspace).getByRole('region', { name: '全天匹配与即时开场' });
    const replies = within(workspace).getByRole('region', { name: '回复与学习' });
    expect(scheduler).toHaveTextContent('全天运行中');
    expect(scheduler).toHaveTextContent('即时处理开场');
    expect(replies).toHaveTextContent('运行中');
    expect(replies).toHaveTextContent('回复和学习');
    expect(within(workspace).getByText('Soul 是 AI Game 可以长期运行的一项应用功能。')).toBeInTheDocument();
    expect(within(workspace).getByText('运行周期').closest('article')).toHaveTextContent('5');
    expect(within(workspace).getByText('已确认结果').closest('article')).toHaveTextContent('2');
    expect(within(workspace).getByText('等待状态').closest('article')).toHaveTextContent('无需等待');
    expect(within(workspace).getByText('不确定结果').closest('article')).toHaveTextContent('1');
    expect(within(workspace).getByText('学习版本').closest('article')).toHaveTextContent('第 4 版');

    const technicalSummary = within(workspace).getByText('技术详情');
    expect(technicalSummary.closest('details')).not.toHaveAttribute('open');
    for (const privateValue of Object.values(PRIVATE_VALUES)) {
      expect(workspace).not.toHaveTextContent(privateValue);
    }
    expect(calls.some((call) => call.url.includes('/integrations/soul'))).toBe(false);
    expect(calls.some((call) => call.url.endsWith('/application-instances?limit=100'))).toBe(true);
    expect(calls.some((call) => call.url.endsWith('/application-profiles/soul-reply-v1/scheduler'))).toBe(true);
  });

  it('恢复同一个未结束的回复实例，并每 1 秒同时轮询 scheduler 与该实例', async () => {
    vi.useFakeTimers();
    const terminal = applicationInstance({
      id: 'soul-instance-terminal',
      status: 'stopped',
      updated_at: '2026-08-10T02:00:00Z',
      finished_at: '2026-08-10T02:00:00Z',
    });
    const active = applicationInstance({
      id: 'soul-instance-long-lived',
      status: 'waiting',
      wake_at: '2026-08-10T03:00:00Z',
      updated_at: '2026-08-10T01:00:00Z',
    });
    const { calls } = installApplicationApi({ items: [terminal, active] });
    render(<SoulWorkspace />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getAllByText('等待下一轮').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: '启动 Soul' })).toBeDisabled();
    expect(calls.filter((call) => call.url.includes('/application-instances/soul-instance-long-lived'))).toHaveLength(0);
    expect(calls.filter((call) => call.url.endsWith('/application-profiles/soul-reply-v1/scheduler'))).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(calls.filter((call) => call.url.endsWith('/application-instances/soul-instance-long-lived'))).toHaveLength(0);
    expect(calls.filter((call) => call.url.endsWith('/application-profiles/soul-reply-v1/scheduler'))).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(calls.filter((call) => call.url.endsWith('/application-instances/soul-instance-long-lived'))).toHaveLength(1);
    expect(calls.filter((call) => call.url.endsWith('/application-profiles/soul-reply-v1/scheduler'))).toHaveLength(2);
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  });

  it('全天 scheduler 运行时允许失败的回复实例重新启动，并同时如实显示两种状态', async () => {
    installApplicationApi({
      items: [applicationInstance({
        id: 'soul-instance-failed',
        status: 'failed',
        error_code: 'reply_cycle_failed',
        finished_at: '2026-08-10T01:00:03Z',
      })],
    });
    render(<SoulWorkspace />);

    const scheduler = await screen.findByRole('region', { name: '全天匹配与即时开场' });
    await waitFor(() => expect(scheduler).toHaveTextContent('全天运行中'));
    expect(screen.getByRole('region', { name: '回复与学习' })).toHaveTextContent('回复运行异常');
    expect(screen.getByRole('button', { name: '启动 Soul' })).toBeEnabled();
  });

  it('scheduler 未达到目标状态时展示真实目标、实际状态和同请求重试边界', async () => {
    installApplicationApi({
      scheduler: () => json({
        profile_id: 'soul-reply-v1',
        state: 'degraded',
        desired_state: 'running',
        effective_state: 'paused',
        controller_matches: false,
        code: 'scheduler_controller_mismatch',
        observed_at: '2026-08-10T01:00:04Z',
        ...PRIVATE_VALUES,
      }),
    });
    render(<SoulWorkspace />);

    const scheduler = await screen.findByRole('region', { name: '全天匹配与即时开场' });
    expect(scheduler).toHaveTextContent('正在恢复控制权');
    expect(scheduler).toHaveTextContent('目标：全天运行');
    expect(scheduler).toHaveTextContent('实际：已暂停');
    expect(scheduler).toHaveTextContent('页面不会自动重发控制请求');
    expect(screen.getAllByRole('button').map((button) => button.textContent?.trim())).not.toContain('重试 scheduler');
    for (const privateValue of Object.values(PRIVATE_VALUES)) {
      expect(scheduler).not.toHaveTextContent(privateValue);
    }
  });

  it('scheduler 安全收尾时以 effective_state 展示正在停止', async () => {
    installApplicationApi({
      scheduler: () => json({
        profile_id: 'soul-reply-v1',
        state: 'degraded',
        desired_state: 'stopped',
        effective_state: 'stopping',
        controller_matches: true,
        code: 'scheduler_stopping',
        observed_at: '2026-08-10T01:00:04Z',
      }),
    });
    render(<SoulWorkspace />);

    const scheduler = await screen.findByRole('region', { name: '全天匹配与即时开场' });
    expect(scheduler).toHaveTextContent('全天运行正在停止');
    expect(scheduler).toHaveTextContent('目标：保持停止');
    expect(scheduler).toHaveTextContent('实际：正在停止');
    expect(scheduler).toHaveTextContent('页面只会刷新状态，不会自动重发控制请求');
  });

  it('scheduler 轮询失败后撤销旧状态的 fresh 标记并禁用生命周期控制', async () => {
    vi.useFakeTimers();
    installApplicationApi({
      scheduler: (attempt) => attempt === 1
        ? json(schedulerRunning)
        : Promise.reject(new TypeError('scheduler offline')),
    });
    render(<SoulWorkspace />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole('region', { name: '全天匹配与即时开场' })).toHaveTextContent('全天运行中');
    expect(screen.getByRole('button', { name: '暂停' })).toBeEnabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    const scheduler = screen.getByRole('region', { name: '全天匹配与即时开场' });
    expect(scheduler).toHaveTextContent('状态未确认');
    expect(scheduler).toHaveTextContent('暂时无法确认全天匹配与即时开场状态');
    expect(screen.getByRole('button', { name: '暂停' })).toBeDisabled();
  });

  it('启动通过 application-instances 创建新实例，并携带控制台写请求头', async () => {
    const user = userEvent.setup();
    let resolveStart!: (response: Response) => void;
    const startResponse = new Promise<Response>((resolve) => {
      resolveStart = resolve;
    });
    const { calls } = installApplicationApi({
      items: [],
      start: () => startResponse,
    });
    render(<SoulWorkspace />);

    const start = await screen.findByRole('button', { name: '启动 Soul' });
    await user.click(start);
    expect(start).toBeDisabled();
    const post = calls.find((call) => call.method === 'POST');
    expect(post?.url).toMatch(/\/api\/v1\/application-instances$/);
    expect(post?.body).toEqual({
      profile_id: 'soul-reply-v1',
      client_request_id: expect.any(String),
    });
    const fetchCall = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith('/application-instances') && init?.method === 'POST');
    expect(new Headers(fetchCall?.[1]?.headers).get('X-AI-Game-Client')).toBe('console-v1');

    await act(async () => {
      resolveStart(json(applicationInstance({ id: 'soul-instance-created', status: 'queued' }), 202));
    });
    expect((await screen.findAllByText('正在准备')).length).toBeGreaterThan(0);
    expect(screen.getByText('启动请求已受理。')).toBeInTheDocument();
    expect(calls.some((call) => call.url.includes('/integrations/soul/commands'))).toBe(false);
  });

  it('暂停、继续、停止和补充指令都使用同一实例的 commands Interface', async () => {
    const user = userEvent.setup();
    const { calls } = installApplicationApi();
    render(<SoulWorkspace />);

    const controls = await screen.findByRole('region', { name: 'Soul 运行控制' });
    await user.click(within(controls).getByRole('button', { name: '暂停' }));
    expect((await screen.findAllByText('已暂停')).length).toBeGreaterThan(0);
    await user.click(within(controls).getByRole('button', { name: '继续' }));
    expect(await screen.findByText('继续请求已受理。')).toBeInTheDocument();

    const instruction = within(controls).getByRole('textbox', { name: '补充指令' });
    await user.type(instruction, '  今天只处理已有对话  ');
    await user.click(within(controls).getByRole('button', { name: '发送补充指令' }));
    expect(await screen.findByText('补充指令已受理。')).toBeInTheDocument();
    expect(instruction).toHaveValue('');

    await user.click(within(controls).getByRole('button', { name: '停止' }));
    expect((await screen.findAllByText('正在停止')).length).toBeGreaterThan(0);

    const commandPosts = calls.filter((call) => call.url.endsWith('/commands'));
    expect(commandPosts.map((call) => call.body)).toEqual([
      { command: 'Pause', client_request_id: expect.any(String) },
      { command: 'Resume', client_request_id: expect.any(String) },
      { command: 'Input', client_request_id: expect.any(String), content: '今天只处理已有对话' },
      { command: 'Stop', client_request_id: expect.any(String) },
    ]);
    expect(new Set(commandPosts.map((call) => call.url))).toEqual(new Set([
      '/api/v1/application-instances/soul-instance-active/commands',
    ]));
  });

  it('写请求结果未知时只允许用完全相同的 request id 和内容显式重试', async () => {
    const user = userEvent.setup();
    const bodies: Record<string, unknown>[] = [];
    const { calls } = installApplicationApi({
      command: (_id, body, attempt) => {
        bodies.push(body);
        if (attempt === 1) throw new TypeError('connection lost');
        return json(applicationInstance({ revision: 8, input_count: 3 }), 202);
      },
    });
    render(<SoulWorkspace />);

    const controls = await screen.findByRole('region', { name: 'Soul 运行控制' });
    const instruction = within(controls).getByRole('textbox', { name: '补充指令' });
    await user.type(instruction, '遇到需要确认的页面就等待');
    await user.click(within(controls).getByRole('button', { name: '发送补充指令' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('结果暂时无法确认');
    expect(calls.filter((call) => call.url.endsWith('/commands'))).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '使用同一请求重试' }));
    expect(await screen.findByText('补充指令已受理。')).toBeInTheDocument();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[0]).toEqual({
      command: 'Input',
      client_request_id: expect.any(String),
      content: '遇到需要确认的页面就等待',
    });
  });

  it('启动结果未知时也复用原 start request id，不创建第二种请求内容', async () => {
    const user = userEvent.setup();
    const bodies: Record<string, unknown>[] = [];
    installApplicationApi({
      items: [],
      start: (body, attempt) => {
        bodies.push(body);
        if (attempt === 1) throw new TypeError('connection lost');
        return json(applicationInstance({ id: 'soul-instance-recovered', status: 'queued' }), 202);
      },
    });
    render(<SoulWorkspace />);

    await user.click(await screen.findByRole('button', { name: '启动 Soul' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('结果暂时无法确认');
    await user.click(screen.getByRole('button', { name: '使用同一请求重试' }));
    expect(await screen.findByText('启动请求已受理。')).toBeInTheDocument();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[0]).toEqual({
      profile_id: 'soul-reply-v1',
      client_request_id: expect.any(String),
    });
  });

  it('读取失败时给出可恢复提示，不开放可能基于旧状态的写控制', async () => {
    installApplicationApi({
      list: () => Promise.reject(new TypeError('offline')),
    });
    render(<SoulWorkspace />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('暂时无法读取 Soul 应用状态');
    expect(alert).not.toHaveTextContent('application_runtime_history_unavailable');
    expect(screen.getByRole('button', { name: '启动 Soul' })).toBeDisabled();
    expect(screen.queryByText(/PRIVATE_/)).not.toBeInTheDocument();
  });
});
