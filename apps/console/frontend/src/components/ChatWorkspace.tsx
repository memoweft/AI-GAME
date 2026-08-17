import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Cloud,
  LoaderCircle,
  MessageSquare,
  MonitorSmartphone,
  Plus,
  RefreshCw,
  Send,
  Square,
} from 'lucide-react';
import { ApiError, api } from '../api';
import { formatDateTime } from '../format';
import type {
  ChatMessage,
  ChatMode,
  ChatSession,
  ChatStep,
  ChatTranscriptResponse,
  ChatTurn,
  RuntimeInfo,
  Target,
} from '../types';

interface ChatWorkspaceProps {
  targets: Target[];
  runtime: RuntimeInfo | null;
  onConfigureCloud?: () => void;
}

const ACTIVE_TURN_STATUSES = new Set([
  'accepted',
  'queued',
  'thinking',
  'planning',
  'executing',
  'stopping',
]);

const SUPPLEMENTABLE_TURN_STATUSES = new Set([
  'accepted',
  'queued',
  'thinking',
  'planning',
  'executing',
]);

const DELIVERY_STATUS_COPY = {
  queued: {
    label: '等待读取',
    title: '这条消息已加入当前任务，等待下一次规划读取。',
  },
  applied: {
    label: '已读取',
    title: '这条消息已被规划读取；不代表设备操作成功。',
  },
  rejected: {
    label: '未处理',
    title: '这条消息未被当前任务处理。',
  },
} as const;

const FOLLOW_TAIL_THRESHOLD_PX = 80;

const TURN_STATUS_LABELS: Record<string, string> = {
  accepted: '已接受',
  queued: '等待处理',
  thinking: '正在生成回复',
  planning: '云端正在规划',
  executing: '本地模型正在操作设备',
  awaiting_user: '旧版本暂停',
  stopping: '正在停止',
  completed: '本轮已完成',
  failed: '本轮失败',
  cancelled: '已停止',
};

const STEP_STATE_LABELS: Record<string, string> = {
  observed: '读取当前画面',
  observing: '读取当前画面',
  proposed: '本地模型提出动作',
  dispatched: '动作已发送',
  transported: '设备已接收',
  observed_after: '已获取动作后画面',
  verifying: '检查界面变化',
  verified: '已检查新画面',
  waiting: '等待界面响应',
  redirected: '自动重新规划',
  paused: '旧版本暂停',
  terminated: '本轮操作结束',
  completed: '执行结束',
  failed: '执行失败',
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '对话服务暂时不可用，请稍后重试。';
}

function requestId(): string {
  if (globalThis.crypto && 'randomUUID' in globalThis.crypto) {
    return globalThis.crypto.randomUUID();
  }
  return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function capabilityReady(runtime: RuntimeInfo | null, id: string): boolean {
  const capability = runtime?.capabilities.find((item) => item.id === id);
  return Boolean(capability?.configured && capability.status === 'ready');
}

function cloudRouteConfigured(runtime: RuntimeInfo | null): boolean {
  const capability = runtime?.capabilities.find((item) => item.id === 'planner');
  return Boolean(
    capability?.configured
    && ['unknown', 'ready'].includes(String(capability.status)),
  );
}

function latestTurn(turns: ChatTurn[]): ChatTurn | null {
  return [...turns].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ?? null;
}

function activeTurn(turns: ChatTurn[]): ChatTurn | null {
  return turns.find((turn) => ACTIVE_TURN_STATUSES.has(turn.status)) ?? null;
}

export function ChatWorkspace({ targets, runtime, onConfigureCloud }: ChatWorkspaceProps) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<ChatTranscriptResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [showNewMessages, setShowNewMessages] = useState(false);
  const [showNewSession, setShowNewSession] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newMode, setNewMode] = useState<ChatMode>('local_chat');
  const [newTargetId, setNewTargetId] = useState('');
  const [newAutoExecute, setNewAutoExecute] = useState(true);
  const [creating, setCreating] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const pendingRequestRef = useRef<{
    sessionId: string;
    content: string;
    clientRequestId: string;
  } | null>(null);
  const refocusAfterSendRef = useRef(false);
  const scrollSessionRef = useRef<string | null>(null);
  const lastMessageKeyRef = useRef<string | null>(null);
  const followsTailRef = useRef(true);

  const localReady = capabilityReady(runtime, 'model');
  const cloudReady = cloudRouteConfigured(runtime);
  const cloudCapability = runtime?.capabilities.find((item) => item.id === 'planner');
  const executorReady = capabilityReady(runtime, 'executor');
  const readyAndroidTargets = useMemo(
    () => targets.filter((target) => target.kind === 'android' && target.status === 'ready'),
    [targets],
  );

  const loadSessions = useCallback(async (initial = false) => {
    initial ? setLoading(true) : setRefreshing(true);
    try {
      const response = await api.getChatSessions();
      setSessions(response.items);
      setError(null);
      setSelectedId((current) => {
        if (current && response.items.some((session) => session.id === current)) return current;
        return response.items[0]?.id ?? null;
      });
      if (response.items.length === 0) setShowNewSession(true);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const loadTranscript = useCallback(async (sessionId: string, quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const response = await api.getChatSession(sessionId);
      setTranscript(response);
      setSessions((current) => current.map((session) => (
        session.id === response.session.id ? response.session : session
      )));
      setError(null);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions(true);
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedId) {
      setTranscript(null);
      return;
    }
    setTranscript(null);
    void loadTranscript(selectedId);
  }, [loadTranscript, selectedId]);

  const runningTurn = activeTurn(transcript?.turns ?? []);
  useEffect(() => {
    if (!selectedId || !runningTurn) return;
    const timer = window.setInterval(() => void loadTranscript(selectedId, true), 1000);
    return () => window.clearInterval(timer);
  }, [loadTranscript, runningTurn, selectedId]);

  useEffect(() => {
    if (!newTargetId && readyAndroidTargets.length > 0) {
      setNewTargetId(readyAndroidTargets[0].id);
    }
  }, [newTargetId, readyAndroidTargets]);

  useEffect(() => {
    pendingRequestRef.current = null;
    setComposerError(null);
    setShowNewMessages(false);
  }, [selectedId]);

  useEffect(() => {
    if (!sending && refocusAfterSendRef.current) {
      refocusAfterSendRef.current = false;
      textareaRef.current?.focus();
    }
  }, [sending]);

  const selectedSession = transcript?.session
    ?? sessions.find((session) => session.id === selectedId)
    ?? null;
  const selectedLatestTurn = latestTurn(transcript?.turns ?? []);
  const selectedSteps = selectedLatestTurn
    ? (transcript?.steps ?? []).filter((step) => step.turn_id === selectedLatestTurn.id)
    : [];
  const transcriptMessages = transcript?.messages ?? [];
  const lastMessage = transcriptMessages[transcriptMessages.length - 1];
  const messageKey = lastMessage ? `${transcriptMessages.length}:${lastMessage.id}` : 'empty';

  const scrollToTail = useCallback(() => {
    const list = messageListRef.current;
    if (!list) return;
    list.scrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
    followsTailRef.current = true;
    setShowNewMessages(false);
  }, []);

  const handleMessageListScroll = useCallback(() => {
    const list = messageListRef.current;
    if (!list) return;
    const distanceFromTail = list.scrollHeight - list.scrollTop - list.clientHeight;
    const isNearTail = distanceFromTail <= FOLLOW_TAIL_THRESHOLD_PX;
    followsTailRef.current = isNearTail;
    if (isNearTail) setShowNewMessages(false);
  }, []);

  useLayoutEffect(() => {
    const transcriptSessionId = transcript?.session.id;
    if (!messageListRef.current || !selectedId || transcriptSessionId !== selectedId) return;

    if (scrollSessionRef.current !== selectedId) {
      scrollSessionRef.current = selectedId;
      lastMessageKeyRef.current = messageKey;
      scrollToTail();
      return;
    }

    if (lastMessageKeyRef.current === messageKey) return;
    lastMessageKeyRef.current = messageKey;
    if (followsTailRef.current) {
      scrollToTail();
    } else {
      setShowNewMessages(true);
    }
  }, [messageKey, scrollToTail, selectedId, transcript?.session.id]);

  const canCreateCloud = cloudReady && localReady && executorReady && readyAndroidTargets.length > 0;
  const canCreate = newMode === 'local_chat' ? localReady : canCreateCloud;
  const composerCapabilityReady = Boolean(
    selectedSession
    && (
      selectedSession.mode === 'local_chat'
        ? localReady
        : cloudReady && localReady && executorReady
    ),
  );
  const composerLocked = Boolean(
    !composerCapabilityReady
    || sending
    || stopping
    || runningTurn?.status === 'stopping',
  );
  const canSend = Boolean(
    selectedSession
    && draft.trim()
    && !composerLocked,
  );
  const supplementing = Boolean(
    !stopping
    && runningTurn
    && SUPPLEMENTABLE_TURN_STATUSES.has(runningTurn.status),
  );

  const createSession = async () => {
    if (!canCreate) return;
    setCreating(true);
    setComposerError(null);
    try {
      const created = await api.createChatSession({
        title: newTitle.trim() || null,
        mode: newMode,
        target_id: newMode === 'cloud_execute' ? newTargetId : null,
        auto_execute: newMode === 'cloud_execute' ? newAutoExecute : false,
      });
      setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedId(created.id);
      setNewTitle('');
      setShowNewSession(false);
    } catch (nextError) {
      setComposerError(errorText(nextError));
    } finally {
      setCreating(false);
    }
  };

  const sendMessage = async () => {
    if (!selectedSession || !canSend) return;
    const content = draft.trim();
    const pendingRequest = pendingRequestRef.current;
    const clientRequestId = pendingRequest
      && pendingRequest.sessionId === selectedSession.id
      && pendingRequest.content === content
      ? pendingRequest.clientRequestId
      : requestId();
    pendingRequestRef.current = {
      sessionId: selectedSession.id,
      content,
      clientRequestId,
    };
    setSending(true);
    setComposerError(null);
    let accepted = false;
    try {
      await api.createChatTurn(selectedSession.id, {
        content,
        client_request_id: clientRequestId,
      });
      accepted = true;
      pendingRequestRef.current = null;
      setDraft('');
      refocusAfterSendRef.current = true;
    } catch (nextError) {
      setComposerError(errorText(nextError));
    } finally {
      setSending(false);
    }
    if (accepted) {
      await loadTranscript(selectedSession.id, true);
      await loadSessions();
    }
  };

  const stopTurn = async () => {
    if (!runningTurn || stopping) return;
    setStopping(true);
    setComposerError(null);
    try {
      await api.cancelChatTurn(runningTurn.id);
      if (selectedId) await loadTranscript(selectedId, true);
    } catch (nextError) {
      setComposerError(errorText(nextError));
    } finally {
      setStopping(false);
    }
  };

  if (loading) {
    return (
      <section className="chat-loading panel" aria-label="正在加载对话">
        <LoaderCircle className="spin" size={24} />
        <div><strong>正在打开对话工作台</strong><span>读取本机会话与模型状态…</span></div>
      </section>
    );
  }

  return (
    <div className="chat-workspace">
      <aside className="chat-session-panel panel">
        <div className="chat-session-heading">
          <div><span className="eyebrow">会话</span><h2>对话记录</h2></div>
          <button className="icon-button" aria-label="刷新对话" title="刷新对话" onClick={() => void loadSessions()} disabled={refreshing}>
            <RefreshCw size={17} className={refreshing ? 'spin' : ''} />
          </button>
        </div>
        <button className="button button-primary chat-new-button" onClick={() => setShowNewSession((value) => !value)}>
          <Plus size={17} /> 新建对话
        </button>

        {showNewSession && (
          <div className="chat-new-session" aria-label="新建对话设置">
            <label>
              <span>对话名称</span>
              <input value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="可留空自动命名" maxLength={120} />
            </label>
            <fieldset className="chat-mode-picker">
              <legend>模式</legend>
              <button type="button" className={newMode === 'local_chat' ? 'selected' : ''} onClick={() => setNewMode('local_chat')}>
                <Bot size={17} /><span><strong>本地直聊</strong><small>只回复，不操作设备</small></span>
              </button>
              <button type="button" className={newMode === 'cloud_execute' ? 'selected' : ''} onClick={() => setNewMode('cloud_execute')}>
                <Cloud size={17} /><span><strong>云端对话 + 本地执行</strong><small>持续自动控制</small></span>
              </button>
            </fieldset>
            {newMode === 'cloud_execute' && (
              <>
                <label>
                  <span>操作目标</span>
                  <select value={newTargetId} onChange={(event) => setNewTargetId(event.target.value)}>
                    {readyAndroidTargets.length === 0 && <option value="">没有就绪的 Android 设备</option>}
                    {readyAndroidTargets.map((target) => <option key={target.id} value={target.id}>{target.name}</option>)}
                  </select>
                </label>
                <label className="chat-auto-toggle">
                  <input type="checkbox" checked={newAutoExecute} onChange={(event) => setNewAutoExecute(event.target.checked)} />
                  <span><strong>自动操作设备</strong><small>不设单轮步数上限，持续执行到任务结束或你主动停止</small></span>
                </label>
              </>
            )}
            {!canCreate && (
              <div className="chat-config-warning">
                <AlertTriangle size={15} />
                <span>{newMode === 'local_chat'
                  ? '本地 GUI-Owl 当前不可用。'
                  : !cloudReady
                    ? cloudCapability?.status === 'error'
                      ? '云端模型连接失败；本地直聊仍可使用。'
                      : '云端模型尚未配置；本地直聊仍可使用。'
                    : !executorReady
                      ? '设备执行器尚未就绪。'
                      : '没有可操作的 Android 目标。'}</span>
                {newMode === 'cloud_execute' && !cloudReady && onConfigureCloud && (
                  <button type="button" className="text-button" onClick={onConfigureCloud}>现在配置</button>
                )}
              </div>
            )}
            <button className="button button-primary" onClick={() => void createSession()} disabled={!canCreate || creating}>
              {creating ? <LoaderCircle className="spin" size={16} /> : <MessageSquare size={16} />}
              创建对话
            </button>
          </div>
        )}

        <div className="chat-session-list" role="list" aria-label="历史对话">
          {sessions.length === 0 && !showNewSession && <p className="chat-empty-copy">还没有对话。</p>}
          {sessions.map((session) => (
            <button
              key={session.id}
              role="listitem"
              className={`chat-session-item ${selectedId === session.id ? 'active' : ''}`}
              onClick={() => setSelectedId(session.id)}
            >
              <span className="chat-session-icon">{session.mode === 'local_chat' ? <Bot size={17} /> : <Cloud size={17} />}</span>
              <span><strong>{session.title}</strong><small>{session.mode === 'local_chat' ? '本地直聊' : '云端 + 本地执行'} · {formatDateTime(session.updated_at)}</small></span>
            </button>
          ))}
        </div>
      </aside>

      <section className="chat-conversation-panel panel">
        {!selectedSession ? (
          <div className="chat-empty-state">
            <MessageSquare size={34} />
            <h2>开始一段对话</h2>
            <p>选择本地直聊，或让云端模型负责对话、由 GUI-Owl 自动操作设备。</p>
            <button className="button button-primary" onClick={() => setShowNewSession(true)}><Plus size={17} /> 新建对话</button>
          </div>
        ) : (
          <>
            <header className="chat-conversation-header">
              <div>
                <span className="eyebrow">{selectedSession.mode === 'local_chat' ? '本地直聊' : '云端对话 + 本地执行'}</span>
                <h2>{selectedSession.title}</h2>
                <p>{selectedSession.mode === 'local_chat'
                  ? '回复由本地 GUI-Owl 生成，本会话不会读取或操作设备。'
                  : selectedSession.auto_execute
                    ? '云端模型负责对话与目标；本地 GUI-Owl 持续观察屏幕并自动操作设备。'
                    : '云端模型负责对话；当前会话未启用设备自动操作。'}</p>
              </div>
              {runningTurn && (
                <button className="button button-danger" onClick={() => void stopTurn()} disabled={stopping || runningTurn.status === 'stopping'}>
                  {stopping || runningTurn.status === 'stopping' ? <LoaderCircle className="spin" size={16} /> : <Square size={15} />}
                  {stopping || runningTurn.status === 'stopping' ? '正在停止' : '停止本轮'}
                </button>
              )}
            </header>

            {selectedSession.mode === 'cloud_execute' && (
              <div className="chat-automation-notice">
                <MonitorSmartphone size={18} />
                <div><strong>运行中也可补充指令</strong><span>新消息会加入当前任务，并由下一次规划读取。已经发送给设备的单个动作无法撤回；如需阻止后续动作，请停止本轮。</span></div>
              </div>
            )}

            <div className="chat-message-region">
              <div
                ref={messageListRef}
                className="chat-message-list"
                aria-live="polite"
                onScroll={handleMessageListScroll}
              >
                {transcriptMessages.length === 0 && (
                  <div className="chat-message-empty">
                    <SparkMessage mode={selectedSession.mode} />
                  </div>
                )}
                {transcriptMessages.map((message) => <MessageBubble key={message.id} message={message} mode={selectedSession.mode} />)}
                {runningTurn && (
                  <div className="chat-working-row">
                    <LoaderCircle className="spin" size={17} />
                    <div>
                      <strong>{TURN_STATUS_LABELS[runningTurn.status] ?? runningTurn.detail ?? '正在处理…'}</strong>
                      <span>{supplementing
                        ? '你可以继续补充，下一次规划会读取新消息。'
                        : '本轮正在停止，暂时不能补充新消息。'}</span>
                    </div>
                  </div>
                )}
              </div>
              {showNewMessages && (
                <button type="button" className="chat-new-messages-button" onClick={scrollToTail}>
                  有新消息 ↓
                </button>
              )}
            </div>

            {selectedLatestTurn?.status === 'awaiting_user' && (
              <div className="chat-human-stop" role="status">
                <AlertTriangle size={19} />
                <div><strong>旧版本暂停记录</strong><span>{selectedLatestTurn.detail || '这是旧版本留下的人工暂停记录；新指令会使用持续自动控制。'}</span></div>
              </div>
            )}

            <div className="chat-composer">
              {composerError && <p className="chat-composer-error" role="alert"><AlertTriangle size={15} />{composerError}</p>}
              <textarea
                ref={textareaRef}
                aria-label="输入消息"
                value={draft}
                onChange={(event) => {
                  setDraft(event.target.value);
                  pendingRequestRef.current = null;
                  setComposerError(null);
                }}
                onKeyDown={(event) => {
                  if (
                    event.key === 'Enter'
                    && !event.shiftKey
                    && !event.nativeEvent.isComposing
                    && event.keyCode !== 229
                  ) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
                placeholder={selectedSession.mode === 'local_chat' ? '直接问本地模型…' : '告诉云端模型你想让设备完成什么…'}
                maxLength={10_000}
                disabled={composerLocked}
              />
              <div className="chat-composer-footer">
                <span>{supplementing
                  ? '继续输入即可；下一次规划会读取 · Shift+Enter 换行'
                  : selectedSession.mode === 'local_chat'
                    ? 'Enter 发送 · Shift+Enter 换行'
                    : 'Enter 发送 · Shift+Enter 换行 · 可随时停止本轮'}</span>
                <button className="button button-primary" aria-label="发送消息" onClick={() => void sendMessage()} disabled={!canSend}>
                  {sending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}
                  {supplementing ? '补充' : '发送'}
                </button>
              </div>
            </div>
          </>
        )}
      </section>

      <aside className="chat-timeline-panel panel">
        <div className="chat-timeline-heading">
          <span className="eyebrow">执行过程</span>
          <h2>实时步骤</h2>
          <p>{selectedSession?.mode === 'cloud_execute' ? '回复和设备执行分别显示。' : '本地直聊不操作设备。'}</p>
        </div>
        {selectedSession?.mode !== 'cloud_execute' ? (
          <div className="chat-timeline-empty"><Bot size={22} /><strong>只生成回复</strong><span>本地直聊不会截图或发送 ADB 动作。</span></div>
        ) : selectedSteps.length === 0 ? (
          <div className="chat-timeline-empty"><MonitorSmartphone size={22} /><strong>尚未开始操作</strong><span>发送设备任务后，这里会依次显示观察、动作与检查结果。</span></div>
        ) : (
          <ol className="chat-step-list">
            {selectedSteps.map((step) => <TimelineStep key={step.id} step={step} />)}
          </ol>
        )}
        {selectedLatestTurn && (
          <div className={`chat-turn-summary status-${selectedLatestTurn.status}`}>
            {selectedLatestTurn.status === 'completed' ? <CheckCircle2 size={17} /> : selectedLatestTurn.status === 'failed' ? <AlertTriangle size={17} /> : <LoaderCircle size={17} className={ACTIVE_TURN_STATUSES.has(selectedLatestTurn.status) ? 'spin' : ''} />}
            <div><strong>{TURN_STATUS_LABELS[selectedLatestTurn.status] ?? selectedLatestTurn.status}</strong><span>{selectedLatestTurn.detail || `已记录 ${selectedLatestTurn.step_count} 个执行步骤`}</span></div>
          </div>
        )}
      </aside>

      {error && <div className="chat-global-error" role="alert"><AlertTriangle size={17} />{error}</div>}
    </div>
  );
}

function SparkMessage({ mode }: { mode: ChatMode }) {
  return (
    <>
      {mode === 'local_chat' ? <Bot size={28} /> : <Cloud size={28} />}
      <strong>{mode === 'local_chat' ? '直接和 GUI-Owl 对话' : '对话并操作设备'}</strong>
      <span>{mode === 'local_chat' ? '可以询问模型能力、界面知识或任何文本问题。' : '描述目标即可；云端负责理解，本地模型负责看屏幕和执行。'}</span>
    </>
  );
}

function MessageBubble({ message, mode }: { message: ChatMessage; mode: ChatMode }) {
  const assistant = message.role === 'assistant';
  const delivery = message.role === 'user' && message.delivery_status
    ? DELIVERY_STATUS_COPY[message.delivery_status]
    : null;
  return (
    <article className={`chat-message chat-message-${message.role}`}>
      <div className="chat-message-avatar">{assistant ? <Bot size={17} /> : message.role === 'system' ? <MonitorSmartphone size={17} /> : '你'}</div>
      <div className="chat-message-body">
        <div className="chat-message-meta">
          <strong>{assistant ? '模型' : message.role === 'system' ? '系统' : '你'}</strong>
          {assistant && message.provider && <span>{mode === 'local_chat' ? '本地' : '云端'}</span>}
          {delivery && (
            <span
              className={`chat-message-delivery delivery-${message.delivery_status}`}
              title={delivery.title}
            >
              {delivery.label}
            </span>
          )}
          <time>{formatDateTime(message.created_at)}</time>
        </div>
        <p>{message.content}</p>
      </div>
    </article>
  );
}

function TimelineStep({ step }: { step: ChatStep }) {
  return (
    <li className={`chat-step chat-step-${step.state}`}>
      <span className="chat-step-index">{step.step_index}</span>
      <div><strong>{STEP_STATE_LABELS[step.state] ?? step.action_type ?? step.state}</strong><span>{step.summary}</span><time>{formatDateTime(step.created_at)}</time></div>
    </li>
  );
}
