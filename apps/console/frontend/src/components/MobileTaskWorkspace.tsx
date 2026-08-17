import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Bot,
  Check,
  CircleDot,
  CirclePause,
  History,
  ListTodo,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  Smartphone,
} from 'lucide-react';
import { ApiError, api } from '../api';
import { formatDateTime } from '../format';
import type {
  CreateMobileTaskRequest,
  MobileTaskAttempt,
  MobileTaskState,
  SendMobileTaskInputRequest,
  StopMobileTaskRequest,
  Target,
} from '../types';
import { EmptyState } from './EmptyState';

const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'stopped', 'uncertain']);

const TASK_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: '等待开始', tone: 'neutral' },
  planning: { label: '正在准备', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  stopping: { label: '正在停止', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '未能完成', tone: 'danger' },
  stopped: { label: '已停止', tone: 'neutral' },
  uncertain: { label: '需要确认', tone: 'warning' },
};

const SUBGOAL_STATUS: Record<string, string> = {
  pending: '等待',
  active: '进行中',
  completed: '已完成',
};

const ACTION_LABELS: Record<string, string> = {
  tap: '点击屏幕',
  click: '点击屏幕',
  swipe: '滑动屏幕',
  scroll: '滚动屏幕',
  type: '输入文字',
  input_text: '输入文字',
  back: '返回上一页',
  home: '回到桌面',
  wait: '等待页面更新',
  observe: '读取当前画面',
};

const EVENT_LABELS: Record<string, string> = {
  task_created: '任务已创建',
  plan_created: '已整理执行步骤',
  plan_updated: '执行步骤已更新',
  input_accepted: '已收到中途补充',
  input_applied: '中途补充已纳入任务',
  attempt_started: '开始一次设备操作',
  attempt_completed: '设备操作已记录',
  reflection_recorded: '已调整执行方法',
  stop_requested: '已收到停止要求',
  task_completed: '任务已完成',
  task_failed: '任务未能完成',
  task_stopped: '任务已停止',
};

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : '请求未完成，请稍后重试。';
}

function requestId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
  return `${prefix}-${suffix}`;
}

function isTerminal(task: MobileTaskState | null): boolean {
  return Boolean(task && TERMINAL_STATUSES.has(task.status));
}

function isReadyAndroid(target: Target): boolean {
  return target.status === 'ready'
    && (target.kind === 'android' || target.kind === 'emulator' || target.platform === 'android');
}

function taskStatus(status: string) {
  return TASK_STATUS[status] ?? { label: '状态更新中', tone: 'neutral' };
}

function actionLabel(actionType?: string | null): string {
  if (!actionType) return '等待下一步';
  return ACTION_LABELS[actionType] ?? '执行设备操作';
}

function actionFact(attempt: MobileTaskAttempt | null): string {
  if (!attempt) return '还没有向设备发送新动作。';
  if (attempt.transport_status === 'accepted') return '动作已送到设备，正在根据新画面确认结果';
  if (attempt.transport_status === 'rejected') return '设备没有接收这个动作，智能体会调整后再试';
  if (attempt.transport_status === 'uncertain') return '动作结果暂时无法确认，正在重新读取画面';
  return '动作尚未送出，正在等待设备就绪';
}

function verifierLabel(attempt: MobileTaskAttempt): string {
  const verification = attempt.verification;
  if (!verification) return '等待新画面';
  if (verification.uncertain) return '结果仍需确认';
  if (verification.satisfied) return '这一步已经确认完成';
  if (verification.progress) return '画面显示有进展';
  return '还没有确认进展';
}

function progressLabel(task: MobileTaskState): string {
  if (task.status === 'queued') return '正在等待设备';
  if (task.status === 'planning') return '正在整理下一步';
  if (task.status === 'running') return '正在处理';
  if (task.status === 'stopping') return '正在安全停止';
  if (task.status === 'completed') return '任务已经完成';
  if (task.status === 'failed') return '这次没有完成';
  if (task.status === 'stopped') return '任务已经停止';
  return '结果需要人工确认';
}

function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? '任务状态已更新';
}

function preferNewestTask(current: MobileTaskState | null, next: MobileTaskState): MobileTaskState {
  if (!current || current.id !== next.id) return next;
  const currentTime = Date.parse(current.updated_at);
  const nextTime = Date.parse(next.updated_at);
  if (Number.isFinite(currentTime) && Number.isFinite(nextTime) && nextTime < currentTime) return current;
  if (next.input_revision < current.input_revision) return current;
  if (next.attempt_count < current.attempt_count) return current;
  if (next.reflection_count < current.reflection_count) return current;
  return next;
}

interface MobileTaskWorkspaceProps {
  targets: Target[];
}

export function MobileTaskWorkspace({ targets }: MobileTaskWorkspaceProps) {
  const [tasks, setTasks] = useState<MobileTaskState[]>([]);
  const [selectedTask, setSelectedTask] = useState<MobileTaskState | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [goal, setGoal] = useState('');
  const [targetId, setTargetId] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const failedCreate = useRef<CreateMobileTaskRequest | null>(null);

  const [instruction, setInstruction] = useState('');
  const [sending, setSending] = useState(false);
  const [inputError, setInputError] = useState<string | null>(null);
  const failedInput = useRef<{ taskId: string; body: SendMobileTaskInputRequest } | null>(null);

  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const failedStop = useRef<{ taskId: string; body: StopMobileTaskRequest } | null>(null);
  const detailRequestsInFlight = useRef(new Set<string>());

  const readyAndroidTargets = useMemo(() => targets.filter(isReadyAndroid), [targets]);

  useEffect(() => {
    setTargetId((current) => {
      if (current && readyAndroidTargets.some((target) => target.id === current)) return current;
      return readyAndroidTargets.length === 1 ? readyAndroidTargets[0].id : '';
    });
  }, [readyAndroidTargets]);

  const mergeTask = useCallback((next: MobileTaskState, select = false) => {
    setTasks((current) => {
      const existing = current.find((item) => item.id === next.id) ?? null;
      return existing
        ? current.map((item) => item.id === next.id ? preferNewestTask(item, next) : item)
        : [next, ...current];
    });
    setSelectedTask((current) => select || current?.id === next.id ? preferNewestTask(current, next) : current);
  }, []);

  const loadTasks = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const response = await api.getMobileTasks(100);
      setTasks((current) => response.items.map((item) => (
        preferNewestTask(current.find((existing) => existing.id === item.id) ?? null, item)
      )));
      setSelectedTask((current) => {
        if (current) {
          const listed = response.items.find((item) => item.id === current.id);
          return listed ? preferNewestTask(current, listed) : response.items[0] ?? null;
        }
        return response.items[0] ?? null;
      });
      setListError(null);
    } catch (error) {
      setListError(messageFrom(error));
    } finally {
      setLoading(false);
      if (!quiet) setRefreshing(false);
    }
  }, []);

  const loadTask = useCallback(async (taskId: string, select = false, quiet = false) => {
    if (detailRequestsInFlight.current.has(taskId)) return;
    detailRequestsInFlight.current.add(taskId);
    try {
      const next = await api.getMobileTask(taskId);
      mergeTask(next, select);
      setDetailError(null);
    } catch (error) {
      if (!quiet) setDetailError(messageFrom(error));
    } finally {
      detailRequestsInFlight.current.delete(taskId);
    }
  }, [mergeTask]);

  useEffect(() => {
    void loadTasks(true);
  }, [loadTasks]);

  useEffect(() => {
    if (!selectedTask?.id) return;
    void loadTask(selectedTask.id, true, true);
  }, [loadTask, selectedTask?.id]);

  useEffect(() => {
    if (!selectedTask || isTerminal(selectedTask)) return;
    let disposed = false;
    let inFlight = false;
    const poll = async () => {
      if (disposed || inFlight || document.visibilityState === 'hidden') return;
      inFlight = true;
      try {
        const next = await api.getMobileTask(selectedTask.id);
        if (!disposed) {
          mergeTask(next, true);
          setDetailError(null);
        }
      } catch (error) {
        if (!disposed) setDetailError(messageFrom(error));
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [mergeTask, selectedTask?.id, selectedTask?.status]);

  useEffect(() => {
    setInstruction('');
    setInputError(null);
    setStopError(null);
    failedInput.current = null;
    failedStop.current = null;
  }, [selectedTask?.id]);

  const submitCreate = useCallback(async (body: CreateMobileTaskRequest) => {
    setCreating(true);
    setCreateError(null);
    try {
      const created = await api.createMobileTask(body);
      failedCreate.current = null;
      mergeTask(created, true);
      setGoal('');
    } catch (error) {
      failedCreate.current = body;
      setCreateError(messageFrom(error));
    } finally {
      setCreating(false);
    }
  }, [mergeTask]);

  const startTask = () => {
    const cleanGoal = goal.trim();
    if (!cleanGoal || !targetId || creating) return;
    void submitCreate({
      goal: cleanGoal,
      client_request_id: requestId('task'),
      target_id: targetId,
    });
  };

  const retryCreate = () => {
    if (failedCreate.current) void submitCreate(failedCreate.current);
  };

  const submitInput = useCallback(async (taskId: string, body: SendMobileTaskInputRequest) => {
    setSending(true);
    setInputError(null);
    try {
      const next = await api.sendMobileTaskInput(taskId, body);
      failedInput.current = null;
      mergeTask(next, true);
      setInstruction('');
    } catch (error) {
      failedInput.current = { taskId, body };
      setInputError(messageFrom(error));
    } finally {
      setSending(false);
    }
  }, [mergeTask]);

  const sendInstruction = () => {
    if (!selectedTask || isTerminal(selectedTask) || selectedTask.status === 'stopping') return;
    const content = instruction.trim();
    if (!content || sending) return;
    void submitInput(selectedTask.id, {
      content,
      client_request_id: requestId('task-input'),
    });
  };

  const retryInput = () => {
    const retry = failedInput.current;
    if (retry) void submitInput(retry.taskId, retry.body);
  };

  const submitStop = useCallback(async (taskId: string, body: StopMobileTaskRequest) => {
    setStopping(true);
    setStopError(null);
    try {
      const next = await api.stopMobileTask(taskId, body);
      failedStop.current = null;
      mergeTask(next, true);
    } catch (error) {
      failedStop.current = { taskId, body };
      setStopError(messageFrom(error));
    } finally {
      setStopping(false);
    }
  }, [mergeTask]);

  const stopTask = () => {
    if (!selectedTask || isTerminal(selectedTask) || selectedTask.status === 'stopping' || stopping) return;
    void submitStop(selectedTask.id, { client_request_id: requestId('task-stop') });
  };

  const retryStop = () => {
    const retry = failedStop.current;
    if (retry) void submitStop(retry.taskId, retry.body);
  };

  const latestAttempt = selectedTask?.attempts.at(-1) ?? null;
  const selectedTarget = selectedTask?.target_id
    ? targets.find((target) => target.id === selectedTask.target_id) ?? null
    : null;
  const activeSubgoal = selectedTask?.plan?.subgoals.find((subgoal) => subgoal.status === 'active')
    ?? selectedTask?.plan?.subgoals[selectedTask?.active_subgoal_index ?? 0]
    ?? null;
  const taskMeta = selectedTask ? taskStatus(selectedTask.status) : null;
  const canInteract = Boolean(selectedTask && !isTerminal(selectedTask) && selectedTask.status !== 'stopping');

  return (
    <section className="agent-workspace" aria-label="一句话开始">
      <section className="agent-composer panel">
        <div className="agent-composer-heading">
          <span className="agent-composer-icon" aria-hidden="true"><Bot size={23} /></span>
          <div>
            <span className="eyebrow">手机智能体</span>
            <h2>一句话告诉我，你想让手机完成什么</h2>
            <p>选好设备并说出结果。开始后你仍可随时补充要求，或停止任务。</p>
          </div>
        </div>
        <label className="agent-goal-field">
          <span>你想完成什么</span>
          <textarea
            aria-label="你想完成什么"
            rows={3}
            maxLength={10000}
            placeholder="例如：打开游戏，领取今天可以领取的活动奖励，并确认已经到账"
            value={goal}
            onChange={(event) => {
              setGoal(event.target.value);
              setCreateError(null);
              failedCreate.current = null;
            }}
          />
        </label>
        <div className="agent-composer-options agent-composer-options-simple">
          <label>
            <span>运行设备</span>
            <select
              aria-label="运行设备"
              value={targetId}
              onChange={(event) => {
                setTargetId(event.target.value);
                setCreateError(null);
                failedCreate.current = null;
              }}
            >
              {readyAndroidTargets.length !== 1 && (
                <option value="">{readyAndroidTargets.length ? '请选择设备' : '暂无可用 Android 设备'}</option>
              )}
              {readyAndroidTargets.map((target) => <option key={target.id} value={target.id}>{target.name}</option>)}
            </select>
            <small>{readyAndroidTargets.length ? `${readyAndroidTargets.length} 台 Android 设备可用` : '请先在“设备”页连接模拟器、真机或平板'}</small>
          </label>
          <button className="button button-primary agent-start-button" onClick={startTask} disabled={!goal.trim() || !targetId || creating}>
            {creating ? <span className="button-spinner" /> : <Sparkles size={17} />}
            {creating ? '正在开始…' : '开始'}
          </button>
        </div>
        {createError && (
          <div className="agent-inline-error" role="alert">
            <AlertCircle size={17} />
            <span>{createError}</span>
            <button className="button button-secondary button-small" onClick={retryCreate} disabled={creating}><RotateCcw size={14} /> 重试同一请求</button>
          </div>
        )}
      </section>

      <div className="agent-main-grid">
        <aside className="agent-task-list panel" aria-label="最近任务">
          <div className="agent-panel-heading">
            <div><h2>最近任务</h2><p>{tasks.length} 个任务</p></div>
            <button className="icon-button" aria-label="刷新任务" title="刷新任务" onClick={() => void loadTasks()} disabled={refreshing}>
              <RefreshCw size={16} className={refreshing ? 'spin' : ''} />
            </button>
          </div>
          {listError && <div className="agent-list-error" role="alert"><AlertTriangle size={16} /><span>{listError}</span><button className="text-button" onClick={() => void loadTasks()}>重试</button></div>}
          {loading ? (
            <div className="agent-task-list-loading" aria-label="正在加载任务"><span className="button-spinner" /> 正在读取任务</div>
          ) : tasks.length ? (
            <div className="agent-task-items">
              {tasks.map((task) => {
                const meta = taskStatus(task.status);
                return (
                  <button
                    key={task.id}
                    className={`agent-task-item ${selectedTask?.id === task.id ? 'agent-task-item-active' : ''}`}
                    aria-pressed={selectedTask?.id === task.id}
                    onClick={() => {
                      setSelectedTask(task);
                      setDetailError(null);
                    }}
                  >
                    <span className={`agent-task-dot agent-tone-${meta.tone}`} />
                    <span className="agent-task-item-copy"><strong>{task.goal}</strong><small>{formatDateTime(task.updated_at)}</small></span>
                    <span className={`agent-mini-status agent-tone-${meta.tone}`}>{meta.label}</span>
                  </button>
                );
              })}
            </div>
          ) : <EmptyState compact icon={ListTodo} title="还没有任务" description="在上方说出想完成的事，开始后会在这里持续显示进度。" />}
        </aside>

        <div className="agent-task-detail">
          {selectedTask && taskMeta ? (
            <>
              {detailError && <div className="agent-inline-error" role="alert"><AlertCircle size={17} /><span>{detailError}</span><button className="button button-secondary button-small" onClick={() => void loadTask(selectedTask.id, true)}><RefreshCw size={14} /> 重试读取</button></div>}
              <section className={`agent-task-hero panel agent-task-hero-${taskMeta.tone}`}>
                <div className="agent-task-hero-title">
                  <span className={`agent-task-state-icon agent-tone-${taskMeta.tone}`} aria-hidden="true">
                    {selectedTask.status === 'completed' ? <ShieldCheck size={22} /> : isTerminal(selectedTask) ? <CirclePause size={22} /> : <Activity size={22} />}
                  </span>
                  <div>
                    <span className="eyebrow">当前任务</span>
                    <h2>{selectedTask.goal}</h2>
                    <p>{selectedTask.detail || '正在等待新的进度。'}</p>
                    <div className="agent-task-meta">
                      <span>{selectedTarget?.name || '设备信息待更新'}</span>
                      <span>更新于 {formatDateTime(selectedTask.updated_at)}</span>
                    </div>
                  </div>
                </div>
                <div className="agent-task-hero-actions">
                  <span className={`agent-status agent-tone-${taskMeta.tone}`}>{taskMeta.label}</span>
                  {canInteract && <button className="button button-ghost-danger button-small" onClick={stopTask} disabled={stopping}><Square size={14} /> {stopping ? '正在提交…' : '停止任务'}</button>}
                </div>
                {stopError && <div className="agent-inline-error agent-stop-error" role="alert"><AlertCircle size={16} /><span>{stopError}</span><button className="button button-secondary button-small" onClick={retryStop} disabled={stopping}><RotateCcw size={14} /> 重试停止请求</button></div>}
              </section>

              <section className="agent-human-progress panel" aria-label="任务当前进度">
                <div className="agent-progress-copy">
                  <span className="eyebrow">当前进度</span>
                  <strong>{progressLabel(selectedTask)}</strong>
                  <p>{activeSubgoal?.description || selectedTask.detail || '智能体正在读取任务状态。'}</p>
                </div>
                <div className="agent-current-action">
                  <span><Smartphone size={17} /> 当前动作</span>
                  <strong>{actionLabel(latestAttempt?.action_type)}</strong>
                  <p>{actionFact(latestAttempt)}</p>
                </div>
              </section>

              <section className="agent-input-panel panel">
                <div className="agent-panel-heading"><div><h2>中途补充</h2><p>任务运行时也可以继续说明；下一步会读取你的新要求。</p></div></div>
                {canInteract ? (
                  <div className="agent-input-composer">
                    <label><span className="sr-only">补充要求</span><textarea aria-label="补充要求" rows={2} maxLength={10000} placeholder="例如：先关闭弹窗，再继续刚才的目标" value={instruction} onChange={(event) => { setInstruction(event.target.value); setInputError(null); failedInput.current = null; }} /></label>
                    <button className="button button-primary" onClick={sendInstruction} disabled={!instruction.trim() || sending}>{sending ? <span className="button-spinner" /> : <Send size={16} />}{sending ? '正在发送…' : '发送补充'}</button>
                  </div>
                ) : <div className="agent-input-closed"><CirclePause size={16} /> 这个任务已经结束，不能再补充要求。</div>}
                {inputError && <div className="agent-inline-error" role="alert"><AlertCircle size={16} /><span>{inputError}</span><button className="button button-secondary button-small" onClick={retryInput} disabled={sending}><RotateCcw size={14} /> 重试同一条补充</button></div>}
                {selectedTask.inputs.length > 0 && (
                  <div className="agent-input-history">
                    {[...selectedTask.inputs].reverse().map((input) => (
                      <article key={input.revision}><span>{input.lifecycle === 'applied' ? '已纳入任务' : '等待任务读取'}</span><p>{input.content}</p><small>{formatDateTime(input.applied_at || input.created_at)}</small></article>
                    ))}
                  </div>
                )}
              </section>

              <details className="agent-technical-details panel">
                <summary><span>技术详情</span><small>计划、尝试、调整与任务事件</small></summary>
                <div className="agent-technical-body">
                  <section>
                    <div className="agent-panel-heading"><div><h2>计划</h2><p>每一步确认后才会继续</p></div></div>
                    {selectedTask.plan?.subgoals.length ? (
                      <ol className="agent-subgoals">
                        {selectedTask.plan.subgoals.map((subgoal) => (
                          <li className={`agent-subgoal agent-subgoal-${subgoal.status}`} key={`${selectedTask.plan?.revision}-${subgoal.index}`}>
                            <span className="agent-subgoal-index">{subgoal.status === 'completed' ? <Check size={15} /> : subgoal.index + 1}</span>
                            <div><strong>{subgoal.description}</strong><small>{SUBGOAL_STATUS[subgoal.status] ?? '状态更新中'}</small></div>
                          </li>
                        ))}
                      </ol>
                    ) : <EmptyState compact icon={ListTodo} title="还没有计划" description="任务开始整理步骤后会显示在这里。" />}
                  </section>

                  <section>
                    <div className="agent-panel-heading"><div><h2>最近尝试</h2><p>只显示动作事实与画面确认结果</p></div></div>
                    {selectedTask.attempts.length ? (
                      <div className="agent-attempt-list">
                        {[...selectedTask.attempts].reverse().slice(0, 12).map((attempt) => (
                          <article key={attempt.id}>
                            <span className="agent-attempt-sequence">#{attempt.sequence}</span>
                            <div><strong>{actionLabel(attempt.action_type)}</strong><p>{attempt.verification?.evidence || '还没有新的画面证据'}</p><small>{formatDateTime(attempt.finalized_at || attempt.created_at)}</small></div>
                            <div className="agent-attempt-facts"><span>{actionFact(attempt)}</span><span>{verifierLabel(attempt)}</span></div>
                          </article>
                        ))}
                      </div>
                    ) : <EmptyState compact icon={CircleDot} title="还没有设备操作" description="执行开始后会显示脱敏后的事实摘要。" />}
                  </section>

                  <section>
                    <div className="agent-panel-heading"><div><h2>调整记录</h2><p>遇到反复无进展时采用的新方法</p></div></div>
                    {selectedTask.reflections.length ? (
                      <div className="agent-reflection-list">
                        {[...selectedTask.reflections].reverse().slice(0, 8).map((reflection) => (
                          <article key={reflection.sequence}><RefreshCw size={15} /><div><strong>{reflection.strategy}</strong><p>{reflection.reason}</p><small>{formatDateTime(reflection.created_at)}</small></div></article>
                        ))}
                      </div>
                    ) : <EmptyState compact icon={RefreshCw} title="还没有调整" description="当前执行方法尚未触发调整。" />}
                  </section>

                  <section>
                    <div className="agent-panel-heading"><div><h2>任务事件</h2><p>持久化的任务状态变化，不展示内部载荷</p></div></div>
                    {selectedTask.events.length ? (
                      <div className="agent-event-list">
                        {[...selectedTask.events].reverse().slice(0, 12).map((event) => (
                          <article key={event.sequence}><History size={14} /><div><strong>{eventLabel(event.event_type)}</strong><small>{formatDateTime(event.created_at)}</small></div></article>
                        ))}
                      </div>
                    ) : <EmptyState compact icon={History} title="暂无任务事件" description="任务状态变化后会显示在这里。" />}
                  </section>
                </div>
              </details>
            </>
          ) : (
            <section className="panel agent-detail-empty"><EmptyState icon={Smartphone} title="选择一个最近任务" description="新任务开始后，进度会自动显示在这里。" /></section>
          )}
        </div>
      </div>
    </section>
  );
}
