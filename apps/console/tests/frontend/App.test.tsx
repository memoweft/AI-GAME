import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../../frontend/src/App';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

Object.defineProperty(window, 'scrollTo', {
  configurable: true,
  value: vi.fn(),
});

const runtime = {
  overall_status: 'ready',
  capabilities: [
    { id: 'model', name: '本地 GUI 模型', status: 'ready', configured: true, detail: '模型已就绪', blocker: null },
    { id: 'executor', name: 'ADB 执行器', status: 'ready', configured: true, detail: '设备通道已就绪', blocker: null },
  ],
};

function target(overrides: Record<string, unknown>) {
  return {
    id: 'adb:emulator-5554',
    name: 'Pixel 模拟器',
    kind: 'android',
    status: 'ready',
    address: 'emulator-5554',
    detail: 'ADB 已连接。',
    capabilities: ['android_adb', 'screen_capture', 'touch_input', 'ascii_text_input'],
    source: 'adb',
    external_id: 'emulator-5554',
    details: {
      address: 'emulator-5554',
      detail: 'ADB 已连接。',
      capabilities: ['android_adb', 'screen_capture', 'touch_input', 'ascii_text_input'],
      connection_type: 'emulator',
      adb_state: 'device',
      properties: {},
    },
    discovered_at: '2026-08-10T00:00:00Z',
    last_seen_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    ...overrides,
  };
}

const targets = [
  target({}),
  target({
    id: 'adb:R58M1234AB',
    name: 'Galaxy 平板',
    address: 'R58M1234AB',
    external_id: 'R58M1234AB',
    details: {
      address: 'R58M1234AB',
      detail: 'USB 调试已授权。',
      capabilities: ['android_adb', 'screen_capture', 'touch_input'],
      connection_type: 'usb',
      adb_state: 'device',
      properties: { usb: '1' },
    },
    capabilities: ['android_adb', 'screen_capture', 'touch_input'],
  }),
  target({
    id: 'adb:192.168.1.22:5555',
    name: '客厅手机',
    address: '192.168.1.22:5555',
    external_id: '192.168.1.22:5555',
    details: {
      address: '192.168.1.22:5555',
      detail: '无线调试已连接。',
      capabilities: ['android_adb', 'screen_capture'],
      connection_type: 'wireless',
      adb_state: 'device',
      properties: {},
    },
    capabilities: ['android_adb', 'screen_capture'],
  }),
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function installApiMock() {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    calls.push({ url, init });
    if (url.endsWith('/targets') && method === 'GET') return json({ items: targets, count: targets.length });
    if (url.endsWith('/runtime') && method === 'GET') return json(runtime);
    if (url.endsWith('/tasks?limit=100') && method === 'GET') return json({ items: [], count: 0 });
    if (url.endsWith('/application-instances?limit=100') && method === 'GET') return json({ items: [], count: 0 });
    if (url.endsWith('/application-profiles/soul-reply-v1/scheduler') && method === 'GET') return json({
      profile_id: 'soul-reply-v1',
      state: 'stopped',
      desired_state: 'stopped',
      effective_state: 'stopped',
      controller_matches: true,
      code: 'scheduler_stopped',
      observed_at: '2026-08-10T00:00:00Z',
    });
    if (url.endsWith('/settings/cloud') && method === 'GET') return json({ endpoint: null, model: null, has_api_key: false, configured: false, credential_source: 'none', status: 'not_configured', detail: '尚未配置云端模型。', revision: 0, updated_at: null });
    if (url.endsWith('/overview') && method === 'GET') return json({ summary: { workflow_count: 0, target_count: targets.length, active_run_count: 0, pending_approval_count: 0 }, run_status_counts: {}, recent_runs: [], runtime });
    if (url.includes('/events?') && method === 'GET') return json({ items: [], count: 0 });
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls };
}

describe('控制台统一入口', () => {
  it('默认就是一句话开始，一级导航仅保留任务、Soul、设备和设置', async () => {
    const { calls } = installApiMock();
    render(<App />);

    expect(await screen.findByRole('region', { name: '一句话开始' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: '主导航' });
    expect(within(nav).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual([
      '一句话开始',
      'Soul',
      '设备',
      '设置',
    ]);
    expect(within(nav).queryByText('总览')).not.toBeInTheDocument();
    expect(within(nav).queryByText('活动记录')).not.toBeInTheDocument();
    expect(within(nav).queryByText('对话执行')).not.toBeInTheDocument();
    expect(within(nav).queryByText('游戏学习')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/mode|profile|transition|reward|skill_id/i);

    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/tasks?limit=100'))).toBe(true));
    expect(calls.some((call) => call.url.includes('/chat/'))).toBe(false);
    expect(calls.some((call) => call.url.includes('/learning/'))).toBe(false);
    expect(calls.some((call) => call.url.endsWith('/overview'))).toBe(false);
    expect(calls.some((call) => call.url.includes('/events?'))).toBe(false);
  });

  it('Soul 作为应用入口在控制台内打开，仍不使用 iframe', async () => {
    const user = userEvent.setup();
    installApiMock();
    render(<App />);

    await screen.findByRole('region', { name: '一句话开始' });
    await user.click(screen.getByRole('button', { name: 'Soul' }));
    expect(await screen.findByRole('region', { name: 'Soul 应用' })).toBeInTheDocument();
    expect(screen.getByText('Soul 是 AI Game 可以长期运行的一项应用功能。')).toBeInTheDocument();
    expect(screen.queryByText(/dating-copilot/)).not.toBeInTheDocument();
    expect(document.querySelector('iframe')).toBeNull();
  });

  it('设备页区分模拟器、USB 与 Wi-Fi，并把能力翻译成人话', async () => {
    const user = userEvent.setup();
    installApiMock();
    render(<App />);

    await screen.findByRole('region', { name: '一句话开始' });
    await user.click(screen.getByRole('button', { name: '设备' }));
    expect(await screen.findByRole('heading', { name: '设备与连接' })).toBeInTheDocument();
    expect(screen.getByText('Android 模拟器')).toBeInTheDocument();
    expect(screen.getByText('USB 真机 / 平板')).toBeInTheDocument();
    expect(screen.getByText('Wi-Fi 调试')).toBeInTheDocument();
    expect(screen.getAllByText('读取屏幕').length).toBeGreaterThan(0);
    expect(screen.getAllByText('点击与滑动').length).toBeGreaterThan(0);
    expect(screen.getByText('英文、数字与基础符号输入')).toBeInTheDocument();
    expect(screen.queryByText('android_adb')).not.toBeInTheDocument();
    expect(screen.queryByText('screen_capture')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '连接真机或平板' })).toBeInTheDocument();
    expect(screen.getByText(/开启开发者选项和 USB 调试/)).toBeInTheDocument();
    expect(screen.getByText(/无线调试配对/)).toBeInTheDocument();
  });

  it('设置保持独立入口并继续呈现模型与执行器事实', async () => {
    const user = userEvent.setup();
    installApiMock();
    render(<App />);

    await screen.findByRole('region', { name: '一句话开始' });
    await user.click(screen.getByRole('button', { name: '设置' }));
    expect(await screen.findByText('本地 GUI 模型')).toBeInTheDocument();
    expect(screen.getByText('ADB 执行器')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '云端模型配置' })).toBeInTheDocument();
  });
});
