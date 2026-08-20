// ---------------------------------------------------------------------------
// Gateway Task Workspace (Phase 6, contract §6/§7/§9/§10/§11).
//
// The Console is a Gateway *client*: it renders the frozen Task Snapshot,
// sends §7 control commands and §9 task messages, and follows the §10/§11
// event stream. It never invents state — status, stages and facts all come
// from the Gateway; the only local state is the conversation id (the §6
// Snapshot intentionally omits it) and transient form/selection state.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  AlertCircle,
  CirclePause,
  History,
  ListPlus,
  Pause,
  Play,
  RefreshCw,
  Send,
  Square,
  UserRound,
  WifiOff,
} from 'lucide-react';
import { ApiError, api, newIdempotencyKey } from '../api';
import { formatDateTime } from '../format';
import { gatewayTaskStatusMeta } from '../status';
import type {
  GatewayControlCommand,
  GatewayDeviceAvailability,
  GatewayDeviceItem,
  GatewayTaskSnapshot,
} from '../types';
import { GATEWAY_TERMINAL_TASK_STATUSES } from '../types';
import { isUserTakeover } from '../gateway/projection';
import { useGatewayTaskStream } from '../gateway/useGatewayTaskStream';
import { EmptyState } from './EmptyState';
import { StatusBadge } from './StatusBadge';

const TASK_POLL_INTERVAL_MS = 3000;
const PROGRESS_DISPLAY_CAP = 50;
const EVENTS_DISPLAY_CAP = 100;

const DEVICE_AVAILABILITY_LABEL: Record<GatewayDeviceAvailability, string> = {
  available: '可用',
  in_use: '使用中',
  unavailable: '不可用',
};

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : '请求未完成，请稍后重试。';
}

function isTerminalStatus(status: string): boolean {
  return (GATEWAY_TERMINAL_TASK_STATUSES as readonly string[]).includes(status);
}

export function GatewayTaskWorkspace() {
  // -- task list -----------------------------------------------------------
  const [tasks, setTasks] = useState<GatewayTaskSnapshot[]>([]);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [loadingTasks, setLoadingTasks] = useState(true);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const tasksInFlight = useRef(false);

  // -- devices ---------------------------------------------------------------
  const [devices, setDevices] = useState<GatewayDeviceItem[]>([]);
  const [devicesError, setDevicesError] = useState<string | null>(null);

  // -- create form -----------------------------------------------------------
  const [goal, setGoal] = useState('');
  const [deviceId, setDeviceId] = useState('');
  const [conversationId, setConversationId] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // -- selected task detail ---------------------------------------------------
  const stream = useGatewayTaskStream(selectedTaskId);
  const [conversationMap, setConversationMap] = useState<Record<string, string>>({});
  const [messageText, setMessageText] = useState('');
  const [messageError, setMessageError] = useState<string | null>(null);
  const [sendingMessage, setSendingMessage] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [controlling, setControlling] = useState(false);

  const loadTasks = useCallback(async (silent: boolean) => {
    if (tasksInFlight.current) {
      return;
    }
    tasksInFlight.current = true;
    if (!silent) {
      setLoadingTasks(true);
    }
    try {
      const response = await api.listGatewayTasks();
      setTasks(response.items);
      setTasksError(null);
    } catch (error) {
      if (!silent) {
        setTasksError(messageFrom(error));
      }
    } finally {
      tasksInFlight.current = false;
      if (!silent) {
        setLoadingTasks(false);
      }
    }
  }, []);

  const loadDevices = useCallback(async () => {
    try {
      const response = await api.getGatewayDevices();
      setDevices(response.items);
      setDevicesError(null);
      setDeviceId((current) =>
        current && response.items.some((device) => device.id === current)
          ? current
          : response.items.find((device) => device.availability === 'available')?.id ?? '',
      );
    } catch (error) {
      setDevicesError(messageFrom(error));
    }
  }, []);

  useEffect(() => {
    void loadTasks(false);
    void loadDevices();
    const timer = window.setInterval(() => {
      if (typeof document === 'undefined' || document.visibilityState !== 'visible') {
        return;
      }
      void loadTasks(true);
    }, TASK_POLL_INTERVAL_MS);
    const onVisible = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
        void loadTasks(true);
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [loadTasks, loadDevices]);

  // -- create --------------------------------------------------------------
  const submitCreate = useCallback(async () => {
    const cleanGoal = goal.trim();
    const cleanConversation = conversationId.trim();
    if (!cleanGoal || !deviceId || !cleanConversation || creating) {
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const response = await api.createGatewayTask({
        goal: cleanGoal,
        device_id: deviceId,
        conversation_id: cleanConversation,
        message_id: newIdempotencyKey(),
      });
      setConversationMap((prev) => ({ ...prev, [response.task.id]: cleanConversation }));
      setSelectedTaskId(response.task.id);
      setGoal('');
      await loadTasks(true);
      await loadDevices();
    } catch (error) {
      setCreateError(messageFrom(error));
    } finally {
      setCreating(false);
    }
  }, [goal, deviceId, conversationId, creating, loadTasks, loadDevices]);

  // -- controls ------------------------------------------------------------
  const submitControl = useCallback(
    async (command: GatewayControlCommand) => {
      if (!selectedTaskId || controlling) {
        return;
      }
      setControlling(true);
      setControlError(null);
      try {
        await stream.sendControl(command);
      } catch (error) {
        setControlError(messageFrom(error));
      } finally {
        setControlling(false);
      }
    },
    [selectedTaskId, controlling, stream.sendControl],
  );

  // -- task message -----------------------------------------------------------
  const selectedConversation = selectedTaskId ? conversationMap[selectedTaskId] ?? '' : '';
  const submitMessage = useCallback(async () => {
    const text = messageText.trim();
    const conversation = selectedConversation.trim();
    if (!selectedTaskId || !text || !conversation || sendingMessage) {
      return;
    }
    setSendingMessage(true);
    setMessageError(null);
    try {
      await stream.sendTaskMessage(conversation, text);
      setMessageText('');
    } catch (error) {
      setMessageError(messageFrom(error));
    } finally {
      setSendingMessage(false);
    }
  }, [messageText, selectedConversation, selectedTaskId, sendingMessage, stream.sendTaskMessage]);

  // -- derived ---------------------------------------------------------------
  const snapshot = stream.snapshot;
  const status = snapshot?.status;
  const terminal = status !== undefined && isTerminalStatus(status);
  const takeover = isUserTakeover(stream.progress);
  const statusMeta = status ? gatewayTaskStatusMeta(status, { userTakeover: takeover }) : null;
  const canPause = status === 'RUNNING';
  const canResume = status === 'PAUSED';
  const canTakeover = status === 'RUNNING';
  const canCancel = status !== undefined && !terminal;

  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? null;
  const progressEntries = stream.progress.slice(0, PROGRESS_DISPLAY_CAP).reverse();
  const rawEvents = stream.events.slice(0, EVENTS_DISPLAY_CAP).reverse();

  return (
    <section className="agent-workspace gateway-workspace" aria-label="网关任务">
      <div className="gateway-layout">
        {/* ------------------------------------------------ task list ------ */}
        <aside className="gateway-task-list panel" aria-label="任务列表">
          <div className="panel-header">
            <div>
              <span className="eyebrow">任务</span>
              <h2>Gateway 任务</h2>
            </div>
            <button
              className="icon-button"
              aria-label="刷新任务列表"
              title="刷新任务列表"
              onClick={() => void loadTasks(false)}
            >
              <RefreshCw size={16} />
            </button>
          </div>
          {tasksError && (
            <div className="agent-inline-error" role="alert">
              <AlertCircle size={16} />
              <span>{tasksError}</span>
            </div>
          )}
          {loadingTasks ? (
            <p className="gateway-list-hint">正在加载任务…</p>
          ) : tasks.length === 0 ? (
            <EmptyState
              icon={History}
              title="还没有任务"
              description="在下方创建第一个 Gateway 任务。"
              compact
            />
          ) : (
            <ul className="gateway-task-items">
              {tasks.map((task) => (
                <li key={task.id}>
                  <button
                    className={`gateway-task-item ${task.id === selectedTaskId ? 'gateway-task-item-active' : ''}`}
                    onClick={() => setSelectedTaskId(task.id)}
                    aria-current={task.id === selectedTaskId ? 'true' : undefined}
                  >
                    <span className="gateway-task-item-goal">{task.goal}</span>
                    <span className="gateway-task-item-meta">
                      <StatusBadge meta={gatewayTaskStatusMeta(task.status)} compact />
                      <span className="gateway-task-item-time">{formatDateTime(task.updated_at)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        {/* ------------------------------------------------ main column ----- */}
        <div className="gateway-main">
          {/* create form */}
          <section className="agent-composer panel" aria-label="创建任务">
            <div className="agent-composer-heading">
              <span className="agent-composer-icon" aria-hidden="true">
                <ListPlus size={23} />
              </span>
              <div>
                <span className="eyebrow">新建任务</span>
                <h2>通过 Gateway 创建一个任务</h2>
                <p>选择设备、填写会话 ID 与目标；创建后任务立即进入 Runtime。</p>
              </div>
            </div>
            <div className="gateway-create-form">
              <label className="gateway-field">
                <span className="gateway-field-label">设备</span>
                <select
                  value={deviceId}
                  onChange={(event) => setDeviceId(event.target.value)}
                  aria-label="设备"
                >
                  <option value="" disabled>
                    {devices.length === 0 ? (devicesError ? '设备列表不可用' : '请选择设备') : '请选择设备'}
                  </option>
                  {devices.map((device) => (
                    <option key={device.id} value={device.id} disabled={device.availability !== 'available'}>
                      {device.id} · {DEVICE_AVAILABILITY_LABEL[device.availability] ?? device.availability}
                    </option>
                  ))}
                </select>
              </label>
              <label className="gateway-field">
                <span className="gateway-field-label">会话 ID</span>
                <input
                  type="text"
                  value={conversationId}
                  onChange={(event) => setConversationId(event.target.value)}
                  placeholder="如 wechat:wxid_xxx 或 hermes:main"
                  aria-label="会话 ID"
                />
              </label>
              <label className="gateway-field gateway-field-goal">
                <span className="gateway-field-label">目标</span>
                <input
                  type="text"
                  value={goal}
                  onChange={(event) => setGoal(event.target.value)}
                  placeholder="用一句话描述要完成的最终结果"
                  aria-label="任务目标"
                />
              </label>
              <button
                className="button button-primary"
                onClick={() => void submitCreate()}
                disabled={!goal.trim() || !deviceId || !conversationId.trim() || creating}
              >
                {creating ? '正在创建…' : '创建任务'}
              </button>
            </div>
            {createError && (
              <div className="agent-inline-error" role="alert">
                <AlertCircle size={16} />
                <span>{createError}</span>
              </div>
            )}
          </section>

          {/* selected task */}
          {selectedTaskId && (
            <section className="gateway-detail" aria-label="任务详情">
              {!snapshot ? (
                <div className="panel gateway-detail-loading">
                  {stream.streamError ? (
                    <div className="agent-inline-error" role="alert">
                      <AlertCircle size={16} />
                      <span>{stream.streamError}</span>
                      <button
                        className="button button-secondary button-small"
                        onClick={() => void stream.refresh()}
                        disabled={stream.refreshing}
                      >
                        <RefreshCw size={14} /> {stream.refreshing ? '正在刷新…' : '刷新'}
                      </button>
                    </div>
                  ) : (
                    <p className="gateway-list-hint">正在加载任务快照…</p>
                  )}
                </div>
              ) : (
                <>
                  {/* hero */}
                  <section className="panel gateway-hero" aria-label="任务状态">
                    <div className="gateway-hero-main">
                      <div className="gateway-hero-top">
                        {statusMeta && <StatusBadge meta={statusMeta} />}
                        <span className="eyebrow">事件序号 {stream.cursor}</span>
                      </div>
                      <h2 className="gateway-hero-goal">{snapshot.goal}</h2>
                      <div className="gateway-hero-facts">
                        <span>
                          <Activity size={14} aria-hidden="true" /> {snapshot.device_id}
                        </span>
                        <span>更新于 {formatDateTime(snapshot.updated_at)}</span>
                      </div>
                    </div>
                    {canPause || canResume || canTakeover || canCancel ? (
                      <div className="gateway-controls" role="group" aria-label="任务控制">
                        {canPause && (
                          <button
                            className="button button-secondary button-small"
                            onClick={() => void submitControl('pause')}
                            disabled={controlling}
                          >
                            <Pause size={14} /> 暂停
                          </button>
                        )}
                        {canTakeover && (
                          <button
                            className="button button-secondary button-small"
                            onClick={() => void submitControl('takeover')}
                            disabled={controlling}
                          >
                            <UserRound size={14} /> 用户接管
                          </button>
                        )}
                        {canResume && (
                          <button
                            className="button button-primary button-small"
                            onClick={() => void submitControl('resume')}
                            disabled={controlling}
                          >
                            <Play size={14} /> 恢复
                          </button>
                        )}
                        {canCancel && (
                          <button
                            className="button button-ghost-danger button-small"
                            onClick={() => void submitControl('cancel')}
                            disabled={controlling}
                          >
                            <Square size={14} /> 取消任务
                          </button>
                        )}
                      </div>
                    ) : (
                      <span className="gateway-terminal-note">
                        <CirclePause size={14} aria-hidden="true" /> 任务已结束，无需控制
                      </span>
                    )}
                    {controlError && (
                      <div className="agent-inline-error" role="alert">
                        <AlertCircle size={16} />
                        <span>{controlError}</span>
                      </div>
                    )}
                  </section>

                  {/* takeover banner */}
                  {takeover && status === 'PAUSED' && (
                    <div className="gateway-takeover-banner" role="status">
                      <UserRound size={18} aria-hidden="true" />
                      <div>
                        <strong>用户接管中</strong>
                        <span>你正在直接操作设备，任务保持暂停；恢复后任务将继续执行。</span>
                      </div>
                    </div>
                  )}

                  {/* stream health */}
                  {(stream.reconnecting || stream.streamError || !stream.streamConnected) && (
                    <div
                      className={`gateway-stream-status ${stream.streamConnected ? 'gateway-stream-ok' : 'gateway-stream-bad'}`}
                      role="status"
                    >
                      <WifiOff size={14} aria-hidden="true" />
                      <span>
                        {stream.reconnecting
                          ? '事件流已断开，正在重连…'
                          : stream.streamError
                            ? stream.streamError
                            : '事件流未连接'}
                      </span>
                      <button
                        className="button button-ghost button-small"
                        onClick={() => void stream.refresh()}
                        disabled={stream.refreshing}
                      >
                        <RefreshCw size={13} /> {stream.refreshing ? '正在刷新…' : '刷新'}
                      </button>
                    </div>
                  )}

                  {/* progress + message */}
                  <div className="gateway-columns">
                    <section className="panel gateway-progress" aria-label="任务进度">
                      <div className="panel-header">
                        <div>
                          <span className="eyebrow">进度</span>
                          <h2>任务进展</h2>
                        </div>
                      </div>
                      {progressEntries.length === 0 ? (
                        <p className="gateway-list-hint">暂无事件；任务开始后这里会实时显示进展。</p>
                      ) : (
                        <ol className="gateway-progress-list">
                          {progressEntries.map((entry) => (
                            <li key={entry.sequence} className="gateway-progress-item">
                              <span className="gateway-progress-seq">#{entry.sequence}</span>
                              <div className="gateway-progress-copy">
                                <p>{entry.text}</p>
                                {entry.createdAt && (
                                  <span>{formatDateTime(entry.createdAt)}</span>
                                )}
                              </div>
                            </li>
                          ))}
                        </ol>
                      )}
                      {rawEvents.length > 0 && (
                        <details className="gateway-raw-events">
                          <summary>
                            <History size={14} aria-hidden="true" /> 原始事件（{stream.events.length}）
                          </summary>
                          <ul>
                            {rawEvents.map((event) => (
                              <li key={event.id}>
                                <span>#{event.sequence}</span>
                                <strong>{event.type}</strong>
                                <span>{event.actor}</span>
                                <span>{formatDateTime(event.created_at)}</span>
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </section>

                    <section className="panel gateway-message" aria-label="给任务发消息">
                      <div className="panel-header">
                        <div>
                          <span className="eyebrow">中途补充</span>
                          <h2>给任务发消息</h2>
                        </div>
                      </div>
                      {terminal ? (
                        <p className="gateway-list-hint">任务已结束，无法继续发送消息。</p>
                      ) : (
                        <>
                          <label className="gateway-field">
                            <span className="gateway-field-label">会话 ID</span>
                            <input
                              type="text"
                              value={selectedConversation}
                              onChange={(event) => {
                                const value = event.target.value;
                                if (selectedTaskId) {
                                  setConversationMap((prev) => ({ ...prev, [selectedTaskId]: value }));
                                }
                              }}
                              placeholder="任务所属的会话 ID"
                              aria-label="任务会话 ID"
                            />
                          </label>
                          <label className="gateway-field">
                            <span className="gateway-field-label">消息</span>
                            <textarea
                              value={messageText}
                              onChange={(event) => setMessageText(event.target.value)}
                              placeholder="补充要求，例如「先不要点确认按钮」"
                              rows={3}
                              aria-label="任务消息"
                            />
                          </label>
                          <button
                            className="button button-primary button-small"
                            onClick={() => void submitMessage()}
                            disabled={!messageText.trim() || !selectedConversation.trim() || sendingMessage}
                          >
                            <Send size={14} /> {sendingMessage ? '正在发送…' : '发送'}
                          </button>
                          {messageError && (
                            <div className="agent-inline-error" role="alert">
                              <AlertCircle size={16} />
                              <span>{messageError}</span>
                            </div>
                          )}
                        </>
                      )}
                    </section>
                  </div>
                </>
              )}
            </section>
          )}

          {selectedTaskId === null && (
            <EmptyState
              icon={Activity}
              title="选择一个任务"
              description={tasks.length > 0 ? '从左侧任务列表选择一个任务查看详情。' : '创建任务后会在这里显示实时进展。'}
            />
          )}
        </div>
      </div>
    </section>
  );
}
