import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CloudModelSettings } from '../../frontend/src/components/CloudModelSettings';
import type { CloudChatConfig } from '../../frontend/src/types';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function configured(overrides: Partial<CloudChatConfig> = {}): CloudChatConfig {
  return {
    endpoint: 'https://cloud.example.test/v1',
    model: 'example-planner',
    has_api_key: true,
    configured: true,
    credential_source: 'console',
    status: 'unknown',
    detail: '配置已保存；首次发送时验证连接。',
    revision: 3,
    updated_at: '2026-08-09T05:00:00Z',
    ...overrides,
  };
}

describe('云端模型设置', () => {
  it('回填公开字段但不回显密钥，留空保存会保留已保存密钥', async () => {
    const user = userEvent.setup();
    const runtimeChanged = vi.fn(async () => undefined);
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (!init?.method) return json(configured());
      if (url.endsWith('/settings/cloud') && init.method === 'POST') {
        return json(configured({ model: 'example-planner-v2', revision: 4 }));
      }
      if (url.endsWith('/settings/cloud/test') && init.method === 'POST') {
        return json({ ok: true, status: 'ready', detail: '连接正常。', latency_ms: 42 });
      }
      throw new Error(`Unexpected request: ${init?.method || 'GET'} ${url}`);
    }));

    render(<CloudModelSettings onRuntimeChanged={runtimeChanged} />);

    expect(await screen.findByDisplayValue('https://cloud.example.test/v1')).toBeInTheDocument();
    const keyInput = screen.getByLabelText('云端模型 API key') as HTMLInputElement;
    expect(keyInput).toHaveAttribute('type', 'password');
    expect(keyInput).toHaveValue('');
    expect(keyInput).toHaveAttribute('placeholder', '已保存；留空保持不变');

    const modelInput = screen.getByLabelText('云端模型名称');
    await user.clear(modelInput);
    await user.type(modelInput, 'example-planner-v2');
    await user.click(screen.getByRole('button', { name: '保存配置' }));

    await screen.findByText(/配置已保存并立即生效/);
    const save = calls.find((call) => call.url.endsWith('/settings/cloud') && call.init?.method === 'POST');
    const payload = JSON.parse(String(save?.init?.body));
    expect(payload).toEqual({
      endpoint: 'https://cloud.example.test/v1',
      model: 'example-planner-v2',
      expected_revision: 3,
    });
    expect(payload).not.toHaveProperty('api_key');
    expect(new Headers(save?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
    expect(keyInput).toHaveValue('');
    expect(runtimeChanged).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: '测试连接' }));
    expect(await screen.findByText('连接正常（42 ms）')).toBeInTheDocument();
    const test = calls.find((call) => call.url.endsWith('/settings/cloud/test'));
    expect(new Headers(test?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
    expect(runtimeChanged).toHaveBeenCalledTimes(2);
  });

  it('首次保存发送新密钥一次，成功后立即清空输入并不显示密钥', async () => {
    const user = userEvent.setup();
    const fakeSecret = 'test-secret-never-real-123';
    const empty = configured({
      endpoint: null,
      model: null,
      has_api_key: false,
      configured: false,
      credential_source: 'none',
      status: 'not_configured',
      detail: '尚未配置云端模型。',
      revision: 0,
      updated_at: null,
    });
    let savedBody: Record<string, unknown> | null = null;
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (!init?.method) return json(empty);
      savedBody = JSON.parse(String(init.body));
      return json(configured({ revision: 1 }));
    }));

    render(<CloudModelSettings onRuntimeChanged={() => undefined} />);
    await screen.findByText('未配置');
    await user.type(screen.getByLabelText('云端模型服务地址'), 'https://cloud.example.test/v1');
    await user.type(screen.getByLabelText('云端模型名称'), 'example-planner');
    await user.type(screen.getByLabelText('云端模型 API key'), fakeSecret);
    await user.click(screen.getByRole('button', { name: '保存配置' }));

    await screen.findByText(/配置已保存并立即生效/);
    expect(savedBody).toMatchObject({ api_key: fakeSecret, expected_revision: 0 });
    expect(screen.getByLabelText('云端模型 API key')).toHaveValue('');
    expect(document.body).not.toHaveTextContent(fakeSecret);
  });

  it('未保存改动会禁止连接测试；测试失败留在局部，清除需要二次确认', async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (!init?.method) return json(configured());
      if (url.endsWith('/settings/cloud/test')) {
        return json({ ok: false, status: 'error', detail: '连接失败，请检查地址、模型和密钥。', latency_ms: null });
      }
      if (url.endsWith('/settings/cloud/clear')) {
        return json(configured({ endpoint: null, model: null, has_api_key: false, configured: false, credential_source: 'none', status: 'not_configured', detail: '尚未配置云端模型。', revision: 4 }));
      }
      throw new Error(`Unexpected request: ${init?.method || 'GET'} ${url}`);
    }));

    render(<CloudModelSettings onRuntimeChanged={() => undefined} />);
    const endpoint = await screen.findByLabelText('云端模型服务地址');
    await user.type(endpoint, '/draft');
    expect(screen.getByRole('button', { name: '测试连接' })).toBeDisabled();
    await user.clear(endpoint);
    await user.type(endpoint, 'https://cloud.example.test/v1');

    await user.click(screen.getByRole('button', { name: '测试连接' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('连接失败，请检查地址、模型和密钥。');

    await user.click(screen.getByRole('button', { name: '清除配置' }));
    expect(screen.getByText(/再次点击“确认清除”/)).toBeInTheDocument();
    expect(calls.filter((call) => call.url.endsWith('/settings/cloud/clear'))).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '确认清除' }));
    await waitFor(() => expect(calls.filter((call) => call.url.endsWith('/settings/cloud/clear'))).toHaveLength(1));
    const clear = calls.find((call) => call.url.endsWith('/settings/cloud/clear'));
    expect(JSON.parse(String(clear?.init?.body))).toEqual({ expected_revision: 3 });
    expect(new Headers(clear?.init?.headers).get('X-AI-Game-Client')).toBe('console-v1');
    expect(screen.getByLabelText('云端模型服务地址')).toHaveValue('');
    expect(screen.getByRole('button', { name: '测试连接' })).toBeDisabled();
  });
});
