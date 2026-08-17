import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LearningWorkspace } from '../../frontend/src/components/LearningWorkspace';

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

const profile = {
  id: 'stzb-tutorial-v1',
  name: '率土之滨低频教程与菜单导航',
  revision: 1,
  allowed_actions: ['tap', 'back', 'wait', 'swipe'],
  max_transitions: 25,
  max_duration_seconds: 180,
  default_target_id: 'target-android',
};

function installLearningApi(options: {
  jobs?: Record<string, unknown>[];
  create?: (body: Record<string, unknown>, attempt: number) => Response;
  detail?: (id: string, attempt: number) => Response;
  stop?: (id: string) => Response;
} = {}) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let createAttempt = 0;
  let detailAttempt = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    calls.push({ url, init });
    if (url.endsWith('/learning/profiles') && method === 'GET') {
      return json({ items: [profile], count: 1 });
    }
    if (url.endsWith('/learning/jobs') && method === 'GET') {
      const jobs = options.jobs ?? [];
      return json({ items: jobs, count: jobs.length });
    }
    if (url.endsWith('/learning/jobs') && method === 'POST') {
      createAttempt += 1;
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return options.create?.(body, createAttempt) ?? json({
        id: 'job-created',
        ...body,
        phase: 'accepted',
        result: 'pending',
      }, 202);
    }
    const stopMatch = url.match(/\/learning\/jobs\/([^/]+)\/stop$/);
    if (stopMatch && method === 'POST') {
      return options.stop?.(decodeURIComponent(stopMatch[1])) ?? json({
        id: decodeURIComponent(stopMatch[1]),
        phase: 'stopping',
        result: 'pending',
        cancel_requested: true,
      }, 202);
    }
    const detailMatch = url.match(/\/learning\/jobs\/([^/]+)$/);
    if (detailMatch && method === 'GET') {
      detailAttempt += 1;
      return options.detail?.(decodeURIComponent(detailMatch[1]), detailAttempt) ?? json({
        id: decodeURIComponent(detailMatch[1]),
        phase: 'running',
        result: 'pending',
      });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls };
}

describe('学习工作台', () => {
  it('默认使用率土之滨 Profile，开始请求失败后重试复用同一个 client_request_id', async () => {
    const user = userEvent.setup();
    const bodies: Record<string, unknown>[] = [];
    const { calls } = installLearningApi({
      create: (body, attempt) => {
        bodies.push(body);
        return attempt === 1
          ? json({ error: { code: 'temporarily_unavailable', message: '学习执行器暂时不可用。' } }, 503)
          : json({ id: 'job-retried', ...body, phase: 'accepted', result: 'pending' }, 202);
      },
    });
    render(<LearningWorkspace />);

    expect(await screen.findByText('率土之滨低频教程与菜单导航')).toBeInTheDocument();
    expect(screen.getByText('25 个 Transition · 180 秒')).toBeInTheDocument();
    const instruction = screen.getByLabelText('这次想让它学会什么？');
    expect(instruction).toHaveAttribute('maxlength', '200');
    expect(instruction).toHaveAttribute('placeholder', '例如：按照当前教程提示继续教程');
    expect(screen.getByText(/支持示例：继续教程、打开任务列表、查看武将列表、返回主界面/)).toBeInTheDocument();
    await user.type(instruction, '打开任务列表');
    await user.click(screen.getByRole('button', { name: '开始学习' }));

    expect(await screen.findByText('学习执行器暂时不可用。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重试同一请求' }));
    expect(await screen.findByText('等待开始')).toBeInTheDocument();

    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual({
      instruction: '打开任务列表',
      client_request_id: expect.any(String),
      profile_id: 'stzb-tutorial-v1',
      target_id: 'target-android',
    });
    expect(bodies[1].client_request_id).toBe(bodies[0].client_request_id);
    const posts = calls.filter((call) => call.url.endsWith('/learning/jobs') && call.init?.method === 'POST');
    expect(new Headers(posts[0].init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
  });

  it('每秒轮询活动 Job，并按 phase terminal + result learned 显示终态和真实指标', async () => {
    const accepted = {
      id: 'job-poll',
      profile_id: profile.id,
      instruction: '推进教程',
      phase: 'running',
      result: 'pending',
      outcome: 'pending',
      control_state: 'executing',
      policy_state: 'unchanged',
      transition_count: 1,
      created_at: '2026-08-09T10:00:00Z',
    };
    installLearningApi({
      jobs: [accepted],
      detail: () => json({
        ...accepted,
        phase: 'terminal',
        result: 'learned',
        outcome: 'verified_success',
        control_state: 'neutral',
        policy_state: 'updated',
        transition_count: 3,
        total_reward: 2.5,
        verified_successes: 2,
        policy_memory_revision: 4,
        policy_memory_count: 8,
        detail: '已确认教程步骤推进。',
        finished_at: '2026-08-09T10:00:04Z',
      }),
    });
    render(<LearningWorkspace />);

    expect(await screen.findByText('学习进行中')).toBeInTheDocument();
    expect(await screen.findByText('学习已完成', {}, { timeout: 2500 })).toBeInTheDocument();
    const facts = screen.getByText('运行事实').closest('article');
    expect(facts).not.toBeNull();
    expect(within(facts!).getByText('terminal')).toBeInTheDocument();
    expect(within(facts!).getByText('learned')).toBeInTheDocument();
    expect(within(facts!).getByText('verified_success')).toBeInTheDocument();
    expect(within(facts!).getByText('neutral')).toBeInTheDocument();
    expect(within(facts!).getByText('updated')).toBeInTheDocument();
    expect(screen.getByText('3', { selector: '.learning-metric-grid strong' })).toBeInTheDocument();
    expect(screen.getByText('2.5', { selector: '.learning-metric-grid strong' })).toBeInTheDocument();
    expect(screen.getByText('4', { selector: '.learning-metric-grid strong' })).toBeInTheDocument();
    expect(screen.getByText('8', { selector: '.learning-metric-grid strong' })).toBeInTheDocument();
    expect(screen.getByText('已确认教程步骤推进。')).toBeInTheDocument();
  });

  it('停止只显示 stopping，不把已提交停止请求冒充为已停止', async () => {
    const user = userEvent.setup();
    const running = {
      id: 'job-running',
      profile_id: profile.id,
      instruction: '查看武将列表',
      phase: 'running',
      result: 'pending',
      control_state: 'executing',
      created_at: '2026-08-09T10:00:00Z',
    };
    const { calls } = installLearningApi({ jobs: [running] });
    render(<LearningWorkspace />);

    await user.click(await screen.findByRole('button', { name: '停止学习' }));
    expect(await screen.findByText('正在停止', { selector: '.learning-state-banner strong' })).toBeInTheDocument();
    expect(screen.getByText(/当前还不能视为已停止/)).toBeInTheDocument();
    expect(screen.queryByText('学习已停止')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '正在停止' })).toBeDisabled();
    const stop = calls.find((call) => call.url.endsWith('/learning/jobs/job-running/stop'));
    expect(stop).toBeDefined();
    expect(new Headers(stop?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
  });

  it('历史 Job 可切换，并按 terminal result 显示失败和已停止文案', async () => {
    const user = userEvent.setup();
    installLearningApi({
      jobs: [
        { id: 'job-failed', instruction: '失败任务', phase: 'terminal', result: 'failed', error_code: 'postcondition_not_observed' },
        { id: 'job-stopped', instruction: '停止任务', phase: 'terminal', result: 'stopped' },
      ],
    });
    render(<LearningWorkspace />);

    expect(await screen.findByText('学习失败')).toBeInTheDocument();
    expect(screen.getByText('postcondition_not_observed')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /停止任务/ }));
    expect(await screen.findByText('学习已停止')).toBeInTheDocument();
    expect(screen.queryByText('正在停止', { selector: '.learning-state-banner strong' })).not.toBeInTheDocument();
  });
});
