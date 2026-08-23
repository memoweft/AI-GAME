import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  RefreshCw,
  Send,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Square,
} from 'lucide-react';
import { ApiError, api, newIdempotencyKey } from '../api';
import { formatDateTime } from '../format';
import type { GoalRun, GoalTargetOption } from '../types';
import { EmptyState } from './EmptyState';

const ACTIVE_GOAL_KEY = 'ai-game.active-goal-id';
const POLL_MS = 1200;
const SETTLED = new Set([
  'CANDIDATE_COMPLETE', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED', 'UNCERTAIN',
]);

const STATUS: Record<string, { label: string; tone: string; detail: string }> = {
  ACCEPTED: { label: '已接收', tone: 'neutral', detail: '目标已经保存，正在准备环境。' },
  PREFLIGHT: { label: '正在检查环境', tone: 'info', detail: '正在确认模型、设备和占用状态。' },
  PLANNING: { label: '正在规划', tone: 'info', detail: '正在根据当前屏幕整理下一步。' },
  RUNNING: { label: '正在操作', tone: 'info', detail: '智能体正在操作手机并观察结果。' },
  VERIFYING: { label: '正在确认', tone: 'info', detail: '正在用新画面确认操作结果。' },
  RECOVERING: { label: '正在恢复', tone: 'warning', detail: '当前画面与预期不同，正在寻找安全返回路径。' },
  WAITING_CONFIGURATION: { label: '等待环境就绪', tone: 'warning', detail: '有本地运行条件尚未就绪。' },
  WAITING_EXTERNAL: { label: '需要你确认', tone: 'warning', detail: '有一步只能由你在设备或现实环境中完成。' },
  CANDIDATE_COMPLETE: { label: '执行计划已结束', tone: 'warning', detail: '兼容执行器已结束计划，但原始目标尚未通过独立完成验证。' },
  COMPLETED: { label: '已完成', tone: 'success', detail: '原始目标的成功条件已经验证。' },
  PARTIAL: { label: '部分完成', tone: 'warning', detail: '已确认一部分结果，仍有明确未完成项。' },
  FAILED: { label: '未能完成', tone: 'danger', detail: '执行已确定失败。' },
  CANCELLED: { label: '已停止', tone: 'neutral', detail: '目标已按停止请求结束。' },
  UNCERTAIN: { label: '结果不确定', tone: 'warning', detail: '物理或外部结果无法安全确认。' },
};

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : '请求没有完成，请稍后重试。';
}

function statusMeta(goal: GoalRun) {
  return STATUS[goal.execution_status] ?? STATUS.ACCEPTED;
}

export function GoalWorkspace() {
  const [goals, setGoals] = useState<GoalRun[]>([]);
  const [active, setActive] = useState<GoalRun | null>(null);
  const [goalText, setGoalText] = useState('');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const merge = useCallback((next: GoalRun) => {
    setActive(next);
    setGoals((current) => [next, ...current.filter((item) => item.id !== next.id)]);
    window.localStorage.setItem(ACTIVE_GOAL_KEY, next.id);
  }, []);

  const load = useCallback(async () => {
    try {
      const result = await api.getGoals(100);
      setGoals(result.items);
      const remembered = window.localStorage.getItem(ACTIVE_GOAL_KEY);
      const selected = result.items.find((item) => item.id === remembered) ?? result.items[0] ?? null;
      setActive(selected);
      if (selected) window.localStorage.setItem(ACTIVE_GOAL_KEY, selected.id);
      setError(null);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!active || SETTLED.has(active.execution_status)) return;
    const id = window.setInterval(() => {
      void api.getGoal(active.id).then(merge).catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [active, merge]);

  const create = async () => {
    const normalized = goalText.trim();
    if (!normalized) return;
    setCreating(true);
    setError(null);
    try {
      const next = await api.createGoal(normalized, newIdempotencyKey());
      merge(next);
      setGoalText('');
    } catch (caught) {
      setError(message(caught));
    } finally {
      setCreating(false);
    }
  };

  const act = async (operation: () => Promise<GoalRun>) => {
    setWorking(true);
    setError(null);
    try { merge(await operation()); } catch (caught) { setError(message(caught)); }
    finally { setWorking(false); }
  };

  const status = active ? statusMeta(active) : null;
  const canStop = Boolean(active?.binding.task_id && !SETTLED.has(active.execution_status));
  const facts = active?.environment_state.facts ?? [];
  const options = active?.environment_state.target_options ?? [];
  const progressFacts = useMemo(
    () => facts.filter((item) => item.state === 'READY').length,
    [facts],
  );

  return (
    <section className="agent-workspace goal-workspace" aria-label="目标中心">
      <section className="panel agent-composer goal-composer">
        <div className="goal-composer-orbit" aria-hidden="true"><span /><span /><Smartphone size={28} /></div>
        <div className="agent-composer-heading">
          <div className="agent-composer-icon"><Sparkles size={22} /></div>
          <div className="goal-composer-copy"><span className="eyebrow">一句话交给本地智能体</span><h2>今天想让手机替你完成什么？</h2><p>只需要说清楚想要的结果。设备、模型和执行方式由 AI Game 自动检查与选择。</p></div>
        </div>
        <div className="goal-capability-strip" aria-label="执行特点"><span>自动检查设备</span><span>逐步观察验证</span><span>随时可以停止</span></div>
        <label className="agent-goal-field">
          <span>目标</span>
          <textarea
            value={goalText}
            onChange={(event) => setGoalText(event.target.value)}
            placeholder="例如：打开设置，查看当前可见的电池信息，告诉我，然后返回桌面。"
            rows={3}
          />
        </label>
        <div className="goal-composer-footer">
          <div className="goal-promise"><ShieldCheck size={16} /><span>只报告经过证据支持的结果</span></div>
          <button className="button button-primary agent-start-button" onClick={() => void create()} disabled={creating || !goalText.trim()}>
            {creating ? <span className="button-spinner" /> : <Send size={17} />}
            {creating ? '正在接收目标…' : '开始执行'}
          </button>
        </div>
        {error && <div className="agent-inline-error" role="alert"><AlertCircle size={17} /><span>{error}</span></div>}
      </section>

      <div className="agent-main-grid goal-main-grid">
        <aside className="panel agent-task-list">
          <div className="agent-panel-heading"><div><h2>最近目标</h2><p>刷新或重新打开后仍可继续查看。</p></div><button className="icon-button" aria-label="刷新目标" onClick={() => void load()}><RefreshCw size={16} /></button></div>
          {loading ? <div className="agent-task-list-loading">正在读取目标…</div> : goals.length ? (
            <div className="agent-task-items">
              {goals.map((item) => {
                const meta = statusMeta(item);
                return <button key={item.id} className={`agent-task-item ${active?.id === item.id ? 'agent-task-item-active' : ''}`} onClick={() => merge(item)}><span className={`agent-task-dot agent-tone-${meta.tone}`} /><span className="agent-task-item-copy"><strong>{item.original_goal}</strong><small>{meta.label} · {formatDateTime(item.updated_at)}</small></span><ChevronRight size={16} /></button>;
              })}
            </div>
          ) : <EmptyState compact icon={Sparkles} title="还没有目标" description="在上方说出想完成的事情即可开始。" />}
        </aside>

        <section className="panel agent-task-detail goal-detail">
          {!active || !status ? <EmptyState icon={Smartphone} title="等待你的第一个目标" description="你不需要先选择设备、模型或运行模式。" /> : <>
            <div className={`agent-task-hero agent-task-hero-${status.tone}`}>
              <div className="agent-task-hero-title"><div className="agent-task-state-icon">{active.execution_status === 'COMPLETED' ? <CheckCircle2 size={22} /> : active.execution_status.startsWith('WAITING') ? <CirclePause size={22} /> : <Smartphone size={22} />}</div><div><span className={`agent-status agent-tone-${status.tone}`}>{status.label}</span><h2>{active.original_goal}</h2><p>{active.waiting_reason?.message || active.result_summary || status.detail}</p></div></div>
              <div className="agent-task-meta"><span>目标 {active.id.slice(0, 8)}</span>{active.binding.target_id && <span>设备 {active.binding.target_id.replace(/^adb:/, '')}</span>}</div>
              {canStop && <button className="button button-secondary button-small" disabled={working} onClick={() => void act(() => api.stopGoal(active.id, newIdempotencyKey()))}><Square size={14} /> 停止</button>}
            </div>

            {active.waiting_reason && <div className="goal-gate" role="status"><CirclePause size={20} /><div><strong>{active.waiting_reason.code === 'TARGET_SELECTION_REQUIRED' ? '请选择这次使用的设备' : '完成这一步后可以继续'}</strong><p>{active.waiting_reason.message}</p></div>{active.waiting_reason.code !== 'TARGET_SELECTION_REQUIRED' && <button className="button button-primary button-small" disabled={working} onClick={() => void act(() => api.retryGoalPreflight(active.id))}><RefreshCw size={14} /> 重新检查</button>}</div>}

            {options.length > 0 && <div className="goal-target-options">{options.map((option: GoalTargetOption) => <button key={option.target_id} disabled={working} onClick={() => void act(() => api.selectGoalTarget(active.id, option.target_id))}><Smartphone size={18} /><span><strong>{option.name}</strong><small>{option.connection}</small></span><ChevronRight size={16} /></button>)}</div>}

            <div className="agent-progress-grid goal-progress-grid"><div><span>环境检查</span><strong>{active.environment_state.state}</strong><small>{progressFacts}/{facts.length || 0} 项已确认就绪</small></div><div><span>执行绑定</span><strong>{active.binding.task_id ? '已建立' : '尚未建立'}</strong><small>{active.binding.kind}</small></div><div><span>结果验证</span><strong>{active.execution_status === 'COMPLETED' ? '已独立验证' : active.execution_status === 'CANDIDATE_COMPLETE' ? '等待独立验证' : '随执行更新'}</strong><small>{active.binding.completion_gate}</small></div></div>

            {active.verified_facts.length > 0 && <div className="goal-verified"><strong><CheckCircle2 size={16} /> 已验证结果</strong>{active.verified_facts.map((fact) => <p key={fact}>{fact}</p>)}</div>}

            {active.uncompleted_items.length > 0 && <div className="goal-uncompleted"><strong>尚未验证</strong>{active.uncompleted_items.map((item) => <p key={item}>{item}</p>)}</div>}

            <details className="agent-technical-details"><summary><span>环境与完成证据</span><small>设备、模型、冻结标准和独立裁决</small></summary><div className="agent-technical-body goal-evidence-list"><section><h3>冻结成功标准</h3>{active.goal_specification.success_criteria.length ? active.goal_specification.success_criteria.map((criterion) => <article key={criterion.id}><strong>{criterion.description}</strong><span>{active.completion_assessment?.criteria.find((item) => item.criterion_id === criterion.id)?.satisfied ? '已验证' : '待验证'}</span><p>原句：{criterion.source_quote} · {criterion.evidence_requirement}</p></article>) : <p>尚未冻结成功标准。</p>}</section><section><h3>预检事实</h3>{facts.length ? facts.map((fact) => <article key={fact.capability}><strong>{fact.capability}</strong><span>{fact.state}</span><p>{fact.detail}</p></article>) : <p>尚未记录环境事实。</p>}</section><section><h3>兼容绑定</h3><p>类型：{active.binding.kind}</p><p>状态：{active.binding.state}</p><p>任务：{active.binding.task_id || '尚未创建'}</p><p>完成裁决：{active.completion_assessment ? `revision ${active.completion_assessment.revision} · ${active.completion_assessment.verdict}` : '尚未执行'}</p></section></div></details>
          </>}
        </section>
      </div>
    </section>
  );
}
