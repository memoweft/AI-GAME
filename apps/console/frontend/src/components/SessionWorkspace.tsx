import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  BellRing,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Eye,
  GitBranch,
  MessageSquarePlus,
  RefreshCw,
  RotateCw,
  Send,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Square,
  UserRound,
} from 'lucide-react';
import { ApiError, api, newIdempotencyKey } from '../api';
import { formatDateTime } from '../format';
import type {
  AgentSession,
  SessionAttentionCandidate,
  SessionContinuation,
  SessionControlAction,
  SessionDirectiveKind,
  SessionEvent,
  SessionGoalNode,
  SessionWakeCondition,
  NeedUserFact,
} from '../types';
import { EmptyState } from './EmptyState';
import { QuestionCard } from './QuestionCard';

const ACTIVE_SESSION_KEY = 'ai-game.active-session-id';
const POLL_MS = 1500;
const TERMINAL = new Set(['STOPPED', 'COMPLETED', 'PARTIAL', 'FAILED']);

const STATUS: Record<string, { label: string; tone: string; detail: string }> = {
  ACCEPTED: { label: '已接收', tone: 'neutral', detail: '这件事已经记住，正在建立第一个执行目标。' },
  PLANNING: { label: '正在安排', tone: 'info', detail: '正在准备当前最先处理的事情。' },
  ACTIVE: { label: '正在处理', tone: 'info', detail: '智能体正在继续当前目标。' },
  USER_ACTIVE: { label: '你正在接管', tone: 'warning', detail: '智能体先观察你的操作，之后可以继续。' },
  WAITING_ALL: { label: '正在等待', tone: 'warning', detail: '当前目标正在等设备、事件或新的事实。' },
  STOPPING: { label: '正在停止', tone: 'warning', detail: '正在停止当前执行并保留已经发生的事实。' },
  STOPPED: { label: '已停止', tone: 'neutral', detail: '这件事已停止，历史和原始指令仍然保留。' },
  COMPLETED: { label: '已完成', tone: 'success', detail: '当前 Session 的结果已经确认。' },
  PARTIAL: { label: '部分完成', tone: 'warning', detail: '已经确认部分结果，仍有未完成事项。' },
  FAILED: { label: '未能完成', tone: 'danger', detail: '执行无法继续，已保留相关事实。' },
};

const CONTROL_MODE: Record<string, { label: string; tone: string; detail: string }> = {
  AGENT_ACTIVE: { label: '智能体正在操作', tone: 'info', detail: '当前 Goal 可以继续观察、思考和执行动作。' },
  USER_ACTIVE: { label: '你正在操作手机', tone: 'warning', detail: '智能体停止下发动作，只继续保存带来源的原始观察。' },
  RECOVERING_CONTEXT: { label: '正在重建手机上下文', tone: 'info', detail: '智能体正在读取当前前台 App 和新画面，完成前不会沿用旧截图执行。' },
  TAKEOVER: { label: '你已明确接管', tone: 'warning', detail: '空闲事件不会自动解除接管；只有你明确点击继续，智能体才恢复。' },
  STOPPING: { label: '正在安全停止', tone: 'warning', detail: '当前动作会先结算，已经形成的事实和恢复点会保留。' },
  STOPPED: { label: '已经停止', tone: 'neutral', detail: '历史事件和恢复点仍然保留。' },
};

const EXTERNAL_EVENT_TYPES = new Set([
  'NotificationPostedEvent',
  'NotificationRemovedEvent',
  'ForegroundApplicationChangedEvent',
  'HumanTouchStartedEvent',
  'HumanTouchEndedEvent',
  'HumanIdleEvent',
  'TimerDueEvent',
  'ScreenStateChangedEvent',
  'LockStateChangedEvent',
  'NetworkStateChangedEvent',
  'CompanionConnectedEvent',
  'CompanionDisconnectedEvent',
  'BodyEvent',
]);

function toMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '请求没有完成，请稍后重试。';
}

function sessionMeta(session: AgentSession) {
  if (session.control_mode === 'USER_ACTIVE' || session.control_mode === 'RECOVERING_CONTEXT' || session.control_mode === 'TAKEOVER') {
    return CONTROL_MODE[session.control_mode];
  }
  return STATUS[session.status] ?? { label: session.status, tone: 'neutral', detail: '状态由本地服务报告。' };
}

function eventLabel(event: SessionEvent): string {
  const labels: Record<string, string> = {
    session_created: '创建了长期任务',
    directive_recorded: '记录了一条补充指令',
    goal_node_created: '建立了当前目标',
    goal_activation_requested: '请求启动当前目标',
    goal_run_bound: '已绑定兼容执行任务',
    goal_projection_changed: '当前目标状态已更新',
    attention_decided: '选择了当前最值得处理的目标',
    attention_decision_recorded: '保存了注意力决策',
    attention_decision_committed: '已确定现在优先处理的目标',
    NotificationPostedEvent: '收到 App 通知',
    NotificationRemovedEvent: 'App 通知已移除',
    ForegroundApplicationChangedEvent: '前台 App 已变化',
    HumanTouchStartedEvent: '检测到你开始操作手机',
    HumanTouchEndedEvent: '检测到本次触摸结束',
    HumanIdleEvent: '检测到你已空闲',
    TimerDueEvent: '时间条件已经到达',
    ScreenStateChangedEvent: '屏幕状态已变化',
    CompanionConnectedEvent: '旧设备桥接已连接',
    CompanionDisconnectedEvent: '旧设备桥接已断开',
    BodyEvent: '收到手机事件',
    continuation_saved: '保存了目标续接点',
    session_event_received: '收到一条调度事件',
    goal_woken: '等待中的目标已被唤醒',
    control_requested: '收到控制请求',
    control_settled: '控制请求已完成',
    session_stopped: '任务已停止',
  };
  return labels[event.event_type] ?? event.event_type;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function firstText(source: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : [];
}

function routingData(event: SessionEvent | null): Record<string, unknown> {
  return record(event ? event.data.routing : null);
}

function eventAffectedGoalIds(event: SessionEvent | null): string[] {
  if (!event) return [];
  const direct = event.affected_goal_ids ?? stringList(event.data.affected_goal_ids);
  return direct.length ? direct : stringList(routingData(event).affected_goal_ids);
}

function eventSource(event: SessionEvent): string {
  const facts = record(event.data.facts);
  const routing = routingData(event);
  const application = firstText(event.data, ['application_package', 'package_name', 'source_app', 'foreground_package'])
    ?? firstText(facts, ['application_package', 'package_name', 'source_app', 'foreground_package'])
    ?? firstText(routing, ['source_app', 'application_package', 'package_name']);
  const source = [event.source_namespace, event.source_event_id].filter(Boolean).join(' / ');
  if (application && source) return `${application} · ${source}`;
  return application || source || '来源 App 尚未返回';
}

function latestRuntimeEvent(session: AgentSession, events: SessionEvent[]): SessionEvent | null {
  const triggerId = session.attention_decision?.trigger_event_id
    ?? session.event_runtime?.pending_preemption_event_id;
  if (triggerId) {
    const trigger = events.find((event) => event.id === triggerId);
    if (trigger) return trigger;
  }
  return events.find((event) => (
    EXTERNAL_EVENT_TYPES.has(event.event_type)
    || Boolean(event.source_namespace)
    || eventAffectedGoalIds(event).length > 0
  )) ?? null;
}

function latestSwitchContinuation(session: AgentSession, selectedGoalId: string | null): SessionContinuation | null {
  const decision = session.attention_decision;
  const ordered = [...(session.continuations ?? [])].sort((left, right) => (
    (right.created_at ?? '').localeCompare(left.created_at ?? '')
  ));
  return ordered.find((item) => item.checkpoint_ref === decision?.preemption_checkpoint_ref)
    ?? ordered.find((item) => item.attention_decision_id != null && item.attention_decision_id === decision?.id)
    ?? ordered.find((item) => item.goal_id !== selectedGoalId && item.goal_node_id !== selectedGoalId && ['PREEMPTED', 'USER_ACTIVITY'].includes(item.yield_reason ?? ''))
    ?? null;
}

function controlState(session: AgentSession) {
  return CONTROL_MODE[session.control_mode]
    ?? { label: session.control_mode, tone: 'neutral', detail: '当前控制模式由本地运行时报告。' };
}

function freshObservationRef(session: AgentSession, continuation: SessionContinuation | null): string | null {
  if (session.event_runtime?.recovery_context_ref) return session.event_runtime.recovery_context_ref;
  const conditions = continuation?.resume_preconditions ?? {};
  return firstText(conditions, ['fresh_observation_ref', 'fresh_snapshot_ref', 'recovery_context_ref']);
}

function activeGoal(session: AgentSession): SessionGoalNode | null {
  const selectedGoalId = session.attention_decision?.selected_goal_id ?? session.active_goal_id;
  return (session.goal_nodes ?? []).find((goal) => goal.id === selectedGoalId) ?? null;
}

function criterionText(criterion: NonNullable<SessionGoalNode['criteria']>[number]): string {
  return criterion.description;
}

function candidateScore(candidate: SessionAttentionCandidate): number | null {
  const score = candidate.score ?? candidate.attention_score ?? candidate.total_score;
  return typeof score === 'number' && Number.isFinite(score) ? Math.round(score) : null;
}

function candidateGoalId(candidate: SessionAttentionCandidate): string {
  return candidate.goal_node_id ?? candidate.goal_id ?? 'unknown-goal';
}

function candidateEligibility(candidate: SessionAttentionCandidate): string {
  if (candidate.eligibility) return candidate.eligibility;
  if (candidate.eligible === true) return 'ELIGIBLE';
  if (candidate.eligible === false) return 'INELIGIBLE';
  return '未返回';
}

function wakeFor(session: AgentSession, node: SessionGoalNode): SessionWakeCondition | null {
  return node.wake_condition
    ?? session.wake_conditions?.find((condition) => (condition.goal_node_id ?? condition.goal_id) === node.id)
    ?? null;
}

function continuationFor(session: AgentSession, node: SessionGoalNode): SessionContinuation | null {
  if (node.continuation) return node.continuation;
  const matches = (session.continuations ?? []).filter((continuation) => (continuation.goal_node_id ?? continuation.goal_id) === node.id);
  return matches.sort((left, right) => (right.revision ?? 0) - (left.revision ?? 0))[0] ?? null;
}

function continuationSummary(continuation: SessionContinuation | null): string {
  if (!continuation) return '尚未保存续接点';
  if (continuation.summary || continuation.next_step || continuation.reason) {
    return continuation.summary || continuation.next_step || continuation.reason || '已保存续接点';
  }
  const stage = continuation.current_stage_id || continuation.stage_id;
  if (continuation.waiting_kind || continuation.waiting_ref) {
    return [continuation.waiting_kind, continuation.waiting_ref].filter(Boolean).join(' · ');
  }
  return stage || continuation.yield_reason || '已保存续接点';
}

function attentionBasis(session: AgentSession): string {
  const decision = session.attention_decision;
  if (!decision) return '尚未生成调度决策；当前目标沿用早期 Session 状态。';
  if (Array.isArray(decision.basis)) return decision.basis.join(' · ');
  if (decision.basis) return decision.basis;
  return decision.trigger_kind || decision.trigger_source || decision.trigger_key || (decision.trigger_event_id ? `事件 ${decision.trigger_event_id}` : '常规调度');
}

export function SessionWorkspace() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [active, setActive] = useState<AgentSession | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [instruction, setInstruction] = useState('');
  const [pendingCreate, setPendingCreate] = useState<{ instruction: string; clientRequestId: string } | null>(null);
  const [message, setMessage] = useState('');
  const [directiveKind, setDirectiveKind] = useState<SessionDirectiveKind>('add');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [factNeeds, setFactNeeds] = useState<NeedUserFact[]>([]);

  const loadFactNeeds = useCallback(async (session: AgentSession) => {
    const waitsForUserFact = session.goal_nodes.some((node) => node.status === 'WAITING_USER_FACT');
    if (!waitsForUserFact) {
      setFactNeeds([]);
      return;
    }
    const result = await api.getSessionFactNeeds(session.id);
    setFactNeeds(result.items.filter((need) => need.status === 'OPEN'));
  }, []);

  const merge = useCallback((next: AgentSession) => {
    setActive(next);
    setSessions((current) => [next, ...current.filter((item) => item.id !== next.id)]);
    window.localStorage.setItem(ACTIVE_SESSION_KEY, next.id);
  }, []);

  const loadEvents = useCallback(async (session: AgentSession) => {
    let after = 0;
    const collected: SessionEvent[] = [];
    // The v3 page cursor is append-only. Follow it when supplied, while still
    // accepting the initial R1 implementation that returns a single page.
    for (;;) {
      const result = await api.getSessionEvents(session.id, after, 50);
      collected.push(...result.items);
      const next = result.next_cursor;
      if (next == null || next <= after || result.items.length === 0) break;
      after = next;
    }
    setEvents(collected);
  }, []);

  const load = useCallback(async () => {
    try {
      const result = await api.getSessions(100);
      setSessions(result.items);
      const remembered = window.localStorage.getItem(ACTIVE_SESSION_KEY);
      const selected = result.items.find((item) => item.id === remembered) ?? result.items[0] ?? null;
      setActive(selected);
      if (selected) {
        window.localStorage.setItem(ACTIVE_SESSION_KEY, selected.id);
        try { await loadEvents(selected); } catch { setEvents(selected.events ?? []); }
      } else {
        setEvents([]);
      }
      setError(null);
    } catch (caught) {
      setError(toMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [loadEvents]);

  useEffect(() => { void load(); }, [load]);

  const activeWaitsForUserFact = active?.goal_nodes.some((node) => node.status === 'WAITING_USER_FACT') ?? false;
  useEffect(() => {
    if (!active || !activeWaitsForUserFact) {
      setFactNeeds([]);
      return;
    }
    void loadFactNeeds(active).catch((caught) => setError(toMessage(caught)));
  }, [active?.id, activeWaitsForUserFact, loadFactNeeds]);

  useEffect(() => {
    if (!active || TERMINAL.has(active.status)) return;
    const id = window.setInterval(() => {
      void api.getSession(active.id).then(async (next) => {
        merge(next);
        await Promise.all([loadEvents(next), loadFactNeeds(next)]);
      }).catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [active, loadEvents, loadFactNeeds, merge]);

  const select = async (session: AgentSession) => {
    merge(session);
    try {
      const detail = await api.getSession(session.id);
      merge(detail);
      await loadEvents(detail);
    } catch (caught) {
      setError(toMessage(caught));
    }
  };

  const create = async () => {
    const normalized = instruction.trim();
    if (!normalized) return;
    setCreating(true);
    setError(null);
    const clientRequestId = pendingCreate?.instruction === normalized
      ? pendingCreate.clientRequestId
      : newIdempotencyKey();
    try {
      const next = await api.createSession(normalized, clientRequestId);
      merge(next);
      setInstruction('');
      setPendingCreate(null);
      try { await loadEvents(next); } catch { setEvents(next.events ?? []); }
    } catch (caught) {
      setPendingCreate({ instruction: normalized, clientRequestId });
      setError(toMessage(caught));
    } finally {
      setCreating(false);
    }
  };

  const control = async (action: SessionControlAction) => {
    if (!active) return;
    setWorking(true);
    setError(null);
    try {
      const next = await api.controlSession(active.id, action);
      merge(next);
      await loadEvents(next);
    } catch (caught) {
      setError(toMessage(caught));
    } finally {
      setWorking(false);
    }
  };

  const addMessage = async () => {
    if (!active || !message.trim()) return;
    setWorking(true);
    setError(null);
    try {
      const next = await api.sendSessionMessage(active.id, message.trim(), directiveKind);
      merge(next);
      setMessage('');
      await loadEvents(next);
    } catch (caught) {
      setError(toMessage(caught));
    } finally {
      setWorking(false);
    }
  };

  const answerFactNeed = async (need: NeedUserFact, value: string, idempotencyKey: string) => {
    if (!active) return;
    await api.answerFactNeed(need.id || need.need_id || '', {
      value,
      idempotency_key: idempotencyKey,
    });
    const refreshed = await api.getSession(active.id);
    merge(refreshed);
    await Promise.all([
      loadEvents(refreshed),
      loadFactNeeds(refreshed),
    ]);
  };

  const status = active ? sessionMeta(active) : null;
  const goal = active ? activeGoal(active) : null;
  const goals = active?.goal_nodes ?? [];
  const selectedGoalId = active?.attention_decision?.selected_goal_id ?? active?.active_goal_id ?? null;
  const attentionCandidates = useMemo<SessionAttentionCandidate[]>(() => {
    if (!active) return [];
    const projected = active.attention_candidates ?? active.attention_decision?.candidates;
    if (projected?.length) {
      return [...projected].sort((left, right) => (left.rank ?? Number.MAX_SAFE_INTEGER) - (right.rank ?? Number.MAX_SAFE_INTEGER));
    }
    const hasNodeScheduling = active.goal_nodes.some((node) => (
      node.attention_score != null || node.eligibility != null || node.eligible != null
    ));
    if (!hasNodeScheduling) return [];
    return active.goal_nodes
      .map((node) => ({
        goal_node_id: node.id,
        attention_score: node.attention_score ?? undefined,
        eligible: node.eligible,
        eligibility: node.eligibility ?? undefined,
        eligibility_reason: node.eligibility_reason,
        hard_tier: node.hard_tier,
        scheduling_class: node.scheduling_class,
      }))
      .sort((left, right) => (right.attention_score ?? Number.NEGATIVE_INFINITY) - (left.attention_score ?? Number.NEGATIVE_INFINITY))
      .map((candidate, index) => ({ ...candidate, rank: index + 1 }));
  }, [active]);
  const selectedContinuation = active
    ? (goal ? continuationFor(active, goal) : null)
      ?? [...(active.continuations ?? [])].sort((left, right) => (right.created_at ?? '').localeCompare(left.created_at ?? ''))[0]
      ?? null
    : null;
  const graphRevision = active?.goal_graph_revision ?? active?.authority_revision ?? 1;
  const canControl = Boolean(active && !TERMINAL.has(active.status));
  const orderedEvents = useMemo(() => [...events].sort((left, right) => right.cursor - left.cursor), [events]);
  const runtimeEvent = active ? latestRuntimeEvent(active, orderedEvents) : null;
  const affectedGoalIds = eventAffectedGoalIds(runtimeEvent);
  const affectedGoalNames = affectedGoalIds.map((goalId) => (
    goals.find((item) => item.id === goalId)?.title ?? goalId
  ));
  const switchContinuation = active ? latestSwitchContinuation(active, selectedGoalId) : null;
  const checkpointRef = active?.attention_decision?.preemption_checkpoint_ref
    ?? switchContinuation?.checkpoint_ref
    ?? null;
  const routeReason = active
    ? firstText(routingData(runtimeEvent), ['reason', 'explanation', 'route_reason'])
      ?? active.event_runtime?.pending_preemption_reason
      ?? active.attention_decision?.reason
      ?? null
    : null;
  const runtimeControl = active ? controlState(active) : null;
  const observationRef = active ? freshObservationRef(active, switchContinuation ?? selectedContinuation) : null;
  const sourceApplication = runtimeEvent
    ? firstText(runtimeEvent.data, ['application_package', 'package_name', 'source_app', 'foreground_package'])
      ?? firstText(record(runtimeEvent.data.facts), ['application_package', 'package_name', 'source_app', 'foreground_package'])
    : null;
  const selectedApplication = goal?.application_hint ?? switchContinuation?.application_package ?? null;
  const latestRawObservation = active?.event_runtime?.latest_raw_observation ?? null;
  const latestRawObservationAt = firstText(record(latestRawObservation), ['occurred_at', 'observed_at', 'created_at']);

  return (
    <section className="agent-workspace goal-workspace" aria-label="执行作业诊断">
      <section className="panel agent-composer goal-composer">
        <div className="goal-composer-orbit" aria-hidden="true"><span /><span /><Smartphone size={28} /></div>
        <div className="agent-composer-heading"><div className="agent-composer-icon"><Sparkles size={22} /></div><div className="goal-composer-copy"><span className="eyebrow">开发者执行诊断</span><h2>提交一条模拟器执行测试指令</h2><p>正式用户在 WeftMate 中与 Harness 对话；这里仅验证 AI-GAME 的执行、持久化、事件和恢复能力。</p></div></div>
        <div className="goal-capability-strip" aria-label="诊断特点"><span>检查持久化</span><span>逐步观察验证</span><span>随时可以停止</span></div>
        <label className="agent-goal-field"><span>今天的事</span><textarea value={instruction} onChange={(event) => { const next = event.target.value; setInstruction(next); if (pendingCreate?.instruction !== next.trim()) setPendingCreate(null); }} placeholder="例如：打开设置看看电池，然后回到桌面。" rows={3} /></label>
        <div className="goal-composer-footer"><div className="goal-promise"><ShieldCheck size={16} /><span>只报告经过证据支持的结果</span></div><button className="button button-primary agent-start-button" onClick={() => void create()} disabled={creating || !instruction.trim()}>{creating ? <span className="button-spinner" /> : <Send size={17} />}{creating ? '正在建立任务…' : '开始处理'}</button></div>
        {error && <div className="agent-inline-error" role="alert"><AlertCircle size={17} /><span>{error}</span></div>}
      </section>

      <div className="agent-main-grid goal-main-grid">
        <aside className="panel agent-task-list"><div className="agent-panel-heading"><div><h2>最近诊断作业</h2><p>重新打开后仍可检查和继续。</p></div><button className="icon-button" aria-label="刷新诊断作业" onClick={() => void load()}><RefreshCw size={16} /></button></div>{loading ? <div className="agent-task-list-loading">正在读取诊断作业…</div> : sessions.length ? <div className="agent-task-items">{sessions.map((item) => { const meta = sessionMeta(item); return <button key={item.id} className={`agent-task-item ${active?.id === item.id ? 'agent-task-item-active' : ''}`} onClick={() => void select(item)}><span className={`agent-task-dot agent-tone-${meta.tone}`} /><span className="agent-task-item-copy"><strong>{item.original_instruction}</strong><small>{meta.label} · {formatDateTime(item.updated_at)}</small></span><ChevronRight size={16} /></button>; })}</div> : <EmptyState compact icon={Sparkles} title="还没有诊断作业" description="在上方提交一条测试执行指令即可开始。" />}</aside>

        <section className="panel agent-task-detail goal-detail">{!active || !status ? <EmptyState icon={Smartphone} title="尚未选择诊断作业" description="提交或选择一条作业后，可检查执行状态、事件与证据。" /> : <><div className={`agent-task-hero agent-task-hero-${status.tone}`}><div className="agent-task-hero-title"><div className="agent-task-state-icon">{active.status === 'COMPLETED' ? <CheckCircle2 size={22} /> : active.status === 'WAITING_ALL' || active.control_mode === 'USER_ACTIVE' || active.control_mode === 'RECOVERING_CONTEXT' || active.control_mode === 'TAKEOVER' ? <CirclePause size={22} /> : <Smartphone size={22} />}</div><div><span className={`agent-status agent-tone-${status.tone}`}>{status.label}</span><h2>{active.original_instruction}</h2><p>{active.summary || status.detail}</p></div></div><div className="agent-task-meta"><span>作业 {active.id.slice(0, 8)}</span><span>事件 {active.event_cursor}</span></div>{canControl && <div className="goal-control-row">{active.control_mode === 'AGENT_ACTIVE' && <button className="button button-secondary button-small" disabled={working} onClick={() => void control('pause')}><CirclePause size={14} /> 暂停</button>}{active.control_mode !== 'AGENT_ACTIVE' && active.status !== 'STOPPING' && <button className="button button-secondary button-small" disabled={working} onClick={() => void control('resume')}><CirclePlay size={14} /> {active.control_mode === 'TAKEOVER' ? '明确交还' : '继续'}</button>}{active.control_mode !== 'TAKEOVER' && <button className="button button-secondary button-small" disabled={working} onClick={() => void control('takeover')}><UserRound size={14} /> 接管</button>}<button className="button button-secondary button-small" disabled={working} onClick={() => void control('stop')}><Square size={14} /> 停止</button></div>}</div>

          <div className="agent-progress-grid goal-progress-grid"><div><span>当前目标</span><strong>{goal?.title || '正在建立'}</strong><small>{goal ? `${goal.status}${goal.bound_goal_run_id ? ' · 已绑定执行任务' : ' · 等待绑定'}` : '尚未选择当前目标'}</small></div><div><span>目标图</span><strong>revision {graphRevision}</strong><small>{goals.length} 个独立 Goal · 原始意图保留</small></div><div><span>控制状态</span><strong>{runtimeControl?.label ?? active.control_mode}</strong><small>{active.control_mode} · 指令 revision {active.authority_revision}</small></div><div><span>持续记录</span><strong>{active.event_cursor} 条事件</strong><small>{formatDateTime(active.updated_at)} 更新</small></div></div>

          {activeWaitsForUserFact && <section className="fact-question-section" aria-label="等待你的回答">
            <div className="goal-graph-heading"><div><span className="eyebrow">UserFact（用户事实）</span><h3>有目标在等你回答</h3><p>只暂停缺少这个事实的 Goal；同一任务里的其他 Goal 会继续处理。</p></div><span className="goal-graph-count">{factNeeds.length} 个问题</span></div>
            {factNeeds.length ? <div className="fact-question-list">{factNeeds.map((need) => {
              const needGoalId = need.goal_id || need.origin_goal_id;
              const goalTitle = goals.find((item) => item.id === needGoalId)?.title;
              return <QuestionCard key={need.id || need.need_id} need={need} goalTitle={goalTitle} onAnswer={(value, key) => answerFactNeed(need, value, key)} />;
            })}</div> : <p className="goal-graph-empty">正在读取需要你补充的问题…</p>}
          </section>}

          <section className="goal-graph event-runtime" aria-label="跨 App 事件运行时">
            <div className="goal-graph-heading"><div><span className="eyebrow">Cross-app event runtime（跨应用事件运行时）</span><h3>这次为什么切换</h3><p>{routeReason || '外部事件会先保存，再唤醒匹配 Goal；是否切换只由新的注意力决策决定。'}</p></div><span className={`event-runtime-mode agent-tone-${runtimeControl?.tone ?? 'neutral'}`}>{runtimeControl?.label ?? active.control_mode}</span></div>
            <div className={`event-control-banner event-control-banner-${runtimeControl?.tone ?? 'neutral'}`}>
              {active.control_mode === 'USER_ACTIVE' ? <Eye size={18} /> : active.control_mode === 'RECOVERING_CONTEXT' ? <RotateCw size={18} /> : active.control_mode === 'TAKEOVER' ? <UserRound size={18} /> : <Smartphone size={18} />}
              <div><strong>{runtimeControl?.label ?? active.control_mode}</strong><p>{active.event_runtime?.control_transition_reason || runtimeControl?.detail}</p></div>
              {(active.control_mode === 'USER_ACTIVE' || active.control_mode === 'RECOVERING_CONTEXT') && <small>{active.event_runtime?.raw_observation_count ?? 0} 条原始观察{latestRawObservationAt ? ` · 最新 ${formatDateTime(latestRawObservationAt)}` : ''}</small>}
            </div>
            {runtimeEvent ? <>
              <div className="event-runtime-path" aria-label="事件切换链路">
                <article className="event-runtime-step event-runtime-step-complete"><span>1</span><div><small>事件先保存</small><strong>{eventLabel(runtimeEvent)}</strong><p>{eventSource(runtimeEvent)}</p></div><BellRing size={16} /></article>
                <article className={`event-runtime-step ${affectedGoalNames.length ? 'event-runtime-step-complete' : ''}`}><span>2</span><div><small>唤醒匹配 Goal</small><strong>{affectedGoalNames.length ? affectedGoalNames.join('、') : '没有匹配 Goal'}</strong><p>{affectedGoalIds.length ? `${affectedGoalIds.length} 个 Goal 受影响` : '事件不会直接启动任何 Goal'}</p></div><GitBranch size={16} /></article>
                <article className={`event-runtime-step ${checkpointRef ? 'event-runtime-step-complete' : ''}`}><span>3</span><div><small>安全让出当前工作</small><strong>{checkpointRef ? '已到 verified checkpoint（已验证检查点）' : '等待已验证检查点'}</strong><p>{checkpointRef || '当前原子动作结算前不会切换'}</p></div><ShieldCheck size={16} /></article>
                <article className={`event-runtime-step ${active.attention_decision?.trigger_event_id === runtimeEvent.id || runtimeEvent.decision_id === active.attention_decision?.id ? 'event-runtime-step-complete' : ''}`}><span>4</span><div><small>重新评估全部 Goal</small><strong>{goal?.title || '尚未选择新 Goal'}</strong><p>{active.attention_decision ? `AttentionDecision revision ${active.attention_decision.decision_revision ?? active.attention_decision.revision ?? '未返回'}` : '事件本身不能代替调度决定'}</p></div><ChevronRight size={16} /></article>
              </div>
              <div className="event-runtime-recovery">
                <RotateCw size={18} />
                <div><strong>恢复不是强制返回栈</strong><p>先前 Goal 的 Continuation 只是下一次全量评估的候选；再次被选中时，系统会先核对目标 App，再从当前真实画面继续。</p></div>
                <dl><div><dt>来源 App</dt><dd>{sourceApplication || '未返回'}</dd></div><div><dt>当前 / 目标 App</dt><dd>{active.current_application || selectedApplication || '等待前台观察'}</dd></div><div><dt>恢复观察</dt><dd>{active.control_mode === 'RECOVERING_CONTEXT' ? '正在采集新画面' : observationRef ? `已保存 · ${observationRef}` : '动作前仍需 fresh observation（新鲜观察）'}</dd></div>{switchContinuation && <div><dt>先前恢复点</dt><dd>{continuationSummary(switchContinuation)}</dd></div>}</dl>
              </div>
            </> : <p className="goal-graph-empty">尚无跨 App 外部事件。旧 Session 仍可查看；新的通知、App、时间和触摸事件会在这里显示完整切换依据。</p>}
          </section>

          <section className="goal-graph" aria-label="注意力调度">
            <div className="goal-graph-heading"><div><span className="eyebrow">AttentionScheduler</span><h3>当前注意力</h3><p>{active.attention_decision?.reason || '调度解释尚未返回，页面仍保留当前目标。'}</p></div><span className="goal-graph-count">{active.attention_decision ? `decision revision ${active.attention_decision.decision_revision ?? active.attention_decision.revision ?? '未返回'}` : '兼容模式'}</span></div>
            <div className="agent-progress-grid goal-progress-grid">
              <div><span>当前选择</span><strong>{goal?.title || '尚未选择'}</strong><small>{selectedGoalId ? `Goal ${selectedGoalId}` : '没有 Goal 占用执行槽'}</small></div>
              <div><span>本次依据</span><strong>{attentionBasis(active)}</strong><small>{active.attention_decision?.created_at ? formatDateTime(active.attention_decision.created_at) : '等待首次持久化决策'}</small></div>
              <div><span>待处理事件</span><strong>{active.pending_event_count ?? 0} 条</strong><small>这里只显示调度事件数量，不代表设备动作已经发生</small></div>
              <div><span>最新 Continuation</span><strong>{continuationSummary(selectedContinuation)}</strong><small>{selectedContinuation?.revision ? `revision ${selectedContinuation.revision}` : '切换 Goal 时会保存恢复位置'}</small></div>
            </div>
            <div className="goal-node-list" aria-label="注意力候选排名">
              {attentionCandidates.length ? attentionCandidates.map((candidate, index) => {
                const goalId = candidateGoalId(candidate);
                const node = goals.find((item) => item.id === goalId);
                const score = candidateScore(candidate);
                return <article key={`${goalId}-${candidate.rank ?? index}`} className={`goal-node ${goalId === selectedGoalId ? 'goal-node-active' : ''}`}>
                  <div className="goal-node-heading"><div><strong>#{candidate.rank ?? index + 1} {node?.title || goalId}</strong><small>{candidate.reason || candidate.eligibility_reason || '已纳入本次候选评估'}</small></div><span className="goal-node-status">{score == null ? '分数未返回' : `${score} 分`}</span></div>
                  <dl className="goal-node-meta"><div><dt>eligibility</dt><dd>{candidateEligibility(candidate)}</dd></div>{candidate.hard_tier != null && <div><dt>hard tier</dt><dd>{candidate.hard_tier}</dd></div>}{candidate.scheduling_class && <div><dt>调度类</dt><dd>{candidate.scheduling_class}</dd></div>}</dl>
                </article>;
              }) : <p className="goal-graph-empty">尚未返回候选排名；当前 Session 仍可继续查看和控制。</p>}
            </div>
          </section>

          <section className="goal-graph" aria-label="目标图">
            <div className="goal-graph-heading"><div><span className="eyebrow">目标图 revision {graphRevision}</span><h3>独立 Goal</h3><p>每个目标保留对应的原始意图、成功条件和独立执行绑定。</p></div><span className="goal-graph-count">{goals.length} 个 Goal</span></div>
            {goals.length ? <div className="goal-node-list">{goals.map((node) => {
              const wake = wakeFor(active, node);
              const continuation = continuationFor(active, node);
              return <article key={node.id} className={`goal-node ${node.id === selectedGoalId ? 'goal-node-active' : ''}`}>
              <div className="goal-node-heading"><div><strong>{node.title}</strong><small>{node.id === selectedGoalId ? '当前处理' : node.scheduling_class === 'IDLE_ONLY' || node.eligibility === 'IDLE_DEFERRED' ? '空闲时处理' : '独立目标'}</small></div><span className="goal-node-status">{node.status}</span></div>
              {node.original_fragment && <p className="goal-node-fragment">原始意图：{node.original_fragment}</p>}
              <dl className="goal-node-meta"><div><dt>激活</dt><dd>{node.activation_state || (node.bound_goal_run_id ? 'BOUND' : 'PREPARED')}</dd></div><div><dt>绑定</dt><dd>{node.bound_goal_run_id || '尚未绑定'}</dd></div>{node.explicit_priority != null && <div><dt>优先级</dt><dd>{node.explicit_priority}</dd></div>}{node.eligibility && <div><dt>eligibility</dt><dd>{node.eligibility}</dd></div>}{node.attention_score != null && <div><dt>调度分</dt><dd>{Math.round(node.attention_score)}</dd></div>}{node.last_service_at && <div><dt>上次服务</dt><dd>{formatDateTime(node.last_service_at)}</dd></div>}</dl>
              {(node.eligibility_reason || wake || node.next_eligible_at) && <div className="goal-node-criteria"><span>等待与恢复</span><p>{node.eligibility_reason || wake?.reason || '等待条件满足后恢复'}</p><small>{wake?.kind || node.status}{wake?.earliest_wake_at || wake?.next_eligible_at || wake?.due_at || node.next_eligible_at ? ` · 最早 ${formatDateTime(wake?.earliest_wake_at || wake?.next_eligible_at || wake?.due_at || node.next_eligible_at)}` : ''}</small></div>}
              {continuation && <div className="goal-node-criteria"><span>Continuation{continuation.revision ? ` revision ${continuation.revision}` : ''}</span><p>{continuationSummary(continuation)}</p></div>}
              <div className="goal-node-criteria"><span>成功条件</span>{node.criteria?.length ? <ul>{node.criteria.map((criterion, index) => <li key={criterion.id ?? `${node.id}-criterion-${index}`}>{criterionText(criterion)}{criterion.status ? <small>{criterion.status}</small> : null}</li>)}</ul> : <p>尚未返回独立成功条件。</p>}</div>
            </article>; })}</div> : <p className="goal-graph-empty">正在把原始指令整理为可独立执行的 Goal。</p>}
          </section>

          {!TERMINAL.has(active.status) && <div className="goal-gate"><MessageSquarePlus size={20} /><div><strong>更新目标图</strong><p>新增、修改或调整优先级都会形成新的目标图 revision，不会覆盖最初的一句话。</p></div><div className="goal-control-row"><select aria-label="补充类型" value={directiveKind} onChange={(event) => setDirectiveKind(event.target.value as SessionDirectiveKind)}><option value="add">新增目标</option><option value="revise">修改目标</option><option value="reprioritize">调整优先级</option></select><input aria-label="补充任务" value={message} onChange={(event) => setMessage(event.target.value)} placeholder={directiveKind === 'reprioritize' ? '例如：优先处理找工作' : '例如：再帮我处理一件事'} /><button className="button button-primary button-small" disabled={working || !message.trim()} onClick={() => void addMessage()}><Send size={14} /> 保存更新</button></div></div>}

          <details className="agent-technical-details" open><summary><span>任务记录</span><small>目标图、补充指令与已发生事件</small></summary><div className="agent-technical-body goal-evidence-list"><section><h3>目标图历史</h3>{active.graph_revisions?.length ? active.graph_revisions.map((revision) => <article key={`${revision.revision}-${revision.created_at ?? ''}`}><strong>revision {revision.revision}{revision.authority_revision ? ` · 指令 ${revision.authority_revision}` : ''}</strong><p>{revision.reason || revision.summary || '已保存目标图变更'}</p></article>) : <p>当前为 revision {graphRevision}。</p>}</section><section><h3>已记录指令</h3>{active.directives.length ? active.directives.map((directive) => <article key={directive.id}><strong>revision {directive.revision} · {directive.directive_kind}</strong><p>{directive.content}</p></article>) : <p>仅保留原始指令。</p>}</section><section><h3>事件</h3>{orderedEvents.length ? orderedEvents.map((event) => <article key={event.id}><strong>{eventLabel(event)}</strong><span>#{event.cursor} · {event.handling_status}</span><p>{formatDateTime(event.created_at)}</p></article>) : <p>尚未读取到事件记录。</p>}</section></div></details>
        </>}</section>
      </div>
    </section>
  );
}
