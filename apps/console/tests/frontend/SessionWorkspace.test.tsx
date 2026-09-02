import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionWorkspace } from '../../frontend/src/components/SessionWorkspace';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const session = {
  id: 'session-1', original_instruction: '打开设置看看电池，然后回到桌面', authority_revision: 1,
  session_kind: 'today', status: 'ACTIVE', control_mode: 'AGENT_ACTIVE', active_goal_id: 'node-1',
  event_cursor: 3, summary: null, created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-24T00:02:00Z', stopped_at: null,
  directives: [{ id: 'directive-1', revision: 1, content: '打开设置看看电池，然后回到桌面', directive_kind: 'original', created_at: '2026-08-24T00:00:00Z' }],
  goal_nodes: [{ id: 'node-1', title: '打开设置看看电池，然后回到桌面', status: 'ACTIVE', bound_goal_run_id: 'goal-run-1' }],
};

const events = { items: [{ id: 'event-3', cursor: 3, session_id: 'session-1', event_type: 'goal_run_bound', data: {}, handling_status: 'HANDLED', created_at: '2026-08-24T00:02:00Z', handled_at: '2026-08-24T00:02:00Z' }], count: 1 };

describe('R2 SessionWorkspace', () => {
  it('展示 R2 多 Goal 图、原始片段、条件和独立激活绑定', async () => {
    const graphSession = {
      ...session,
      authority_revision: 2,
      goal_graph_revision: 2,
      graph_revisions: [{ revision: 1, summary: '初始目标图' }, { revision: 2, summary: '新增招聘目标' }],
      goal_nodes: [
        { id: 'node-1', title: 'Soul 认识人', status: 'ACTIVE', bound_goal_run_id: 'goal-run-1', original_fragment: '去 Soul 认识些人', explicit_priority: 1, activation_state: 'ACTIVE', criteria: [{ id: 'criterion-1', description: '完成一次有证据支持的互动', status: 'PENDING' }] },
        { id: 'node-2', title: '投简历', status: 'PREPARED', bound_goal_run_id: null, original_fragment: '顺便帮我投简历', explicit_priority: 2, activation_state: 'PREPARED', criteria: [{ id: 'criterion-2', description: '找到并提交一份合适职位', status: 'PENDING' }] },
        { id: 'node-3', title: '微信维护', status: 'WAITING', bound_goal_run_id: 'goal-run-3', original_fragment: '微信有人找我就维护一下', activation_state: 'WAITING', criteria: [{ id: 'criterion-3', description: '新消息出现时作出合适回应', status: 'PENDING' }] },
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [graphSession], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    expect(await screen.findByText('目标图 revision 2')).toBeInTheDocument();
    expect(screen.getByText('3 个 Goal')).toBeInTheDocument();
    expect(screen.getAllByText('Soul 认识人').length).toBeGreaterThan(0);
    expect(screen.getByText('投简历')).toBeInTheDocument();
    expect(screen.getByText('微信维护')).toBeInTheDocument();
    expect(screen.getByText('原始意图：顺便帮我投简历')).toBeInTheDocument();
    expect(screen.getByText('找到并提交一份合适职位')).toBeInTheDocument();
    expect(screen.getByText('新增招聘目标')).toBeInTheDocument();
  });

  it('展示 R3 当前选择、候选排名、等待恢复条件和 Continuation', async () => {
    const scheduledSession = {
      ...session,
      active_goal_id: 'node-1',
      pending_event_count: 2,
      attention_decision: {
        id: 'decision-7', revision: 7, selected_goal_id: 'node-2',
        reason: '微信新消息比常规目标更紧急', trigger_kind: 'EVENT',
        basis: ['新通知 urgency 90', '用户优先级 20'], created_at: '2026-08-24T00:03:00Z',
      },
      attention_candidates: [
        { goal_node_id: 'node-2', rank: 1, score: 114.6, eligible: true, eligibility_reason: '匹配新通知', hard_tier: 0, scheduling_class: 'NORMAL' },
        { goal_node_id: 'node-1', rank: 2, score: 70.2, eligibility: 'ELIGIBLE', scheduling_class: 'NORMAL' },
        { goal_node_id: 'node-3', rank: 3, score: -5, eligibility: 'IDLE_DEFERRED', eligibility_reason: '有普通 READY Goal 时延后', scheduling_class: 'IDLE_ONLY' },
      ],
      continuations: [{ goal_node_id: 'node-1', revision: 3, summary: '已打开设置，下一步读取电池状态', created_at: '2026-08-24T00:02:59Z' }],
      wake_conditions: [{ goal_node_id: 'node-1', kind: 'WAITING_TIME', reason: '等待系统统计刷新', earliest_wake_at: '2026-08-24T00:10:00Z' }],
      goal_nodes: [
        { id: 'node-1', title: '查看电池', status: 'WAITING_TIME', bound_goal_run_id: 'goal-run-1', eligibility: 'WAITING', eligibility_reason: '等待系统统计刷新', attention_score: 70.2, next_eligible_at: '2026-08-24T00:10:00Z' },
        { id: 'node-2', title: '处理微信新消息', status: 'ACTIVE', bound_goal_run_id: 'goal-run-2', eligibility: 'ELIGIBLE', attention_score: 114.6 },
        { id: 'node-3', title: '空闲时玩游戏', status: 'READY', bound_goal_run_id: null, eligibility: 'IDLE_DEFERRED', eligibility_reason: '有普通 READY Goal 时延后', scheduling_class: 'IDLE_ONLY', attention_score: -5 },
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [scheduledSession], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json({
        items: [{
          id: 'event-4', cursor: 4, session_id: 'session-1', event_type: 'attention_decision_committed',
          data: {}, handling_status: 'HANDLED', created_at: '2026-08-24T00:03:00Z', handled_at: '2026-08-24T00:03:00Z',
        }],
        count: 1,
      });
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    const attention = await screen.findByRole('region', { name: '注意力调度' });
    expect(within(attention).getByText('decision revision 7')).toBeInTheDocument();
    expect(within(attention).getByText('微信新消息比常规目标更紧急')).toBeInTheDocument();
    expect(within(attention).getByText('新通知 urgency 90 · 用户优先级 20')).toBeInTheDocument();
    expect(within(attention).getByText('2 条')).toBeInTheDocument();
    expect(within(attention).getByText('已打开设置，下一步读取电池状态')).toBeInTheDocument();
    expect(within(attention).getByText('#1 处理微信新消息')).toBeInTheDocument();
    expect(within(attention).getByText('115 分')).toBeInTheDocument();
    expect(within(attention).getByText('IDLE_DEFERRED')).toBeInTheDocument();
    expect(screen.getAllByText('等待系统统计刷新').length).toBeGreaterThan(0);
    expect(screen.getByText(/最早/)).toBeInTheDocument();
    expect(screen.getByText('空闲时处理')).toBeInTheDocument();
    expect(screen.getByText('已确定现在优先处理的目标')).toBeInTheDocument();
  });

  it('展示 R7 event -> affected Goal -> checkpoint -> decision -> fresh observation 切换链', async () => {
    const crossAppSession = {
      ...session,
      active_goal_id: 'goal-wechat',
      current_application: 'com.tencent.mm',
      attention_decision: {
        id: 'decision-8', decision_revision: 8, selected_goal_id: 'goal-wechat',
        reason: '微信通知唤醒了消息 Goal', trigger_event_id: 'event-notification',
        preemption_policy: 'VERIFIED_CHECKPOINT_ONLY', preemption_checkpoint_ref: 'checkpoint:goal-a:step-2',
        created_at: '2026-08-26T02:03:00Z',
      },
      continuations: [{
        id: 'continuation-a', goal_id: 'goal-a', revision: 4, attention_decision_id: 'decision-8',
        checkpoint_kind: 'VERIFIED_ACTION', checkpoint_ref: 'checkpoint:goal-a:step-2',
        yield_reason: 'PREEMPTED', application_package: 'com.android.settings',
        summary: '设置页已完成两步，下一步读取电池详情', created_at: '2026-08-26T02:02:59Z',
      }],
      event_runtime: {
        pending_preemption_event_id: 'event-notification',
        pending_preemption_reason: '新消息与微信 Goal 的等待条件精确匹配',
        recovery_context_ref: 'snapshot:wechat:fresh-9',
      },
      goal_nodes: [
        { id: 'goal-a', title: '查看电池状态', status: 'READY', bound_goal_run_id: 'run-a', application_hint: 'com.android.settings' },
        { id: 'goal-wechat', title: '处理微信新消息', status: 'ACTIVE', bound_goal_run_id: 'run-b', application_hint: 'com.tencent.mm' },
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [crossAppSession], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json({
        items: [{
          id: 'event-notification', cursor: 8, session_id: 'session-1', event_type: 'NotificationPostedEvent',
          data: { application_package: 'com.tencent.mm', routing: { reason: '微信通知精确命中等待条件' } },
          handling_status: 'HANDLED', source_namespace: 'android-companion-v1', source_event_id: 'notification-42',
          affected_goal_ids: ['goal-wechat'], decision_id: 'decision-8', created_at: '2026-08-26T02:02:58Z',
          handled_at: '2026-08-26T02:03:00Z',
        }],
        count: 1,
      });
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    const runtime = await screen.findByRole('region', { name: '跨 App 事件运行时' });
    expect(within(runtime).getByText('微信通知精确命中等待条件')).toBeInTheDocument();
    expect(within(runtime).getByText('收到 App 通知')).toBeInTheDocument();
    expect(within(runtime).getByText('com.tencent.mm · android-companion-v1 / notification-42')).toBeInTheDocument();
    expect(within(runtime).getByText('1 个 Goal 受影响')).toBeInTheDocument();
    expect(within(runtime).getByText('已到 verified checkpoint（已验证检查点）')).toBeInTheDocument();
    expect(within(runtime).getByText('checkpoint:goal-a:step-2')).toBeInTheDocument();
    expect(within(runtime).getByText('AttentionDecision revision 8')).toBeInTheDocument();
    expect(within(runtime).getByText('恢复不是强制返回栈')).toBeInTheDocument();
    expect(within(runtime).getByText('已保存 · snapshot:wechat:fresh-9')).toBeInTheDocument();
    expect(within(runtime).getByText('设置页已完成两步，下一步读取电池详情')).toBeInTheDocument();
  });

  it.each([
    ['USER_ACTIVE', '你正在操作手机', '智能体停止下发动作，只继续保存带来源的原始观察。'],
    ['RECOVERING_CONTEXT', '正在重建手机上下文', '智能体正在读取当前前台 App 和新画面，完成前不会沿用旧截图执行。'],
    ['TAKEOVER', '你已明确接管', '空闲事件不会自动解除接管；只有你明确点击继续，智能体才恢复。'],
  ])('用日常语言解释 R7 控制模式 %s', async (controlMode, label, detail) => {
    const controlledSession = {
      ...session,
      status: 'USER_ACTIVE',
      control_mode: controlMode,
      event_runtime: {
        raw_observation_count: 3,
        latest_raw_observation: { occurred_at: '2026-08-26T03:00:00Z', foreground_package: 'com.tencent.mm' },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [controlledSession], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    const runtime = await screen.findByRole('region', { name: '跨 App 事件运行时' });
    expect(within(runtime).getAllByText(label).length).toBeGreaterThan(0);
    expect(within(runtime).getByText(detail)).toBeInTheDocument();
    if (controlMode === 'USER_ACTIVE' || controlMode === 'RECOVERING_CONTEXT') {
      expect(within(runtime).getByText(/3 条原始观察/)).toBeInTheDocument();
    } else {
      expect(screen.getByRole('button', { name: '明确交还' })).toBeEnabled();
    }
  });

  it('R1/R2 Session 没有调度投影时优雅退化', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [session], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    const attention = await screen.findByRole('region', { name: '注意力调度' });
    expect(within(attention).getByText('兼容模式')).toBeInTheDocument();
    expect(within(attention).getByText('尚未返回候选排名；当前 Session 仍可继续查看和控制。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '停止' })).toBeEnabled();
  });

  it('将补充指令的 add、revise 或 reprioritize 类型发送给 R2 API', async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [session], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      if (url.endsWith('/api/v3/sessions/session-1/messages') && init?.method === 'POST') {
        bodies.push(JSON.parse(String(init.body)));
        return json(session);
      }
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    await screen.findByRole('combobox', { name: '补充类型' });
    await user.selectOptions(screen.getByRole('combobox', { name: '补充类型' }), 'reprioritize');
    await user.type(screen.getByLabelText('补充任务'), '优先处理找工作');
    await user.click(screen.getByRole('button', { name: '保存更新' }));

    expect(bodies).toEqual([{ content: '优先处理找工作', directive_kind: 'reprioritize', client_request_id: expect.any(String) }]);
  });

  it('用一句话建立长期 Session，并显示当前目标与事件', async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [], count: 0 });
      if (url.endsWith('/api/v3/sessions') && init?.method === 'POST') {
        bodies.push(JSON.parse(String(init.body)));
        return json(session);
      }
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    await screen.findByText('还没有诊断作业');
    await user.type(screen.getByLabelText('今天的事'), session.original_instruction);
    await user.click(screen.getByRole('button', { name: '开始处理' }));

    expect((await screen.findAllByText('当前目标')).length).toBeGreaterThan(0);
    expect(screen.getAllByText(session.original_instruction).length).toBeGreaterThan(0);
    expect(screen.getByText('ACTIVE · 已绑定执行任务')).toBeInTheDocument();
    expect(screen.getByText('已绑定兼容执行任务')).toBeInTheDocument();
    expect(screen.getByText('事件 3')).toBeInTheDocument();
    expect(bodies).toEqual([{ instruction: session.original_instruction, client_request_id: expect.any(String) }]);
  });

  it('把控制请求发送到当前 Session', async () => {
    const user = userEvent.setup();
    let action = '';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [session], count: 1 });
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      if (url.endsWith('/api/v3/sessions/session-1/controls') && init?.method === 'POST') {
        action = JSON.parse(String(init.body)).action;
        return json({ ...session, status: 'STOPPED', control_mode: 'STOPPED', stopped_at: '2026-08-24T00:03:00Z' });
      }
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    await screen.findByRole('button', { name: '停止' });
    await user.click(screen.getByRole('button', { name: '停止' }));
    expect(action).toBe('stop');
    expect(await screen.findByText('已停止')).toBeInTheDocument();
  });

  it('创建网络失败后，对未改变的一句话复用同一个 client_request_id', async () => {
    const user = userEvent.setup();
    const requestIds: string[] = [];
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [], count: 0 });
      if (url.endsWith('/api/v3/sessions') && init?.method === 'POST') {
        attempts += 1;
        requestIds.push(JSON.parse(String(init.body)).client_request_id);
        if (attempts === 1) throw new Error('network unavailable');
        return json(session);
      }
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    await screen.findByText('还没有诊断作业');
    await user.type(screen.getByLabelText('今天的事'), session.original_instruction);
    await user.click(screen.getByRole('button', { name: '开始处理' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地控制服务');
    await user.click(screen.getByRole('button', { name: '开始处理' }));

    expect(await screen.findByText('事件 3')).toBeInTheDocument();
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
  });

  it('只为 WAITING_USER_FACT Goal 加载问题，失败重试复用幂等键，成功后刷新并消失', async () => {
    const user = userEvent.setup();
    const waitingSession = {
      ...session,
      active_goal_id: 'goal-other',
      goal_nodes: [
        { id: 'goal-salary', title: '回复 HR 期望薪资', status: 'WAITING_USER_FACT', bound_goal_run_id: 'run-salary' },
        { id: 'goal-other', title: '查看今天的天气', status: 'ACTIVE', bound_goal_run_id: 'run-other' },
      ],
    };
    const resumedSession = {
      ...waitingSession,
      active_goal_id: 'goal-salary',
      event_cursor: 4,
      goal_nodes: [
        { id: 'goal-salary', title: '回复 HR 期望薪资', status: 'READY', bound_goal_run_id: 'run-salary' },
        { id: 'goal-other', title: '查看今天的天气', status: 'READY', bound_goal_run_id: 'run-other' },
      ],
    };
    const need = {
      id: 'need-salary', session_id: 'session-1', goal_id: 'goal-salary',
      fact_key: 'employment.expected_salary', question: '你期望的月薪是多少？',
      why_needed: 'HR 正在等这个答案，我不能替你决定。', answer_schema: { type: 'string' },
      status: 'OPEN', resume_stage_id: 'reply-hr',
    };
    const requestIds: string[] = [];
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v3/sessions?limit=100')) return json({ items: [waitingSession], count: 1 });
      if (url.endsWith('/api/v3/sessions/session-1/fact-needs')) return json({ items: [need], count: 1 });
      if (url.endsWith('/api/v3/fact-needs/need-salary/answers') && init?.method === 'POST') {
        attempts += 1;
        const body = JSON.parse(String(init.body));
        requestIds.push(body.idempotency_key);
        expect(body.value).toBe('20k-25k');
        if (attempts === 1) throw new Error('network unavailable');
        return json({ ...need, status: 'ANSWERED' });
      }
      if (url.endsWith('/api/v3/sessions/session-1')) return json(resumedSession);
      if (url.includes('/api/v3/sessions/session-1/events?after=0&limit=50')) return json(events);
      throw new Error(`Unexpected request: ${url}`);
    }));

    render(<SessionWorkspace />);
    const questionRegion = await screen.findByRole('region', { name: '等待你的回答' });
    expect(await within(questionRegion).findByText('你期望的月薪是多少？')).toBeInTheDocument();
    expect(within(questionRegion).getByText('HR 正在等这个答案，我不能替你决定。')).toBeInTheDocument();
    expect(within(questionRegion).getByText('等待目标：回复 HR 期望薪资')).toBeInTheDocument();
    expect(within(questionRegion).getByText('只暂停缺少这个事实的 Goal；同一任务里的其他 Goal 会继续处理。')).toBeInTheDocument();

    await user.type(within(questionRegion).getByLabelText('你的回答'), '20k-25k');
    await user.click(within(questionRegion).getByRole('button', { name: '回答并继续' }));
    expect(await within(questionRegion).findByRole('alert')).toHaveTextContent('无法连接本地控制服务');
    await user.click(within(questionRegion).getByRole('button', { name: '重试保存' }));

    expect(await screen.findByText('事件 4')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '等待你的回答' })).not.toBeInTheDocument();
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
  });
});
