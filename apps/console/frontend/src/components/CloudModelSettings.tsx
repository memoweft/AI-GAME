import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Cloud,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  Trash2,
  Wifi,
} from 'lucide-react';
import { ApiError, api } from '../api';
import type { CloudChatConfig } from '../types';

interface CloudModelSettingsProps {
  onRuntimeChanged: () => Promise<void> | void;
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : '云端模型配置操作失败。';
}

export function CloudModelSettings({ onRuntimeChanged }: CloudModelSettingsProps) {
  const [config, setConfig] = useState<CloudChatConfig | null>(null);
  const [endpoint, setEndpoint] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<'save' | 'test' | 'clear' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const applyConfig = useCallback((next: CloudChatConfig) => {
    setConfig(next);
    setEndpoint(next.endpoint ?? '');
    setModel(next.model ?? '');
    setApiKey('');
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      applyConfig(await api.getCloudChatConfig());
    } catch (nextError) {
      setError(message(nextError));
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => {
    if (!config) return Boolean(endpoint || model || apiKey);
    return endpoint.trim() !== (config.endpoint ?? '')
      || model.trim() !== (config.model ?? '')
      || Boolean(apiKey);
  }, [apiKey, config, endpoint, model]);

  const canSave = Boolean(
    config
    && endpoint.trim()
    && model.trim()
    && (config.has_api_key || apiKey.trim())
    && dirty
    && !action,
  );
  const canTest = Boolean(config?.configured && !dirty && !action);

  const save = async () => {
    if (!config || !canSave) return;
    setAction('save');
    setError(null);
    setNotice(null);
    setConfirmClear(false);
    try {
      const next = await api.saveCloudChatConfig({
        endpoint: endpoint.trim(),
        model: model.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        expected_revision: config.revision,
      });
      applyConfig(next);
      setNotice('配置已保存并立即生效；你现在可以创建云端执行对话。');
      await onRuntimeChanged();
    } catch (nextError) {
      setError(message(nextError));
    } finally {
      setAction(null);
    }
  };

  const testConnection = async () => {
    if (!config || !canTest) return;
    setAction('test');
    setError(null);
    setNotice(null);
    setConfirmClear(false);
    try {
      const result = await api.testCloudChatConfig();
      setConfig((current) => current ? {
        ...current,
        status: result.status,
        detail: result.detail,
      } : current);
      if (result.ok) {
        setNotice(result.latency_ms == null
          ? result.detail
          : `${result.detail.replace(/[。.!！]+$/, '')}（${result.latency_ms} ms）`);
      } else {
        setError(result.detail);
      }
      await onRuntimeChanged();
    } catch (nextError) {
      setError(message(nextError));
    } finally {
      setAction(null);
    }
  };

  const clear = async () => {
    if (!config || action) return;
    if (!confirmClear) {
      setConfirmClear(true);
      setNotice('再次点击“确认清除”才会删除本机保存的云端配置。');
      return;
    }
    setAction('clear');
    setError(null);
    setNotice(null);
    try {
      applyConfig(await api.clearCloudChatConfig(config.revision));
      setConfirmClear(false);
      setNotice('云端配置已清除；本地直聊仍可继续使用。');
      await onRuntimeChanged();
    } catch (nextError) {
      setError(message(nextError));
    } finally {
      setAction(null);
    }
  };

  if (loading) {
    return (
      <section className="panel settings-panel cloud-settings-panel" aria-label="正在读取云端模型配置">
        <div className="cloud-settings-loading"><LoaderCircle className="spin" size={20} /><span>正在读取本机保存的云端配置…</span></div>
      </section>
    );
  }

  return (
    <section className="panel settings-panel cloud-settings-panel">
      <div className="cloud-settings-heading">
        <div className="panel-title-copy">
          <span className="eyebrow">云端对话</span>
          <h2>云端模型配置</h2>
          <p>保存后立即用于新对话；设备截图仍只交给本地 GUI-Owl。</p>
        </div>
        {config && (
          <span className={`cloud-config-state cloud-config-${config.status}`}>
            {config.status === 'ready' ? <CheckCircle2 size={15} /> : config.status === 'error' ? <AlertTriangle size={15} /> : <Cloud size={15} />}
            {config.status === 'ready' ? '连接正常' : config.status === 'error' ? '连接失败' : config.configured ? '已保存' : '未配置'}
          </span>
        )}
      </div>

      {error && (
        <div className="cloud-settings-message cloud-settings-error" role="alert">
          <AlertTriangle size={16} /><span>{error}</span>
          {!config && <button type="button" className="text-button" onClick={() => void load()}><RefreshCw size={14} />重试</button>}
        </div>
      )}

      {config && (
        <form className="cloud-settings-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label>
            <span>服务地址</span>
            <input
              type="url"
              aria-label="云端模型服务地址"
              value={endpoint}
              onChange={(event) => { setEndpoint(event.target.value); setConfirmClear(false); }}
              placeholder="https://example.com/v1"
              autoComplete="off"
              spellCheck={false}
              maxLength={2048}
            />
          </label>
          <label>
            <span>模型名称</span>
            <input
              aria-label="云端模型名称"
              value={model}
              onChange={(event) => { setModel(event.target.value); setConfirmClear(false); }}
              placeholder="例如 gpt-5 或其他兼容模型"
              autoComplete="off"
              spellCheck={false}
              maxLength={256}
            />
          </label>
          <label>
            <span>API key</span>
            <div className="cloud-secret-input">
              <KeyRound size={16} />
              <input
                type="password"
                aria-label="云端模型 API key"
                value={apiKey}
                onChange={(event) => { setApiKey(event.target.value); setConfirmClear(false); }}
                placeholder={config.has_api_key ? '已保存；留空保持不变' : '请输入 API key'}
                autoComplete="new-password"
                spellCheck={false}
                maxLength={8192}
              />
            </div>
            <small>{config.has_api_key ? 'API key 已保存到当前 Windows 用户的受保护存储；页面不会回显。' : '首次配置需要填写；密钥不会保存到浏览器。'}</small>
          </label>

          <div className="cloud-settings-detail">
            <Wifi size={15} />
            <span>{dirty ? '有尚未保存的更改。' : config.detail}</span>
          </div>
          {notice && <div className="cloud-settings-message cloud-settings-success" role="status"><CheckCircle2 size={16} /><span>{notice}</span></div>}

          <div className="cloud-settings-actions">
            <button type="submit" className="button button-primary" disabled={!canSave}>
              {action === 'save' ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
              保存配置
            </button>
            <button type="button" className="button button-secondary" disabled={!canTest} onClick={() => void testConnection()}>
              {action === 'test' ? <LoaderCircle className="spin" size={16} /> : <Wifi size={16} />}
              测试连接
            </button>
            <button type="button" className={`button ${confirmClear ? 'button-danger' : 'button-ghost'}`} disabled={!config.configured || Boolean(action)} onClick={() => void clear()}>
              {action === 'clear' ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}
              {confirmClear ? '确认清除' : '清除配置'}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
