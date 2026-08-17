import { useCallback, useRef, useState, useEffect } from 'react';
import {
  Activity,
  AlertTriangle,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Heart,
  Layers3,
  LoaderCircle,
  MessageSquare,
  Pause,
  Play,
  Radio,
  RefreshCw,
  Send,
  ShieldAlert,
  Square,
  TimerReset,
  Wifi,
  WifiOff,
} from 'lucide-react';
import { ApiError, api } from '../api';
import { formatDateTime } from '../format';
import type {
  ApplicationCommandName,
  ApplicationCommandRequest,
  ApplicationInstance,
  ApplicationInstanceStatus,
  CreateApplicationInstanceRequest,
  SoulSchedulerStatus,
} from '../types';

const SOUL_PROFILE_ID = 'soul-reply-v1';
const POLL_INTERVAL_MS = 1000;
const TERMINAL_STATUSES = new Set<ApplicationInstanceStatus>([
  'stopped',
  'completed',
  'failed',
]);

interface StatusMeta {
  label: string;
  detail: string;
  tone: 'ready' | 'warning' | 'paused' | 'stopped' | 'danger' | 'unknown';
}

const STATUS_META: Record<ApplicationInstanceStatus, StatusMeta> = {
  queued: {
    label: '正在准备',
    detail: '回复与学习请求已经保存，正在等待本地运行资源。',
    tone: 'warning',
  },
  running: {
    label: '运行中',
    detail: '回复和学习实例正在按当前策略运行。',
    tone: 'ready',
  },
  waiting: {
    label: '等待下一轮',
    detail: '回复与学习当前没有需要立即处理的工作，到时间后会继续。',
    tone: 'warning',
  },
  paused: {
    label: '已暂停',
    detail: '回复与学习实例仍然保留，可以从这里继续。',
    tone: 'paused',
  },
  stopping: {
    label: '正在停止',
    detail: '回复与学习正在等待当前安全步骤结束。',
    tone: 'warning',
  },
  stopped: {
    label: '已停止',
    detail: '这次回复与学习运行已经停止，可以重新启动一个实例。',
    tone: 'stopped',
  },
  completed: {
    label: '已完成',
    detail: '这次回复与学习运行已经正常结束，可以重新启动。',
    tone: 'stopped',
  },
  failed: {
    label: '运行异常',
    detail: '回复与学习实例已经结束；全天匹配可能仍在运行，可以重新启动回复实例。',
    tone: 'danger',
  },
};

type StartMutation = {
  kind: 'start';
  body: CreateApplicationInstanceRequest;
};

type CommandMutation = {
  kind: 'command';
  instanceId: string;
  body: ApplicationCommandRequest;
};

type PendingMutation = StartMutation | CommandMutation;

function newRequestId(): string {
  if (globalThis.crypto && 'randomUUID' in globalThis.crypto) {
    return globalThis.crypto.randomUUID();
  }
  return `soul-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isTerminal(status: ApplicationInstanceStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

function timestamp(value: string | null | undefined): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : 0;
}

function selectSoulInstance(items: ApplicationInstance[]): ApplicationInstance | null {
  const soulInstances = items.filter((item) => item.profile_id === SOUL_PROFILE_ID);
  const active = soulInstances.filter((item) => !isTerminal(item.status));
  const candidates = active.length ? active : soulInstances;
  return [...candidates].sort((left, right) => (
    timestamp(right.updated_at) - timestamp(left.updated_at)
      || timestamp(right.created_at) - timestamp(left.created_at)
  ))[0] ?? null;
}

function mutationAcceptedLabel(mutation: PendingMutation): string {
  if (mutation.kind === 'start') return '启动请求已受理。';
  const labels: Record<ApplicationCommandName, string> = {
    Input: '补充指令已受理。',
    Pause: '暂停请求已受理。',
    Resume: '继续请求已受理。',
    Stop: '停止请求已受理。',
  };
  return labels[mutation.body.command];
}

function mutationProgressLabel(mutation: PendingMutation | null): string {
  if (!mutation) return '';
  if (mutation.kind === 'start') return '正在提交启动请求';
  const labels: Record<ApplicationCommandName, string> = {
    Input: '正在提交补充指令',
    Pause: '正在提交暂停请求',
    Resume: '正在提交继续请求',
    Stop: '正在提交停止请求',
  };
  return labels[mutation.body.command];
}

function shouldRetryWithSameRequest(error: unknown): boolean {
  return error instanceof ApiError
    && (error.status === 0 || error.status === 429 || error.status >= 500);
}

function mutationErrorText(error: unknown): string {
  if (!(error instanceof ApiError)) return '请求没有完成，请刷新状态后再试。';
  if (error.code === 'application_runtime_request_id_conflict') {
    return '这个请求编号已经用于另一项操作，请刷新状态后再试。';
  }
  if (error.code === 'application_runtime_queue_full') {
    return '当前运行队列已满，请稍后使用同一请求重试。';
  }
  if (error.code === 'application_runtime_not_found') {
    return '这个长期实例已经找不到了，请刷新后重新启动。';
  }
  return error.message || '请求没有完成，请刷新状态后再试。';
}

function latestCycle(instance: ApplicationInstance): number {
  return Math.max(
    0,
    ...instance.intents.map((intent) => intent.cycle),
    ...instance.outcomes.map((outcome) => outcome.cycle),
  );
}

function confirmedOutcomeCount(instance: ApplicationInstance): number {
  return instance.outcomes.filter((outcome) => (
    outcome.status === 'confirmed_success'
      || outcome.status === 'confirmed_failure'
  )).length;
}

function uncertainOutcomeCount(instance: ApplicationInstance): number {
  return instance.outcomes.filter((outcome) => outcome.status === 'uncertain').length;
}

function outcomeLabel(status: ApplicationInstance['outcomes'][number]['status']): string {
  const labels = {
    confirmed_success: '结果已确认',
    confirmed_failure: '未完成，结果已确认',
    unconfirmed: '结果尚待确认',
    uncertain: '结果不确定',
  } satisfies Record<ApplicationInstance['outcomes'][number]['status'], string>;
  return labels[status];
}

function technicalStatusLabel(status: ApplicationInstanceStatus): string {
  return STATUS_META[status].label;
}

interface SchedulerMeta extends StatusMeta {
  retryBoundary?: string;
}

function schedulerMeta(status: SoulSchedulerStatus | null): SchedulerMeta {
  if (!status) {
    return {
      label: '状态未确认',
      detail: '正在读取全天匹配与即时开场状态。',
      tone: 'unknown',
    };
  }
  if (status.state === 'running') {
    return {
      label: '全天运行中',
      detail: '持续寻找匹配，并在新匹配确认后即时处理开场。',
      tone: 'ready',
    };
  }
  if (status.state === 'paused') {
    return {
      label: '全天运行已暂停',
      detail: '当前不会继续匹配或处理新的即时开场。',
      tone: 'paused',
    };
  }
  if (status.effective_state === 'stopping' || status.code === 'scheduler_stopping') {
    return {
      label: '全天运行正在停止',
      detail: '停止目标已经保存，正在等待安全收尾。',
      tone: 'warning',
      retryBoundary: '当前还没有完全停止；页面只会刷新状态，不会自动重发控制请求。',
    };
  }
  if (status.state === 'stopped') {
    return {
      label: '全天运行已停止',
      detail: '当前不会继续匹配，也不会处理新的即时开场。',
      tone: 'stopped',
    };
  }
  const degraded = {
    scheduler_controller_mismatch: {
      label: '正在恢复控制权',
      detail: '当前控制器与 AI Game 保存的控制目标还没有对齐。',
    },
    scheduler_reply_owner_mismatch: {
      label: '正在恢复回复交接',
      detail: '全天匹配仍有状态，但回复处理还没有交给 ApplicationRuntime。',
    },
    scheduler_mode_mismatch: {
      label: '正在恢复匹配模式',
      detail: '当前调度模式还没有回到全天匹配模式。',
    },
    scheduler_state_mismatch: {
      label: '正在恢复目标状态',
      detail: '全天运行的实际状态还没有达到已保存的目标状态。',
    },
  } as const;
  const copy = degraded[status.code as keyof typeof degraded] ?? degraded.scheduler_state_mismatch;
  return {
    ...copy,
    tone: 'warning',
    retryBoundary: '当前没有达到已保存的目标状态；页面不会自动重发控制请求。若刚才的控制结果未知，只能使用页面保留的同一请求重试。',
  };
}

function schedulerDesiredLabel(status: SoulSchedulerStatus['desired_state']): string {
  return {
    running: '全天运行',
    paused: '保持暂停',
    stopped: '保持停止',
  }[status];
}

function schedulerEffectiveLabel(status: SoulSchedulerStatus['effective_state']): string {
  return {
    running: '运行中',
    paused: '已暂停',
    stopping: '正在停止',
    stopped: '已停止',
  }[status];
}

function replyStatusLabel(instance: ApplicationInstance | null): string {
  if (!instance) return '回复尚未启动';
  if (instance.status === 'failed') return '回复运行异常';
  if (instance.status === 'completed') return '本轮回复已完成';
  if (instance.status === 'stopped') return '回复已停止';
  return STATUS_META[instance.status].label;
}

function latestTimestamp(...values: Array<string | null | undefined>): string | null {
  return values
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => timestamp(right) - timestamp(left))[0] ?? null;
}

export function SoulWorkspace() {
  const [instance, setInstanceState] = useState<ApplicationInstance | null>(null);
  const [scheduler, setScheduler] = useState<SoulSchedulerStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [readFresh, setReadFresh] = useState(false);
  const [schedulerLoaded, setSchedulerLoaded] = useState(false);
  const [schedulerFresh, setSchedulerFresh] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [schedulerError, setSchedulerError] = useState<string | null>(null);
  const [instruction, setInstruction] = useState('');
  const [mutationInFlight, setMutationInFlight] = useState<PendingMutation | null>(null);
  const [retryMutation, setRetryMutationState] = useState<PendingMutation | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const instanceRef = useRef<ApplicationInstance | null>(null);
  const readInFlightRef = useRef(false);
  const schedulerReadInFlightRef = useRef(false);
  const mutationInFlightRef = useRef(false);
  const readSequenceRef = useRef(0);
  const schedulerReadSequenceRef = useRef(0);
  const retryMutationRef = useRef<PendingMutation | null>(null);

  const setInstance = useCallback((next: ApplicationInstance | null) => {
    instanceRef.current = next;
    setInstanceState(next);
  }, []);

  const setRetryMutation = useCallback((next: PendingMutation | null) => {
    retryMutationRef.current = next;
    setRetryMutationState(next);
  }, []);

  const refresh = useCallback(async (quiet = false) => {
    if (readInFlightRef.current || mutationInFlightRef.current) return;
    readInFlightRef.current = true;
    const sequence = ++readSequenceRef.current;
    if (!quiet) setRefreshing(true);
    try {
      const current = instanceRef.current;
      let next: ApplicationInstance | null;
      if (!current || isTerminal(current.status)) {
        const response = await api.getApplicationInstances(100);
        next = selectSoulInstance(response.items);
      } else {
        next = await api.getApplicationInstance(current.id);
      }
      if (sequence !== readSequenceRef.current) return;
      setInstance(next);
      setReadFresh(true);
      setReadError(null);
      setLoaded(true);
      if (next && retryMutationRef.current?.kind === 'start' && !isTerminal(next.status)) {
        setRetryMutation(null);
        setMutationError(null);
        setFeedback('已经恢复到同一个长期实例。');
      }
    } catch {
      if (sequence !== readSequenceRef.current) return;
      setReadFresh(false);
      setReadError('暂时无法读取 Soul 应用状态。恢复连接后会继续刷新，不会自动发送任何命令。');
      setLoaded(true);
    } finally {
      readInFlightRef.current = false;
      if (!quiet) setRefreshing(false);
    }
  }, [setInstance, setRetryMutation]);

  const refreshScheduler = useCallback(async () => {
    if (schedulerReadInFlightRef.current || mutationInFlightRef.current) return;
    schedulerReadInFlightRef.current = true;
    const sequence = ++schedulerReadSequenceRef.current;
    try {
      const next = await api.getSoulSchedulerStatus();
      if (sequence !== schedulerReadSequenceRef.current) return;
      setScheduler(next);
      setSchedulerFresh(true);
      setSchedulerError(null);
      setSchedulerLoaded(true);
    } catch {
      if (sequence !== schedulerReadSequenceRef.current) return;
      setSchedulerFresh(false);
      setSchedulerError('暂时无法确认全天匹配与即时开场状态。页面不会用回复实例的状态代替它。');
      setSchedulerLoaded(true);
    } finally {
      schedulerReadInFlightRef.current = false;
    }
  }, []);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([refresh(true), refreshScheduler()]);
    setRefreshing(false);
  }, [refresh, refreshScheduler]);

  useEffect(() => {
    void refresh(true);
    void refreshScheduler();
  }, [refresh, refreshScheduler]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') {
        void refresh(true);
        void refreshScheduler();
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, refreshScheduler]);

  const executeMutation = async (mutation: PendingMutation) => {
    if (mutationInFlightRef.current) return;
    const affectsScheduler = mutation.kind === 'start'
      || (mutation.kind === 'command' && mutation.body.command !== 'Input');
    mutationInFlightRef.current = true;
    ++readSequenceRef.current;
    if (affectsScheduler) {
      ++schedulerReadSequenceRef.current;
      setSchedulerFresh(false);
    }
    setMutationInFlight(mutation);
    setMutationError(null);
    setFeedback(null);
    try {
      const next = mutation.kind === 'start'
        ? await api.createApplicationInstance(mutation.body)
        : await api.commandApplicationInstance(mutation.instanceId, mutation.body);
      setInstance(next);
      setReadFresh(true);
      setReadError(null);
      setLoaded(true);
      setRetryMutation(null);
      setFeedback(mutationAcceptedLabel(mutation));
      if (mutation.kind === 'command' && mutation.body.command === 'Input') {
        setInstruction('');
      }
    } catch (error) {
      if (shouldRetryWithSameRequest(error)) {
        setRetryMutation(mutation);
        setMutationError('请求已经发出，但结果暂时无法确认。为避免重复操作，只能使用同一请求重试。');
      } else {
        setRetryMutation(null);
        setMutationError(mutationErrorText(error));
      }
    } finally {
      mutationInFlightRef.current = false;
      setMutationInFlight(null);
    }
    if (affectsScheduler) void refreshScheduler();
  };

  const start = () => {
    if (instance && !isTerminal(instance.status)) return;
    void executeMutation({
      kind: 'start',
      body: {
        profile_id: SOUL_PROFILE_ID,
        client_request_id: newRequestId(),
      },
    });
  };

  const command = (name: Exclude<ApplicationCommandName, 'Input'>) => {
    if (!instance || isTerminal(instance.status)) return;
    void executeMutation({
      kind: 'command',
      instanceId: instance.id,
      body: {
        command: name,
        client_request_id: newRequestId(),
      },
    });
  };

  const sendInstruction = () => {
    const content = instruction.trim();
    if (!instance || isTerminal(instance.status) || !content) return;
    void executeMutation({
      kind: 'command',
      instanceId: instance.id,
      body: {
        command: 'Input',
        client_request_id: newRequestId(),
        content,
      },
    });
  };

  const statusMeta = instance ? STATUS_META[instance.status] : null;
  const schedulerStatusMeta = schedulerMeta(scheduler);
  const mutationLocked = Boolean(mutationInFlight) || Boolean(retryMutation);
  const lifecycleControlsLocked = !loaded
    || !readFresh
    || !schedulerLoaded
    || !schedulerFresh
    || mutationLocked;
  const inputControlsLocked = !loaded || !readFresh || mutationLocked;
  const activeInstance = Boolean(instance && !isTerminal(instance.status));
  const schedulerNeedsPause = Boolean(scheduler
    && (scheduler.desired_state !== 'paused' || scheduler.effective_state !== 'paused'));
  const schedulerNeedsResume = Boolean(scheduler
    && (scheduler.desired_state !== 'running' || scheduler.effective_state !== 'running'));
  const canPause = Boolean(instance
    && !isTerminal(instance.status)
    && instance.status !== 'stopping'
    && (instance.status !== 'paused' || schedulerNeedsPause));
  const canResume = Boolean(instance
    && !isTerminal(instance.status)
    && instance.status !== 'stopping'
    && (['paused', 'waiting'].includes(instance.status) || schedulerNeedsResume));
  const canStop = Boolean(instance && !isTerminal(instance.status) && instance.status !== 'stopping');
  const canInput = Boolean(instance && !isTerminal(instance.status) && instance.status !== 'stopping');
  const uncertainCount = instance ? uncertainOutcomeCount(instance) : 0;
  const recentOutcomes = instance ? [...instance.outcomes].sort((left, right) => right.cycle - left.cycle).slice(0, 5) : [];
  const allReadsFresh = readFresh && schedulerFresh;
  const lastObservedAt = latestTimestamp(instance?.updated_at, scheduler?.observed_at);

  return (
    <section className="soul-workspace" aria-label="Soul 应用">
      <header className="soul-hero panel">
        <div className="soul-hero-mark" aria-hidden="true"><Heart size={25} /></div>
        <div className="soul-hero-copy">
          <span className="eyebrow">Application</span>
          <h2>Soul</h2>
          <p>Soul 是 AI Game 可以长期运行的一项应用功能。</p>
          <div className="soul-hero-statuses">
            <span className={`soul-pill soul-pill-${allReadsFresh ? 'ready' : 'unknown'}`}>
              {allReadsFresh ? <Wifi size={14} /> : <WifiOff size={14} />}
              {allReadsFresh ? '两部分状态已同步' : '部分状态暂不可用'}
            </span>
            <span className={`soul-pill soul-pill-${schedulerFresh ? schedulerStatusMeta.tone : 'unknown'}`}>
              <Radio size={14} /> {schedulerFresh ? schedulerStatusMeta.label : '全天状态未确认'}
            </span>
            <span className={`soul-pill soul-pill-${statusMeta?.tone ?? 'unknown'}`}>
              <MessageSquare size={14} /> {replyStatusLabel(instance)}
            </span>
            {instance?.degraded && <span className="soul-pill soul-pill-warning"><AlertTriangle size={14} /> 需要留意</span>}
            {instance?.hard_risk && <span className="soul-pill soul-pill-danger"><ShieldAlert size={14} /> 需要确认</span>}
          </div>
        </div>
        <div className="soul-hero-actions">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void refreshAll()}
            disabled={refreshing || mutationInFlightRef.current}
          >
            <RefreshCw size={16} className={refreshing ? 'spin' : ''} />
            {refreshing ? '正在刷新' : '刷新状态'}
          </button>
          <small>最近更新：{formatDateTime(lastObservedAt)}</small>
        </div>
      </header>

      {(!loaded || !schedulerLoaded) && (
        <div className="soul-loading panel" role="status">
          <LoaderCircle className="spin" size={22} />
          <div><strong>正在读取 Soul 的两个运行部分</strong><span>这里只读取调度和回复记录，不会触发设备操作。</span></div>
        </div>
      )}

      {readError && (
        <div className="soul-offline panel" role="alert">
          <WifiOff size={22} />
          <div><strong>暂时无法读取回复与学习状态</strong><span>{readError}</span></div>
          <button type="button" className="button button-secondary" onClick={() => void refreshAll()} disabled={refreshing}>
            <RefreshCw size={16} className={refreshing ? 'spin' : ''} /> 重新读取
          </button>
        </div>
      )}

      <section className="soul-runtime-split" aria-label="Soul 两个运行部分">
        <section className="soul-runtime-card panel" aria-label="全天匹配与即时开场">
          <div className="soul-runtime-card-heading">
            <div className="soul-runtime-card-icon soul-runtime-card-icon-scheduler"><Radio size={20} /></div>
            <div><span className="eyebrow">A · 全天运行</span><h3>全天匹配与即时开场</h3></div>
            <span className={`soul-health-state soul-health-${schedulerFresh ? schedulerStatusMeta.tone : 'unknown'}`}>
              {schedulerFresh ? schedulerStatusMeta.label : '状态未确认'}
            </span>
          </div>
          <p>{schedulerFresh ? schedulerStatusMeta.detail : '正在单独读取全天运行状态；不会用回复实例的状态代替。'}</p>
          {scheduler && schedulerFresh && (
            <div className="soul-scheduler-facts">
              <span>目标：{schedulerDesiredLabel(scheduler.desired_state)}</span>
              <span>实际：{schedulerEffectiveLabel(scheduler.effective_state)}</span>
              <span>{scheduler.controller_matches ? '控制权已对齐' : '控制权未对齐'}</span>
            </div>
          )}
          {schedulerError && (
            <div className="soul-inline-error" role="status"><WifiOff size={15} /><span>{schedulerError}</span></div>
          )}
          {schedulerFresh && schedulerStatusMeta.retryBoundary && (
            <div className="soul-health-note soul-note-warning">
              <TimerReset size={16} />
              <div><strong>{schedulerStatusMeta.label}</strong><span>{schedulerStatusMeta.retryBoundary}</span></div>
            </div>
          )}
          <small>最近读取：{formatDateTime(scheduler?.observed_at)}</small>
        </section>

        <section className="soul-runtime-card panel" aria-label="回复与学习">
          <div className="soul-runtime-card-heading">
            <div className="soul-runtime-card-icon soul-runtime-card-icon-reply"><MessageSquare size={20} /></div>
            <div><span className="eyebrow">B · ApplicationInstance</span><h3>回复与学习</h3></div>
            <span className={`soul-health-state soul-health-${statusMeta?.tone ?? 'unknown'}`}>{replyStatusLabel(instance)}</span>
          </div>
          <p>{statusMeta?.detail ?? '还没有回复与学习实例；全天匹配状态与这里相互独立。'}</p>
          {instance?.status === 'waiting' && (
            <div className="soul-health-note soul-note-warning">
              <Clock3 size={16} />
              <div><strong>正在等待下一轮</strong><span>{instance.wake_at ? `预计 ${formatDateTime(instance.wake_at)} 继续。` : '运行条件满足后会继续。'}</span></div>
            </div>
          )}
          {instance?.hard_risk && (
            <div className="soul-health-note soul-note-danger">
              <ShieldAlert size={16} />
              <div><strong>当前需要人工确认</strong><span>在确认前，不应把这次回复运行当作已经完成。</span></div>
            </div>
          )}
          {instance?.degraded && (
            <div className="soul-health-note soul-note-warning">
              <AlertTriangle size={16} />
              <div><strong>回复运行需要留意</strong><span>现有实例仍会保留；可展开技术详情查看诊断编号。</span></div>
            </div>
          )}
          {uncertainCount > 0 && (
            <div className="soul-health-note soul-note-warning">
              <TimerReset size={16} />
              <div><strong>{uncertainCount} 个回复周期结果仍不确定</strong><span>不会显示成成功，也不会因此自动重做操作。</span></div>
            </div>
          )}
          <small>最近更新：{formatDateTime(instance?.updated_at)}</small>
        </section>
      </section>

      <section className="soul-controls panel" aria-label="Soul 运行控制" aria-busy={Boolean(mutationInFlight)}>
        <div className="soul-section-heading">
          <div>
            <span className="eyebrow">统一生命周期</span>
            <h3>启动、暂停、继续或停止</h3>
            <p>这四个现有 ApplicationRuntime 控制会同时协调全天匹配/即时开场和回复/学习；没有另一套 scheduler 写按钮。</p>
          </div>
          {mutationInFlight && (
            <span className="soul-command-progress"><LoaderCircle className="spin" size={15} /> {mutationProgressLabel(mutationInFlight)}</span>
          )}
        </div>

        <div className="soul-command-groups">
          <div className="soul-command-row">
            <button type="button" className="button button-primary" onClick={start} disabled={lifecycleControlsLocked || activeInstance}>
              <Play size={15} /> 启动 Soul
            </button>
            <button type="button" className="button button-secondary" onClick={() => command('Pause')} disabled={lifecycleControlsLocked || !canPause}>
              <Pause size={15} /> 暂停
            </button>
            <button type="button" className="button button-secondary" onClick={() => command('Resume')} disabled={lifecycleControlsLocked || !canResume}>
              <Play size={15} /> 继续
            </button>
            <button type="button" className="button button-danger" onClick={() => command('Stop')} disabled={lifecycleControlsLocked || !canStop}>
              <Square size={14} /> 停止
            </button>
          </div>

          <div className="soul-instruction">
            <label htmlFor="soul-supplemental-input">补充指令 <span>仅进入回复与学习</span></label>
            <div>
              <textarea
                id="soul-supplemental-input"
                aria-label="补充指令"
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder="例如：今天只处理已有对话，遇到需要确认的页面就等待"
                maxLength={10_000}
                rows={3}
                disabled={inputControlsLocked || !canInput}
              />
              <button
                type="button"
                className="button button-primary"
                onClick={sendInstruction}
                disabled={inputControlsLocked || !canInput || !instruction.trim()}
              >
                <Send size={15} /> 发送补充指令
              </button>
            </div>
            <small>指令会在安全的运行检查点生效；页面不会回显运行中的对话或身份信息。</small>
          </div>
        </div>

        {mutationError && (
          <div className="soul-command-feedback soul-feedback-danger" role="alert">
            <AlertTriangle size={17} />
            <div><strong>{mutationError}</strong><span>{retryMutation ? '轮询只会读取状态，不会替你重发这次写请求。' : '请刷新状态后再决定下一步。'}</span></div>
            {retryMutation && (
              <button type="button" className="text-button" onClick={() => void executeMutation(retryMutation)} disabled={Boolean(mutationInFlight)}>
                使用同一请求重试
              </button>
            )}
          </div>
        )}

        {feedback && (
          <div className="soul-command-feedback soul-feedback-accepted" role="status">
            <CheckCircle2 size={17} />
            <div><strong>{feedback}</strong><span>最终状态以随后分别读取到的全天运行和回复实例事实为准。</span></div>
          </div>
        )}
      </section>

      {instance && (
        <>
          <section className="soul-stat-grid" aria-label="回复与学习摘要">
            <SoulStat icon={Layers3} label="运行周期" value={String(latestCycle(instance))} detail="已进入的最新周期" />
            <SoulStat icon={CheckCircle2} label="已确认结果" value={String(confirmedOutcomeCount(instance))} detail="确认完成或确认未完成" />
            <SoulStat icon={Clock3} label="等待状态" value={instance.status === 'waiting' ? '正在等待' : '无需等待'} detail={instance.wake_at ? formatDateTime(instance.wake_at) : '没有已安排的唤醒时间'} />
            <SoulStat icon={AlertTriangle} label="不确定结果" value={String(uncertainCount)} detail="不会被当作已成功" />
            <SoulStat icon={BrainCircuit} label="学习版本" value={`第 ${instance.memory_version} 版`} detail="当前使用的经验版本" />
          </section>

          <section className="soul-cycle-summary panel" aria-label="最近周期结果">
            <div className="soul-section-heading">
              <div><span className="eyebrow">结果</span><h3>最近周期</h3><p>这里只显示是否已确认，不显示身份、对话正文、截图或模型提示。</p></div>
              <Activity size={20} />
            </div>
            {recentOutcomes.length ? (
              <ol className="soul-outcome-list">
                {recentOutcomes.map((outcome, index) => (
                  <li key={`${outcome.cycle}-${outcome.created_at}-${index}`}>
                    <span>周期 {outcome.cycle}</span>
                    <strong>{outcomeLabel(outcome.status)}</strong>
                    {outcome.hard_risk && <em>需要确认</em>}
                    <time>{formatDateTime(outcome.created_at)}</time>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="soul-empty"><Activity size={21} /><strong>还没有周期结果</strong><span>运行开始产生确认结果后会显示在这里。</span></div>
            )}
          </section>

          <details className="soul-technical panel">
            <summary>技术详情</summary>
            <p>以下信息用于本地诊断；不包含 Application 的输入、身份、消息、截图、证据或提示词。</p>
            <dl className="soul-technical-grid">
              <div><dt>实例编号</dt><dd>{instance.id}</dd></div>
              <div><dt>Application Profile</dt><dd>{instance.profile_id}</dd></div>
              <div><dt>状态</dt><dd>{technicalStatusLabel(instance.status)}</dd></div>
              <div><dt>修订</dt><dd>{instance.revision}</dd></div>
              <div><dt>输入数量</dt><dd>{instance.input_count}</dd></div>
              <div><dt>计划动作数量</dt><dd>{instance.intent_count}</dd></div>
              <div><dt>结果数量</dt><dd>{instance.outcome_count}</dd></div>
              <div><dt>事件数量</dt><dd>{instance.event_count}</dd></div>
              <div><dt>创建时间</dt><dd>{formatDateTime(instance.created_at)}</dd></div>
              <div><dt>结束时间</dt><dd>{formatDateTime(instance.finished_at)}</dd></div>
              <div><dt>诊断编号</dt><dd>{instance.error_code || '无'}</dd></div>
              <div><dt>最近动作阶段</dt><dd>{instance.intents.at(-1)?.phase || '尚无'}</dd></div>
              <div><dt>全天运行状态码</dt><dd>{scheduler?.code || '尚未读取'}</dd></div>
              <div><dt>全天运行读取时间</dt><dd>{formatDateTime(scheduler?.observed_at)}</dd></div>
            </dl>
          </details>
        </>
      )}
    </section>
  );
}

function SoulStat({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <article className="soul-stat panel">
      <span><Icon size={15} /> {label}</span>
      <strong className="soul-stat-value">{value}</strong>
      <small>{detail}</small>
    </article>
  );
}
