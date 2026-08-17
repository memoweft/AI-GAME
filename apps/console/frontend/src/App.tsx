import { useCallback, useEffect, useState } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  AlertCircle,
  AlertTriangle,
  Bot,
  Check,
  CirclePause,
  Gauge,
  Heart,
  Info,
  Laptop,
  ListTodo,
  Menu,
  MonitorCog,
  MonitorSmartphone,
  RefreshCw,
  Settings,
  ShieldCheck,
  Smartphone,
  TerminalSquare,
  X,
  XCircle,
} from 'lucide-react';
import { API_BASE, ApiError, api } from './api';
import { EmptyState } from './components/EmptyState';
import { StatusBadge } from './components/StatusBadge';
import { CloudModelSettings } from './components/CloudModelSettings';
import { MobileTaskWorkspace } from './components/MobileTaskWorkspace';
import { SoulWorkspace } from './components/SoulWorkspace';
import { compactId, formatDateTime } from './format';
import { capabilityRole, executorCapability, modelCapability } from './compat';
import {
  runtimeStatusMeta,
  targetStatusMeta,
} from './status';
import type {
  RuntimeCapability,
  RuntimeInfo,
  Target,
} from './types';

type PageId = 'agent' | 'soul' | 'targets' | 'settings';
type ResourceKey = 'targets' | 'runtime';

interface NavItem {
  id: PageId;
  label: string;
  description: string;
  icon: LucideIcon;
}

const NAV_ITEMS: NavItem[] = [
  { id: 'agent', label: '一句话开始', description: '说出目标，让手机智能体替你完成', icon: ListTodo },
  { id: 'soul', label: 'Soul', description: 'AI Game 中可长期运行的一项应用功能', icon: Heart },
  { id: 'targets', label: '设备', description: '查看模拟器、真机和平板连接', icon: MonitorSmartphone },
  { id: 'settings', label: '设置', description: '模型、执行器与连接配置', icon: Settings },
];

function toMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '暂时无法取得数据，请稍后重试。';
}

function capabilityStatus(capability?: RuntimeCapability): string {
  return capability?.status || (capability?.configured ? 'unknown' : 'not_configured');
}

function isExecutionReady(runtime: RuntimeInfo | null): boolean {
  if (!runtime) return false;
  const model = modelCapability(runtime);
  const executor = executorCapability(runtime);
  return Boolean(model?.configured && model.status === 'ready' && executor?.configured && executor.status === 'ready');
}

export default function App() {
  const [page, setPage] = useState<PageId>('agent');
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [targets, setTargets] = useState<Target[]>([]);
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [errors, setErrors] = useState<Partial<Record<ResourceKey, string>>>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [toast, setToast] = useState<{ tone: 'success' | 'warning' | 'danger'; title: string; detail: string } | null>(null);

  const loadAll = useCallback(async (mode: 'initial' | 'refresh' = 'refresh') => {
    mode === 'initial' ? setLoading(true) : setRefreshing(true);
    const requests = await Promise.allSettled([
      api.getTargets(),
      api.getRuntime(),
    ]);

    const nextErrors: Partial<Record<ResourceKey, string>> = {};
    const keys: ResourceKey[] = ['targets', 'runtime'];
    requests.forEach((result, index) => {
      if (result.status === 'rejected') nextErrors[keys[index]] = toMessage(result.reason);
    });

    if (requests[0].status === 'fulfilled') setTargets(requests[0].value.items);
    if (requests[1].status === 'fulfilled') setRuntime(requests[1].value);

    setErrors(nextErrors);
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void loadAll('initial');
  }, [loadAll]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 6500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const currentNav = NAV_ITEMS.find((item) => item.id === page) ?? NAV_ITEMS[0];
  const effectiveRuntime = runtime;
  const executionReady = isExecutionReady(effectiveRuntime);
  const connected = Object.keys(errors).length < 2 && !loading;

  const navigate = (nextPage: PageId) => {
    setPage(nextPage);
    setMobileNavOpen(false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <div className="app-shell">
      {mobileNavOpen && <button className="mobile-nav-backdrop" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <aside className={`sidebar ${mobileNavOpen ? 'sidebar-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><TerminalSquare size={22} strokeWidth={2.2} /></div>
          <div><strong>AI Game</strong><span>手机智能体控制台</span></div>
          <button className="icon-button sidebar-close" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)}><X size={20} /></button>
        </div>

        <div className="sidebar-context">
          <span className={`connection-dot ${connected ? 'connection-local' : 'connection-offline'}`} />
          <div><strong>{loading ? '正在连接…' : connected ? '本地服务' : '服务未连接'}</strong><span>{connected ? '控制服务仅监听本机' : '请启动控制服务'}</span></div>
        </div>

        <nav className="main-nav" aria-label="主导航">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${page === id ? 'nav-item-active' : ''}`}
              aria-current={page === id ? 'page' : undefined}
              onClick={() => navigate(id)}
            >
              <Icon size={19} aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-runtime">
            <div><Bot size={16} /><span>模型 / 执行器</span></div>
            <StatusBadge meta={runtimeStatusMeta(executionReady ? 'ready' : 'not_configured')} compact />
          </div>
            <p>{executionReady ? '本地模型与设备通道已就绪' : '执行通道尚未就绪'}</p>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-title">
            <button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setMobileNavOpen(true)}><Menu size={21} /></button>
            <div><h1>{currentNav.label}</h1><p>{currentNav.description}</p></div>
          </div>
          <div className="topbar-actions">
            <button className="icon-button refresh-button" aria-label="刷新数据" title="刷新数据" onClick={() => void loadAll()} disabled={refreshing}>
              <RefreshCw size={18} className={refreshing ? 'spin' : ''} />
            </button>
          </div>
        </header>

        <main className="page-content">
          {page === 'agent' && <MobileTaskWorkspace targets={targets} />}
          {page === 'soul' && <SoulWorkspace />}
          {page === 'targets' && (
            <TargetsPage targets={targets} loading={loading} error={errors.targets} onRetry={() => void loadAll()} onTargets={setTargets} onToast={setToast} onRefresh={() => void loadAll()} />
          )}
          {page === 'settings' && <SettingsPage runtime={effectiveRuntime} loading={loading} error={errors.runtime} onRetry={() => void loadAll()} onRuntimeChanged={() => loadAll()} />}
        </main>
      </div>

      {toast && (
        <div className={`toast toast-${toast.tone}`} role="status">
          <div className="toast-icon" aria-hidden="true">
            {toast.tone === 'success' ? <Check size={18} /> : toast.tone === 'warning' ? <AlertTriangle size={18} /> : <XCircle size={18} />}
          </div>
          <div><strong>{toast.title}</strong><span>{toast.detail}</span></div>
          <button className="icon-button" aria-label="关闭提示" onClick={() => setToast(null)}><X size={17} /></button>
        </div>
      )}
    </div>
  );
}

function PanelHeader({ title, description, action }: { title: string; description: string; action?: React.ReactNode }) {
  return <div className="panel-header"><div><h2>{title}</h2><p>{description}</p></div>{action}</div>;
}

interface TargetsPageProps {
  targets: Target[];
  loading: boolean;
  error?: string;
  onRetry: () => void;
  onTargets: (targets: Target[]) => void;
  onToast: (toast: { tone: 'success' | 'warning' | 'danger'; title: string; detail: string }) => void;
  onRefresh: () => void;
}

const TARGET_CAPABILITY_LABELS: Record<string, string> = {
  android_adb: 'Android 设备控制',
  adb: 'Android 设备控制',
  screen_capture: '读取屏幕',
  touch_input: '点击与滑动',
  ascii_text_input: '英文、数字与基础符号输入',
  text_input: '输入文字',
};

function targetCapabilityLabel(capability: string): string {
  return TARGET_CAPABILITY_LABELS[capability]
    ?? capability.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function targetConnectionLabel(target: Target): string {
  const reported = target.details?.connection_type;
  if (reported === 'emulator') return 'Android 模拟器';
  if (reported === 'usb') return 'USB 真机 / 平板';
  if (reported === 'wireless') return 'Wi-Fi 调试';
  if (target.kind === 'android' || target.platform === 'android') return 'Android 设备';
  return '本机设备';
}

function TargetsPage({ targets, loading, error, onRetry, onTargets, onToast, onRefresh }: TargetsPageProps) {
  const [discovering, setDiscovering] = useState(false);
  const discover = async () => {
    setDiscovering(true);
    try {
      const result = await api.discoverTargets();
      onTargets(result.items);
      onToast({ tone: result.discovery.device_count > 0 ? 'success' : 'warning', title: result.discovery.device_count > 0 ? `发现 ${result.discovery.device_count} 个设备` : '发现完成，未找到新设备', detail: result.discovery.message || '设备列表已更新。' });
      onRefresh();
    } catch (caught) {
      onToast({ tone: 'danger', title: '设备发现未完成', detail: toMessage(caught) });
    } finally {
      setDiscovering(false);
    }
  };

  if (loading) return <CardSkeleton count={4} />;
  const readyCount = targets.filter((target) => target.status === 'ready').length;
  return (
    <div className="page-stack">
      {error && <PageError message={error} onRetry={onRetry} compact />}
      <div className="section-intro"><div><h2>设备与连接</h2><p>{readyCount} 个可用，共 {targets.length} 个设备。刷新连接只读取设备状态，不会操作屏幕。</p></div><button className="button button-primary" onClick={() => void discover()} disabled={discovering}>{discovering ? <span className="button-spinner" /> : <RefreshCw size={17} />}{discovering ? '正在刷新…' : '刷新设备'}</button></div>
      {targets.length ? (
        <div className="target-grid">
          {targets.map((target) => {
            const isAndroid = target.kind === 'android' || target.platform === 'android' || target.kind === 'emulator';
            const detail = target.detail || (typeof target.details?.detail === 'string' ? target.details.detail : null);
            const address = target.external_id || target.address || (typeof target.details?.address === 'string' ? target.details.address : null);
            return (
              <article className="target-card panel" key={target.id}>
                <div className="target-visual">{isAndroid ? <Smartphone size={27} /> : <Laptop size={27} />}<span className={`target-pulse target-pulse-${targetStatusMeta(target.status).tone}`} /></div>
                <div className="target-head"><div><h3>{target.name}</h3><p>{targetConnectionLabel(target)}</p></div><StatusBadge meta={targetStatusMeta(target.status)} /></div>
                <dl className="detail-list"><div><dt>连接标识</dt><dd>{address || '由本地服务管理'}</dd></div><div><dt>最近确认</dt><dd>{formatDateTime(target.last_seen_at || target.updated_at)}</dd></div><div><dt>设备编号</dt><dd title={target.id}>{compactId(target.id)}</dd></div></dl>
                {detail && <div className="target-detail"><Info size={15} /> {detail}</div>}
                <div className="capability-tags">{target.capabilities.length ? target.capabilities.map((item) => <span key={item}>{targetCapabilityLabel(item)}</span>) : <span>能力尚未报告</span>}</div>
              </article>
            );
          })}
        </div>
      ) : <section className="panel"><EmptyState icon={MonitorSmartphone} title="尚未发现 Android 设备" description="启动模拟器，或按下方提示连接真机和平板，然后刷新设备。" action={<button className="button button-primary" onClick={() => void discover()} disabled={discovering}><RefreshCw size={17} /> 刷新设备</button>} /></section>}

      <section className="panel target-connect-guide">
        <PanelHeader title="连接真机或平板" description="首次连接需要在设备上确认调试授权；连接成功后再点击“刷新设备”。" />
        <div className="target-connect-options">
          <article><strong>USB 连接</strong><p>用数据线连接电脑，开启开发者选项和 USB 调试，并在设备上允许这台电脑进行调试。</p></article>
          <article><strong>Wi-Fi 连接</strong><p>Android 11 及以上可在开发者选项中打开无线调试配对；电脑与设备需处于可互通的网络。</p></article>
        </div>
      </section>
    </div>
  );
}

function SettingsPage({ runtime, loading, error, onRetry, onRuntimeChanged }: { runtime: RuntimeInfo | null; loading: boolean; error?: string; onRetry: () => void; onRuntimeChanged: () => Promise<void> | void }) {
  if (loading) return <CardSkeleton count={4} />;
  const capabilities = runtime?.capabilities ?? [];
  const modelReady = capabilityStatus(modelCapability(runtime)) === 'ready';
  const executorReady = capabilityStatus(executorCapability(runtime)) === 'ready';
  const executionReady = modelReady && executorReady;
  const boundaryDetail = executionReady
    ? '本地 GUI 模型与设备执行器均已就绪；对话执行可以持续运行截图、判断、单动作、再观察循环，不设单轮步数上限。'
    : modelReady
      ? '本地 GUI 模型已就绪，但设备执行器尚未接入；文字对话仍可使用，手机操作暂不可用。'
      : executorReady
        ? '设备执行器已就绪，但本地 GUI 模型当前不可用；手机操作暂不可用。'
        : '本地 GUI 模型与设备执行器尚未同时就绪；请先完成连接配置。';
  return (
    <div className="page-stack settings-layout">
      {error && <PageError message={error} onRetry={onRetry} compact />}
      <CloudModelSettings onRuntimeChanged={onRuntimeChanged} />
      <section className="panel settings-panel"><PanelHeader title="运行组件" description="来自本地控制服务的当前状态，不做推测" />{capabilities.length ? <div className="component-list">{capabilities.map((capability) => { const role = capabilityRole(capability.id); return <article className="component-row" key={capability.id}><div className="component-icon">{role === 'model' || role === 'planner' ? <Bot size={20} /> : role === 'executor' ? <MonitorCog size={20} /> : <Gauge size={20} />}</div><div><strong>{capability.name}</strong><span>{capability.detail || (capability.configured ? '组件已配置' : '组件尚未配置')}</span>{capability.blocker && <small>{typeof capability.blocker === 'string' ? capability.blocker : capability.blocker.message}</small>}</div><StatusBadge meta={runtimeStatusMeta(capabilityStatus(capability))} /></article>; })}</div> : <EmptyState compact icon={MonitorCog} title="没有组件状态" description="控制服务尚未报告运行组件。" />}</section>
      <section className="panel settings-panel"><PanelHeader title="API 连接" description="前端连接本地控制服务所使用的地址" /><dl className="settings-list"><div><dt>基础路径</dt><dd><code>{API_BASE}</code></dd></div><div><dt>连接方式</dt><dd>同源本地请求</dd></div><div><dt>客户端标识</dt><dd><code>console-v1</code></dd></div><div><dt>远程跨域访问</dt><dd>未开放</dd></div></dl></section>
      <section className="panel settings-panel"><PanelHeader title="本阶段能力边界" description="控制台不会把未发生的动作显示为成功" /><div className="boundary-list"><div><Check size={17} /><span><strong>可以使用</strong>直接和本地模型对话；云端配置完成后，可由云端对话、本地模型自动操作 Android 设备。</span></div><div>{executionReady ? <Check size={17} /> : <CirclePause size={17} />}<span><strong>{executionReady ? '设备闭环已接入' : '暂不可执行'}</strong>{boundaryDetail}</span></div><div><ShieldCheck size={17} /><span><strong>测试模式全部放开</strong>对话执行不设单轮步数上限，账号、验证码、实名、付款、CAPTCHA、权限、法律确认或无法判断的页面也不会自动暂停；循环持续到目标完成、主动停止或运行故障。</span></div></div></section>
    </div>
  );
}

function PageError({ message, onRetry, compact = false }: { message: string; onRetry: () => void; compact?: boolean }) {
  return <div className={`page-error ${compact ? 'page-error-compact' : ''}`} role="alert"><AlertCircle size={20} /><div><strong>数据暂时不可用</strong><span>{message}</span></div><button className="button button-secondary button-small" onClick={onRetry}><RefreshCw size={14} /> 重试</button></div>;
}

function CardSkeleton({ count }: { count: number }) {
  return <div className="workflow-grid" aria-label="正在加载">{Array.from({ length: count }, (_, index) => <div className="skeleton skeleton-card" key={index} />)}</div>;
}
