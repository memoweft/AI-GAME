import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChatWorkspace } from '../../frontend/src/components/ChatWorkspace';
import type { ChatSession, ChatTranscriptResponse, ChatTurn, RuntimeInfo, Target } from '../../frontend/src/types';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const androidTarget: Target = {
  id: 'target-android',
  name: 'MuMu 模拟器',
  kind: 'android',
  status: 'ready',
  address: 'emulator-5554',
  detail: 'ADB 已连接。',
  capabilities: ['adb'],
  source: 'adb',
  external_id: 'emulator-5554',
  details: { connection_type: 'emulator' },
  discovered_at: '2026-08-10T00:00:00Z',
  last_seen_at: '2026-08-10T00:00:00Z',
  updated_at: '2026-08-10T00:00:00Z',
};

function runtime(capabilities: Array<{ id: string; status: string; configured?: boolean }>): RuntimeInfo {
  return {
    overall_status: 'ready',
    capabilities: capabilities.map((item) => ({
      id: item.id,
      name: item.id,
      status: item.status,
      configured: item.configured ?? item.status === 'ready',
      detail: null,
      blocker: null,
    })),
  };
}

const localReady = runtime([{ id: 'model', status: 'ready' }]);
const cloudNotConfigured = runtime([
  { id: 'model', status: 'ready' },
  { id: 'planner', status: 'not_configured', configured: false },
  { id: 'executor', status: 'ready' },
]);
const cloudConfigured = runtime([
  { id: 'model', status: 'ready' },
  { id: 'planner', status: 'unknown', configured: true },
  { id: 'executor', status: 'ready' },
]);
const cloudError = runtime([
  { id: 'model', status: 'ready' },
  { id: 'planner', status: 'error', configured: true },
  { id: 'executor', status: 'ready' },
]);
const cloudReady = runtime([
  { id: 'model', status: 'ready' },
  { id: 'planner', status: 'ready', configured: true },
  { id: 'executor', status: 'ready' },
]);

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function session(mode: ChatSession['mode'] = 'local_chat'): ChatSession {
  return {
    id: mode === 'local_chat' ? 'session-local' : 'session-cloud',
    title: mode === 'local_chat' ? '本地对话' : '设备任务',
    mode,
    target_id: mode === 'cloud_execute' ? androidTarget.id : null,
    auto_execute: mode === 'cloud_execute',
    status: 'active',
    created_at: '2026-08-09T03:00:00Z',
    updated_at: '2026-08-09T03:00:00Z',
  };
}

function transcript(value: ChatSession, overrides: Partial<ChatTranscriptResponse> = {}): ChatTranscriptResponse {
  return {
    session: value,
    messages: [],
    turns: [],
    steps: [],
    ...overrides,
  };
}

function turn(
  value: ChatSession,
  status: ChatTurn['status'] = 'executing',
  overrides: Partial<ChatTurn> = {},
): ChatTurn {
  return {
    id: 'turn-1',
    session_id: value.id,
    mode: value.mode,
    target_id: value.target_id,
    auto_execute: value.auto_execute,
    status,
    step_count: 0,
    input_revision: 1,
    created_at: '2026-08-09T03:00:00Z',
    updated_at: '2026-08-09T03:00:01Z',
    ...overrides,
  };
}

function installChatApi(options: {
  sessions?: ChatSession[];
  transcript?: ChatTranscriptResponse;
  transcriptSequence?: ChatTranscriptResponse[];
  afterTurnTranscript?: ChatTranscriptResponse;
  createTurn?: (body: Record<string, unknown>, attempt: number) => Response;
} = {}) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const currentSessions = [...(options.sessions ?? [])];
  let currentTranscript = options.transcript;
  let transcriptRead = 0;
  let turnAttempt = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    calls.push({ url, init });
    if (url.endsWith('/chat/sessions') && method === 'GET') return json({ items: currentSessions, count: currentSessions.length });
    if (url.endsWith('/chat/sessions') && method === 'POST') {
      const body = JSON.parse(String(init?.body));
      const created: ChatSession = {
        id: body.mode === 'cloud_execute' ? 'session-cloud' : 'session-local',
        title: body.title || (body.mode === 'cloud_execute' ? '设备任务' : '本地对话'),
        mode: body.mode,
        target_id: body.target_id,
        auto_execute: body.auto_execute,
        status: 'active',
        created_at: '2026-08-09T03:00:00Z',
        updated_at: '2026-08-09T03:00:00Z',
      };
      currentSessions.unshift(created);
      currentTranscript = transcript(created);
      return json(created, 201);
    }
    if (url.includes('/chat/sessions/') && method === 'GET') {
      const sequence = options.transcriptSequence;
      if (sequence?.length) {
        currentTranscript = sequence[Math.min(transcriptRead, sequence.length - 1)];
        transcriptRead += 1;
      }
      return json(currentTranscript);
    }
    if (url.includes('/turns') && method === 'POST' && !url.endsWith('/cancel')) {
      turnAttempt += 1;
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      if (options.createTurn) {
        const response = options.createTurn(body, turnAttempt);
        if (response.ok) currentTranscript = options.afterTurnTranscript ?? currentTranscript;
        return response;
      }
      currentTranscript = options.afterTurnTranscript ?? currentTranscript;
      return json({ id: 'turn-1', session_id: currentSessions[0]?.id, status: 'completed' }, 202);
    }
    if (url.includes('/chat/turns/') && url.endsWith('/cancel') && method === 'POST') {
      return json({ id: 'turn-1', status: 'stopping' }, 202);
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls };
}

describe('对话执行工作台', () => {
  it('本地模型就绪时可创建本地直聊，发送携带安全标识并随后读取 transcript', async () => {
    const user = userEvent.setup();
    const created = session();
    const afterTurn = transcript(created, {
      messages: [{ id: 'assistant-1', session_id: created.id, turn_id: 'turn-1', role: 'assistant', content: '这是本地回复。', provider: 'local_openai_compatible', created_at: '2026-08-09T03:01:00Z' }],
      turns: [{ id: 'turn-1', session_id: created.id, mode: 'local_chat', target_id: null, auto_execute: false, status: 'completed', step_count: 0, input_revision: 1, created_at: '2026-08-09T03:01:00Z', updated_at: '2026-08-09T03:01:00Z' }],
    });
    const { calls } = installChatApi({ afterTurnTranscript: afterTurn });
    render(<ChatWorkspace targets={[androidTarget]} runtime={localReady} />);

    expect(await screen.findByLabelText('新建对话设置')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '创建对话' }));
    expect((await screen.findAllByText('本地对话')).length).toBeGreaterThan(0);
    await user.type(screen.getByLabelText('输入消息'), '你好，本地模型');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    const localReply = await screen.findByText('这是本地回复。');
    const localReplyArticle = localReply.closest('article');
    expect(localReplyArticle).not.toBeNull();
    expect(within(localReplyArticle!).getByText('本地')).toBeInTheDocument();
    expect(within(localReplyArticle!).queryByText('云端')).not.toBeInTheDocument();
    const create = calls.find((call) => call.url.endsWith('/chat/sessions') && call.init?.method === 'POST');
    expect(JSON.parse(String(create?.init?.body))).toEqual({ title: null, mode: 'local_chat', target_id: null, auto_execute: false });
    const send = calls.find((call) => call.url.endsWith('/chat/sessions/session-local/turns') && call.init?.method === 'POST');
    const payload = JSON.parse(String(send?.init?.body));
    expect(payload).toMatchObject({ content: '你好，本地模型' });
    expect(payload.client_request_id).toEqual(expect.any(String));
    expect(new Headers(send?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
    expect(calls.filter((call) => call.url.endsWith('/chat/sessions/session-local') && call.init?.method === undefined).length).toBeGreaterThan(0);
  });

  it('执行中仍可用同一发送入口补充，并从同一 Turn transcript 显示新输入', async () => {
    const user = userEvent.setup();
    const cloudSession = session('cloud_execute');
    const activeTurn = turn(cloudSession, 'executing', { input_revision: 1 });
    const running = transcript(cloudSession, {
      messages: [{
        id: 'user-1',
        session_id: cloudSession.id,
        turn_id: activeTurn.id,
        role: 'user',
        content: '先打开设置',
        delivery_status: 'applied',
        input_revision: 1,
        applied_at: '2026-08-09T03:00:01Z',
        created_at: '2026-08-09T03:00:00Z',
      }],
      turns: [activeTurn],
    });
    const afterSupplement = transcript(cloudSession, {
      messages: [
        ...running.messages,
        {
          id: 'user-2',
          session_id: cloudSession.id,
          turn_id: activeTurn.id,
          role: 'user',
          content: '再打开显示设置',
          delivery_status: 'queued',
          input_revision: 2,
          applied_at: null,
          created_at: '2026-08-09T03:00:02Z',
        },
      ],
      turns: [{ ...activeTurn, input_revision: 2, updated_at: '2026-08-09T03:00:02Z' }],
    });
    const { calls } = installChatApi({
      sessions: [cloudSession],
      transcript: running,
      afterTurnTranscript: afterSupplement,
      createTurn: () => json(afterSupplement.turns[0], 202),
    });
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudConfigured} />);

    await screen.findByText('先打开设置');
    const composer = screen.getByLabelText('输入消息');
    expect(composer).toBeEnabled();
    expect(screen.getByText('你可以继续补充，下一次规划会读取新消息。')).toBeInTheDocument();
    await user.type(composer, '再打开显示设置');
    const send = screen.getByRole('button', { name: '发送消息' });
    expect(send).toBeEnabled();
    expect(within(send).getByText('补充')).toBeInTheDocument();
    await user.click(send);

    const supplemented = await screen.findByText('再打开显示设置');
    expect(within(supplemented.closest('article')!).getByText('等待读取')).toBeInTheDocument();
    expect(afterSupplement.turns).toHaveLength(1);
    expect(afterSupplement.messages.every((message) => message.turn_id === activeTurn.id)).toBe(true);
    const posts = calls.filter((call) => call.url.endsWith(`/chat/sessions/${cloudSession.id}/turns`) && call.init?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0].init?.body))).toMatchObject({ content: '再打开显示设置' });
  });

  it('输入状态从等待读取更新为已读取，状态轮询不拉动滚动且新消息提供回到底部入口', async () => {
    vi.spyOn(Element.prototype, 'scrollHeight', 'get').mockReturnValue(600);
    vi.spyOn(Element.prototype, 'clientHeight', 'get').mockReturnValue(200);
    const cloudSession = session('cloud_execute');
    const activeTurn = turn(cloudSession, 'executing', { input_revision: 2 });
    const queuedMessage = {
      id: 'user-queued',
      session_id: cloudSession.id,
      turn_id: activeTurn.id,
      role: 'user' as const,
      content: '执行中的补充',
      delivery_status: 'queued' as const,
      input_revision: 2,
      applied_at: null,
      created_at: '2026-08-09T03:00:02Z',
    };
    const queued = transcript(cloudSession, { messages: [queuedMessage], turns: [activeTurn] });
    const applied = transcript(cloudSession, {
      messages: [{
        ...queuedMessage,
        delivery_status: 'applied',
        applied_at: '2026-08-09T03:00:03Z',
      }],
      turns: [activeTurn],
    });
    const withNewReply = transcript(cloudSession, {
      messages: [
        ...applied.messages,
        {
          id: 'assistant-new',
          session_id: cloudSession.id,
          turn_id: activeTurn.id,
          role: 'assistant',
          content: '这是新的执行进展。',
          provider: 'cloud',
          created_at: '2026-08-09T03:00:04Z',
        },
      ],
      turns: [activeTurn],
    });
    installChatApi({
      sessions: [cloudSession],
      transcriptSequence: [queued, applied, withNewReply],
    });
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudConfigured} />);

    const queuedStatus = await screen.findByText('等待读取');
    expect(queuedStatus).toHaveAttribute('title', '这条消息已加入当前任务，等待下一次规划读取。');
    const list = queuedStatus.closest('.chat-message-list') as HTMLElement;
    expect(list.scrollTop).toBe(400);
    list.scrollTop = 100;
    fireEvent.scroll(list);

    const appliedStatus = await screen.findByText('已读取', {}, { timeout: 2500 });
    expect(appliedStatus).toHaveAttribute('title', '这条消息已被规划读取；不代表设备操作成功。');
    expect(list.scrollTop).toBe(100);
    expect(screen.queryByRole('button', { name: '有新消息 ↓' })).not.toBeInTheDocument();

    await screen.findByText('这是新的执行进展。', {}, { timeout: 2500 });
    const jump = screen.getByRole('button', { name: '有新消息 ↓' });
    expect(list.scrollTop).toBe(100);
    fireEvent.click(jump);
    expect(list.scrollTop).toBe(400);
  });

  it('输入法组词时 Enter 不发送，Shift+Enter 换行，普通 Enter 发送', async () => {
    const user = userEvent.setup();
    const localSession = session();
    const { calls } = installChatApi({
      sessions: [localSession],
      transcript: transcript(localSession),
    });
    render(<ChatWorkspace targets={[androidTarget]} runtime={localReady} />);

    const composer = await screen.findByLabelText('输入消息');
    await user.click(composer);
    await user.type(composer, '输入法文字');
    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter', isComposing: true });
    expect(calls.filter((call) => call.url.endsWith('/turns') && call.init?.method === 'POST')).toHaveLength(0);
    expect(composer).toHaveValue('输入法文字');

    await user.keyboard('{Shift>}{Enter}{/Shift}');
    expect(composer).toHaveValue('输入法文字\n');
    expect(calls.filter((call) => call.url.endsWith('/turns') && call.init?.method === 'POST')).toHaveLength(0);

    await user.keyboard('{Enter}');
    await waitFor(() => expect(calls.filter((call) => call.url.endsWith('/turns') && call.init?.method === 'POST')).toHaveLength(1));
  });

  it('失败后保留草稿和请求 ID；编辑会换新 ID，未编辑重试继续复用', async () => {
    const user = userEvent.setup();
    const localSession = session();
    const bodies: Record<string, unknown>[] = [];
    installChatApi({
      sessions: [localSession],
      transcript: transcript(localSession),
      createTurn: (body, attempt) => {
        bodies.push(body);
        if (attempt === 1) throw new TypeError('connection dropped');
        if (attempt === 2) {
          return json({ error: { code: 'temporarily_unavailable', message: '发送结果暂时未知，请重试。' } }, 503);
        }
        return json(turn(localSession, 'accepted', { input_revision: 1 }), 202);
      },
    });
    render(<ChatWorkspace targets={[androidTarget]} runtime={localReady} />);

    const composer = await screen.findByLabelText('输入消息');
    await user.type(composer, '保留这份草稿');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地控制服务');
    expect(composer).toHaveValue('保留这份草稿');

    await user.type(composer, '，再补一句');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('发送结果暂时未知，请重试。');
    expect(composer).toHaveValue('保留这份草稿，再补一句');

    await user.click(screen.getByRole('button', { name: '发送消息' }));
    await waitFor(() => expect(composer).toHaveValue(''));
    await waitFor(() => expect(composer).toHaveFocus());

    expect(bodies).toHaveLength(3);
    expect(bodies[0].client_request_id).toEqual(expect.any(String));
    expect(bodies[1].client_request_id).not.toBe(bodies[0].client_request_id);
    expect(bodies[2].client_request_id).toBe(bodies[1].client_request_id);
    expect(bodies[1].content).toBe('保留这份草稿，再补一句');
    expect(bodies[2].content).toBe(bodies[1].content);
  });

  it('云端未配置时明确阻止云端会话，同时保留可用的本地直聊', async () => {
    const user = userEvent.setup();
    const { calls } = installChatApi();
    const onConfigureCloud = vi.fn();
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudNotConfigured} onConfigureCloud={onConfigureCloud} />);

    await screen.findByLabelText('新建对话设置');
    await user.click(screen.getByRole('button', { name: /云端对话 \+ 本地执行/ }));
    expect(screen.getByText('云端模型尚未配置；本地直聊仍可使用。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '现在配置' }));
    expect(onConfigureCloud).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '创建对话' })).toBeDisabled();
    expect(calls.some((call) => call.init?.method === 'POST')).toBe(false);
    await user.click(screen.getByRole('button', { name: /本地直聊/ }));
    expect(screen.getByRole('button', { name: '创建对话' })).toBeEnabled();
  });

  it('云端连接失败时不放行；runtime 恢复 ready 后无需重载工作台即可创建', async () => {
    const user = userEvent.setup();
    installChatApi();
    const { rerender } = render(<ChatWorkspace targets={[androidTarget]} runtime={cloudError} />);

    await screen.findByLabelText('新建对话设置');
    await user.click(screen.getByRole('button', { name: /云端对话 \+ 本地执行/ }));
    expect(screen.getByText('云端模型连接失败；本地直聊仍可使用。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建对话' })).toBeDisabled();

    rerender(<ChatWorkspace targets={[androidTarget]} runtime={cloudReady} />);
    expect(screen.queryByText('云端模型连接失败；本地直聊仍可使用。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建对话' })).toBeEnabled();
  });

  it('全部就绪时创建云端执行会话，默认自动执行并绑定 Android 目标', async () => {
    const user = userEvent.setup();
    const { calls } = installChatApi();
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudConfigured} />);

    await screen.findByLabelText('新建对话设置');
    await user.click(screen.getByRole('button', { name: /云端对话 \+ 本地执行/ }));
    expect(screen.getByRole('checkbox')).toBeChecked();
    await user.click(screen.getByRole('button', { name: '创建对话' }));

    expect((await screen.findAllByText('设备任务')).length).toBeGreaterThan(0);
    expect(screen.getByText('运行中也可补充指令')).toBeInTheDocument();
    expect(screen.getByText(/新消息会加入当前任务，并由下一次规划读取/)).toBeInTheDocument();
    expect(screen.getByText(/已经发送给设备的单个动作无法撤回/)).toBeInTheDocument();
    expect(screen.queryByText(/账号、验证码、实名、付款、CAPTCHA、权限、法律确认/)).not.toBeInTheDocument();
    const create = calls.find((call) => call.url.endsWith('/chat/sessions') && call.init?.method === 'POST');
    expect(JSON.parse(String(create?.init?.body))).toEqual({ title: null, mode: 'cloud_execute', target_id: 'target-android', auto_execute: true });
    expect(new Headers(create?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
  });

  it('将回复与执行时间线分开，兼容显示旧版暂停记录并保留主动停止请求', async () => {
    const user = userEvent.setup();
    const cloudSession = session('cloud_execute');
    const paused = transcript(cloudSession, {
      messages: [
        { id: 'user-1', session_id: cloudSession.id, turn_id: 'turn-1', role: 'user', content: '继续登录', created_at: '2026-08-09T03:00:00Z' },
        { id: 'assistant-1', session_id: cloudSession.id, turn_id: 'turn-1', role: 'assistant', content: '我已打开登录页。', provider: 'cloud', created_at: '2026-08-09T03:00:01Z' },
      ],
      turns: [{ id: 'turn-1', session_id: cloudSession.id, mode: 'cloud_execute', target_id: androidTarget.id, auto_execute: true, status: 'awaiting_user', step_count: 3, input_revision: 1, detail: '遇到账号凭据、验证码、实名、付款、CAPTCHA、权限、法律确认、身份核验或无法判断的页面，请你在设备上处理。', created_at: '2026-08-09T03:00:00Z', updated_at: '2026-08-09T03:00:03Z' }],
      steps: [
        { id: 'step-1', turn_id: 'turn-1', step_index: 1, state: 'observing', summary: '读取当前画面', created_at: '2026-08-09T03:00:01Z' },
        { id: 'step-2', turn_id: 'turn-1', step_index: 2, state: 'redirected', action_type: 'invalid_tool_call', summary: '动作格式无效，正在自动重新规划', created_at: '2026-08-09T03:00:02Z' },
        { id: 'step-3', turn_id: 'turn-1', step_index: 3, state: 'paused', summary: '检测到验证码', created_at: '2026-08-09T03:00:03Z' },
      ],
    });
    const { calls } = installChatApi({ sessions: [cloudSession], transcript: paused });
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudConfigured} />);

    await screen.findByText('我已打开登录页。');
    expect(screen.getByText('执行过程')).toBeInTheDocument();
    expect(screen.getByText('读取当前画面', { selector: '.chat-step strong' })).toBeInTheDocument();
    expect(screen.getByText('自动重新规划', { selector: '.chat-step strong' })).toBeInTheDocument();
    expect(screen.getByText('旧版本暂停记录')).toBeInTheDocument();
    expect(screen.getByText(/账号凭据、验证码、实名、付款、CAPTCHA、权限、法律确认、身份核验或无法判断/, { selector: '.chat-human-stop span' })).toBeInTheDocument();
    expect(screen.queryByText('必要暂停')).not.toBeInTheDocument();

    // awaiting_user is not an active loop, so exercise the stop API against a running transcript.
    const running = transcript(cloudSession, { ...paused, turns: [{ ...paused.turns[0], status: 'executing', detail: '正在执行普通操作' }] });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.endsWith('/chat/sessions')) return json({ items: [cloudSession], count: 1 });
      if (url.endsWith(`/chat/sessions/${cloudSession.id}`)) return json(running);
      if (url.endsWith('/chat/turns/turn-1/cancel')) return json({ ...running.turns[0], status: 'stopping' }, 202);
      throw new Error(`Unexpected request: ${init?.method || 'GET'} ${url}`);
    }));
    cleanup();
    render(<ChatWorkspace targets={[androidTarget]} runtime={cloudConfigured} />);
    await screen.findByRole('button', { name: '停止本轮' });
    await user.click(screen.getByRole('button', { name: '停止本轮' }));
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/chat/turns/turn-1/cancel'))).toBe(true));
    const stop = calls.find((call) => call.url.endsWith('/chat/turns/turn-1/cancel'));
    expect(new Headers(stop?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
  });
});
