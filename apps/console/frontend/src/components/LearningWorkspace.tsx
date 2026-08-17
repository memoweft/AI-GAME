import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Database,
  Gauge,
  History,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Square,
  Target,
} from 'lucide-react';
import { ApiError, api } from '../api';
import type {
  CreateLearningJobRequest,
  LearningJob,
  LearningProfile,
} from '../types';

const DEFAULT_PROFILE_ID = 'stzb-tutorial-v1';
const TERMINAL_STATUSES = new Set([
  'completed',
  'succeeded',
  'failed',
  'cancelled',
  'stopped',
  'rejected',
]);

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : '学习工作台暂时无法取得数据，请稍后重试。';
}

function requestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `learning-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function phaseOf(job: LearningJob | null): string {
  return job?.phase?.trim().toLowerCase() || '';
}

function resultOf(job: LearningJob | null): string {
  return job?.result?.trim().toLowerCase() || '';
}

function statusOf(job: LearningJob | null): string {
  const phase = phaseOf(job);
  if (phase === 'terminal') return resultOf(job) || 'terminal';
  return phase || job?.status?.trim().toLowerCase() || 'unknown';
}

function isTerminal(job: LearningJob | null): boolean {
  return phaseOf(job) === 'terminal' || TERMINAL_STATUSES.has(job?.status?.trim().toLowerCase() || '');
}

function terminalCopy(job: LearningJob | null): { title: string; detail: string; tone: string } {
  const phase = phaseOf(job);
  const result = resultOf(job);
  const status = statusOf(job);
  if (phase === 'stopping' || status === 'stopping') {
    return { title: '正在停止', detail: '停止请求已提交，正在等待后端确认；当前还不能视为已停止。', tone: 'warning' };
  }
  if (phase === 'terminal') {
    if (result === 'learned') {
      return { title: '学习已完成', detail: '后端已确认本轮形成学习成果；仍请结合 outcome 与验证指标理解结果。', tone: 'success' };
    }
    if (result === 'not_learned') {
      return { title: '未形成学习成果', detail: '本轮已到达终态，但后端没有确认可写入策略的学习成果。', tone: 'warning' };
    }
    if (result === 'failed') {
      return { title: '学习失败', detail: '本轮已结束，具体原因见下方 detail / error。', tone: 'danger' };
    }
    if (result === 'stopped_uncertain') {
      return { title: '停止状态不确定', detail: '本轮已到达终态，但后端无法确认设备控制已完全停止；请人工检查。', tone: 'danger' };
    }
    if (result === 'stopped') {
      return { title: '学习已停止', detail: '后端已确认停止，不会把停止请求本身冒充终态。', tone: 'neutral' };
    }
    return { title: '学习已结束', detail: '本轮已到达终态，请以 result、outcome 与 detail 判断实际结果。', tone: 'neutral' };
  }
  if (status === 'completed' || status === 'succeeded') {
    return { title: '学习已完成', detail: '本轮已到达终态，请以 outcome 与验证指标判断实际结果。', tone: 'success' };
  }
  if (status === 'failed' || status === 'rejected') {
    return { title: '学习失败', detail: '本轮已结束，具体原因见下方 detail / error。', tone: 'danger' };
  }
  if (status === 'cancelled' || status === 'stopped') {
    return { title: '学习已停止', detail: '后端已确认停止，不会把停止请求本身冒充终态。', tone: 'neutral' };
  }
  if (status === 'queued' || status === 'accepted' || status === 'pending') {
    return { title: '等待开始', detail: '学习任务已受理，正在等待执行资源。', tone: 'info' };
  }
  if (status === 'unknown') {
    return { title: '尚未开始学习', detail: '输入一句话目标后开始；运行事实会从本地后端读取。', tone: 'neutral' };
  }
  return { title: '学习进行中', detail: '工作台每秒读取一次最新状态，不根据前端计时推断完成。', tone: 'info' };
}

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

function metric(job: LearningJob | null, ...keys: string[]): unknown {
  if (!job) return null;
  for (const key of keys) {
    const direct = job[key];
    if (direct !== null && direct !== undefined) return direct;
    const nested = job.metrics?.[key];
    if (nested !== null && nested !== undefined) return nested;
  }
  return null;
}

function budgetText(profile: LearningProfile | null): string {
  if (!profile) return '等待 Profile 数据';
  if (profile.budget_summary) return profile.budget_summary;
  if (typeof profile.budget === 'string') return profile.budget;
  const budget = typeof profile.budget === 'object' ? profile.budget : null;
  const transitions = profile.max_transitions ?? profile.max_actions ?? budget?.max_transitions ?? budget?.max_steps;
  const seconds = profile.max_duration_seconds ?? budget?.max_duration_seconds ?? budget?.max_episode_seconds;
  const parts = [
    transitions == null ? null : `${transitions} 个 Transition`,
    seconds == null ? null : `${seconds} 秒`,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : '由后端 Profile 冻结';
}

function safetyText(profile: LearningProfile | null): string {
  if (!profile) return '等待 Profile 数据';
  if (profile.safety_summary) return profile.safety_summary;
  if (Array.isArray(profile.safety_boundary)) return profile.safety_boundary.join('；');
  if (profile.safety_boundary) return profile.safety_boundary;
  if (profile.allowed_actions?.length) return `仅允许 ${profile.allowed_actions.join(' / ')}`;
  return '仅限已授权训练 / 自定义 / 沙盒环境';
}

function profileLabel(profile: LearningProfile | null): string {
  return profile?.name || profile?.display_name || profile?.game || (profile ? profile.id : '—');
}

function formatWhen(value?: string | null): string {
  if (!value) return '时间未知';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false });
}

function normalizeItems<T>(payload: unknown, alternate: string): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (!payload || typeof payload !== 'object') return [];
  const value = payload as Record<string, unknown>;
  if (Array.isArray(value.items)) return value.items as T[];
  if (Array.isArray(value[alternate])) return value[alternate] as T[];
  return [];
}

export function LearningWorkspace() {
  const [profiles, setProfiles] = useState<LearningProfile[]>([]);
  const [jobs, setJobs] = useState<LearningJob[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [instruction, setInstruction] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const failedStart = useRef<CreateLearningJobRequest | null>(null);

  const profile = useMemo(() => (
    profiles.find((item) => item.id === DEFAULT_PROFILE_ID)
    ?? profiles.find((item) => profileLabel(item).includes('率土之滨'))
    ?? profiles[0]
    ?? null
  ), [profiles]);

  const selectedJob = useMemo(
    () => jobs.find((item) => item.id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );

  const mergeJob = useCallback((job: LearningJob) => {
    setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
  }, []);

  const loadWorkspace = useCallback(async (quiet = false) => {
    quiet ? setRefreshing(true) : setLoading(true);
    try {
      const [profilePayload, jobPayload] = await Promise.all([
        api.getLearningProfiles(),
        api.getLearningJobs(),
      ]);
      const nextProfiles = normalizeItems<LearningProfile>(profilePayload, 'profiles');
      const nextJobs = normalizeItems<LearningJob>(jobPayload, 'jobs');
      setProfiles(nextProfiles);
      setJobs(nextJobs);
      setSelectedJobId((current) => current && nextJobs.some((job) => job.id === current)
        ? current
        : nextJobs[0]?.id ?? null);
      setError(null);
    } catch (nextError) {
      setError(messageFrom(nextError));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  useEffect(() => {
    if (!selectedJob || isTerminal(selectedJob)) return;
    let disposed = false;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const latest = await api.getLearningJob(selectedJob.id);
        if (!disposed) {
          mergeJob(latest);
          setError(null);
        }
      } catch (nextError) {
        if (!disposed) setError(messageFrom(nextError));
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [mergeJob, selectedJob]);

  const submitStart = useCallback(async (body: CreateLearningJobRequest) => {
    setStarting(true);
    setStartError(null);
    try {
      const created = await api.createLearningJob(body);
      failedStart.current = null;
      mergeJob(created);
      setSelectedJobId(created.id);
    } catch (nextError) {
      failedStart.current = body;
      setStartError(messageFrom(nextError));
    } finally {
      setStarting(false);
    }
  }, [mergeJob]);

  const start = () => {
    const text = instruction.trim();
    if (!text || !profile) return;
    const body: CreateLearningJobRequest = {
      instruction: text,
      client_request_id: requestId(),
      profile_id: profile.id,
      ...(profile.target_id || profile.default_target_id
        ? { target_id: profile.target_id || profile.default_target_id || undefined }
        : {}),
    };
    void submitStart(body);
  };

  const retryStart = () => {
    if (failedStart.current) void submitStart(failedStart.current);
  };

  const stop = async () => {
    if (!selectedJob || isTerminal(selectedJob) || phaseOf(selectedJob) === 'stopping') return;
    setStopping(true);
    setError(null);
    try {
      const latest = await api.stopLearningJob(selectedJob.id);
      mergeJob(latest);
    } catch (nextError) {
      setError(messageFrom(nextError));
    } finally {
      setStopping(false);
    }
  };

  const statusCopy = terminalCopy(selectedJob);
  const policyRevision = selectedJob?.policy_memory_revision
    ?? selectedJob?.policy_memory?.revision
    ?? selectedJob?.metrics?.policy_memory_revision
    ?? selectedJob?.policy_version;
  const policyCount = selectedJob?.policy_memory_count
    ?? selectedJob?.policy_memory?.count
    ?? selectedJob?.metrics?.policy_memory_count;

  return (
    <section className="learning-workspace" aria-label="学习工作台">
      <div className="learning-command panel">
        <div className="learning-command-heading">
          <div className="learning-command-icon" aria-hidden="true"><Sparkles size={22} /></div>
          <div>
            <h2>让 AI 从一轮真实操作中学习</h2>
            <p>默认使用“率土之滨”Profile；一句话描述目标，其余预算与安全约束由本地后端执行。</p>
          </div>
        </div>
        <label className="learning-instruction">
          <span>这次想让它学会什么？</span>
          <textarea
            aria-label="这次想让它学会什么？"
            value={instruction}
            onChange={(event) => {
              setInstruction(event.target.value);
              setStartError(null);
              failedStart.current = null;
            }}
            rows={4}
            maxLength={200}
            placeholder="例如：按照当前教程提示继续教程"
            disabled={starting}
          />
          <small className="learning-instruction-help">支持示例：继续教程、打开任务列表、查看武将列表、返回主界面。最多 200 字。</small>
        </label>
        {startError && (
          <div className="learning-error" role="alert">
            <AlertCircle size={17} />
            <span>{startError}</span>
            <button type="button" className="text-button" onClick={retryStart} disabled={starting}>重试同一请求</button>
          </div>
        )}
        <div className="learning-command-actions">
          <span>{profile ? `Profile：${profileLabel(profile)}` : loading ? '正在读取 Profile…' : '没有可用 Profile'}</span>
          <button
            type="button"
            className="button button-primary learning-start"
            onClick={start}
            disabled={starting || !instruction.trim() || !profile || profile.enabled === false}
          >
            {starting ? <RefreshCw size={18} className="spin" /> : <Play size={18} />}
            {starting ? '正在创建…' : '开始学习'}
          </button>
        </div>
      </div>

      <div className="learning-summary-grid" aria-label="学习配置摘要">
        <article className="learning-summary-card"><Sparkles size={18} /><span>Profile</span><strong>{profileLabel(profile)}</strong><small>{profile ? `${profile.id}${profile.version != null || profile.revision != null ? ` · v${profile.version ?? profile.revision}` : ''}` : '尚未加载'}</small></article>
        <article className="learning-summary-card"><Target size={18} /><span>目标</span><strong>{selectedJob?.instruction || instruction.trim() || '尚未输入'}</strong><small>{profile?.target_name || profile?.target_id || profile?.default_target_id || '由 Profile 绑定目标'}</small></article>
        <article className="learning-summary-card"><Gauge size={18} /><span>预算</span><strong>{budgetText(profile)}</strong><small>运行时不会由前端静默放宽</small></article>
        <article className="learning-summary-card"><ShieldCheck size={18} /><span>安全边界</span><strong>{safetyText(profile)}</strong><small>越界应结束本轮并记录结果</small></article>
      </div>

      {error && <div className="learning-global-error" role="alert"><AlertCircle size={17} /><span>{error}</span><button className="text-button" onClick={() => void loadWorkspace(true)}>重新读取</button></div>}

      <div className="learning-columns">
        <aside className="panel learning-history">
          <div className="learning-section-heading">
            <div><History size={18} /><div><h2>历史 Job</h2><p>切换查看每一轮的真实记录</p></div></div>
            <button className="icon-button" aria-label="刷新学习记录" onClick={() => void loadWorkspace(true)} disabled={refreshing}><RefreshCw size={17} className={refreshing ? 'spin' : ''} /></button>
          </div>
          <div className="learning-job-list">
            {loading && <p className="learning-list-empty">正在读取…</p>}
            {!loading && jobs.length === 0 && <p className="learning-list-empty">还没有学习记录。</p>}
            {jobs.map((job) => (
              <button
                type="button"
                key={job.id}
                className={`learning-job-item ${selectedJobId === job.id ? 'active' : ''}`}
                onClick={() => setSelectedJobId(job.id)}
              >
                <span className={`learning-status-dot status-${statusOf(job)}`} />
                <span><strong>{job.instruction || job.profile_name || job.profile_id || '未命名学习任务'}</strong><small>{formatWhen(job.updated_at || job.created_at)} · {statusOf(job)}</small></span>
              </button>
            ))}
          </div>
        </aside>

        <div className="learning-detail">
          <article className={`panel learning-state-banner tone-${statusCopy.tone}`} role="status">
            <div>{statusCopy.tone === 'success' ? <CheckCircle2 size={21} /> : <Sparkles size={21} />}</div>
            <span><strong>{statusCopy.title}</strong><small>{statusCopy.detail}</small></span>
            {selectedJob && !isTerminal(selectedJob) && (
              <button
                type="button"
                className="button button-danger"
                onClick={() => void stop()}
                disabled={stopping || phaseOf(selectedJob) === 'stopping'}
              >
                <Square size={16} />
                {stopping || phaseOf(selectedJob) === 'stopping' ? '正在停止' : '停止学习'}
              </button>
            )}
          </article>

          <article className="panel learning-facts">
            <div className="learning-section-heading"><div><Database size={18} /><div><h2>运行事实</h2><p>字段值直接来自 Job detail</p></div></div></div>
            <dl className="learning-fact-grid">
              <div><dt>status</dt><dd>{display(selectedJob?.status)}</dd></div>
              <div><dt>phase</dt><dd>{display(selectedJob?.phase)}</dd></div>
              <div><dt>result</dt><dd>{display(selectedJob?.result)}</dd></div>
              <div><dt>outcome</dt><dd>{display(selectedJob?.outcome)}</dd></div>
              <div><dt>control_state</dt><dd>{display(selectedJob?.control_state)}</dd></div>
              <div><dt>policy_state</dt><dd>{display(selectedJob?.policy_state)}</dd></div>
            </dl>
          </article>

          <article className="panel learning-metrics">
            <div className="learning-section-heading"><div><Gauge size={18} /><div><h2>学习指标</h2><p>传输、验证和策略状态分开显示</p></div></div></div>
            <div className="learning-metric-grid">
              <div><span>Transition 使用数</span><strong>{display(metric(selectedJob, 'transition_count', 'transitions_used'))}</strong></div>
              <div><span>总 Reward</span><strong>{display(metric(selectedJob, 'total_reward'))}</strong></div>
              <div><span>verified successes</span><strong>{display(metric(selectedJob, 'verified_successes'))}</strong></div>
              <div><span>PolicyMemory revision</span><strong>{display(policyRevision)}</strong></div>
              <div><span>PolicyMemory count</span><strong>{display(policyCount)}</strong></div>
            </div>
          </article>

          <article className="panel learning-detail-copy">
            <h2>detail / error</h2>
            <dl>
              <div><dt>detail</dt><dd>{display(selectedJob?.detail)}</dd></div>
              <div><dt>error</dt><dd className={selectedJob?.error || selectedJob?.error_code ? 'danger' : ''}>{display(selectedJob?.error || selectedJob?.error_code)}</dd></div>
            </dl>
          </article>
        </div>
      </div>
    </section>
  );
}
