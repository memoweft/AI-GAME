// ---------------------------------------------------------------------------
// useGatewayTaskStream (Phase 6, contract §11).
//
// Lifecycle per selected task:
//   1. GET the §6 Snapshot (status badge, current stage, control buttons);
//   2. backfill the full event history page by page from sequence 0 via the
//      §10 paginated endpoint (drives the progress timeline + technical view);
//   3. open the §11 SSE stream at the calibrated cursor;
//   4. on drop: re-GET the Snapshot, calibrate cursor = max(local, snapshot),
//      backfill the gap, and reconnect with a fixed delay, capped retries;
//      the visible state says "流已断开 / 重连中" the whole time;
//   5. on a terminal status the stream is not opened.
//
// The Snapshot is the calibration source of truth; the event stream never
// invents state the Runtime did not record (§13).
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, newIdempotencyKey } from '../api';
import type {
  GatewayControlCommand,
  GatewayControlResponse,
  GatewayRuntimeEvent,
  GatewayTaskMessageResponse,
  GatewayTaskSnapshot,
} from '../types';
import { GATEWAY_TERMINAL_TASK_STATUSES } from '../types';
import { calibrateCursor, GatewayEventStream } from './eventStream';
import { projectProgressEvent } from './projection';
import type { GatewayProgressEntry } from './projection';

const BACKFILL_PAGE_LIMIT = 200;
const MAX_RECONNECT_ATTEMPTS = 8;
const RECONNECT_DELAY_MS = 2000;
const PROGRESS_CAP = 200;
const EVENTS_CAP = 200;

export interface GatewayTaskStream {
  snapshot: GatewayTaskSnapshot | null;
  progress: GatewayProgressEntry[];
  events: GatewayRuntimeEvent[];
  cursor: number;
  streamConnected: boolean;
  reconnecting: boolean;
  streamError: string | null;
  refreshing: boolean;
  refresh: () => Promise<void>;
  sendControl: (
    command: GatewayControlCommand,
    reason?: string,
  ) => Promise<GatewayControlResponse>;
  sendTaskMessage: (
    conversationId: string,
    text: string,
  ) => Promise<GatewayTaskMessageResponse>;
}

type SnapshotLoad = GatewayTaskSnapshot | 'fatal' | null;

function isTerminal(status: string | undefined): boolean {
  return status !== undefined && (GATEWAY_TERMINAL_TASK_STATUSES as readonly string[]).includes(status);
}

export function useGatewayTaskStream(taskId: string | null): GatewayTaskStream {
  const [snapshot, setSnapshot] = useState<GatewayTaskSnapshot | null>(null);
  const [progress, setProgress] = useState<GatewayProgressEntry[]>([]);
  const [events, setEvents] = useState<GatewayRuntimeEvent[]>([]);
  const [cursor, setCursor] = useState(0);
  const [streamConnected, setStreamConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const controllerRef = useRef<{ refresh: () => Promise<void> } | null>(null);

  useEffect(() => {
    // Reset every bit of state when the selected task changes.
    setSnapshot(null);
    setProgress([]);
    setEvents([]);
    setCursor(0);
    setStreamConnected(false);
    setReconnecting(false);
    setStreamError(null);
    setRefreshing(false);

    let active = true;
    let reconnectTimer: number | null = null;
    let cursorValue = 0;
    let lastBackfilled = 0;
    let reconnectAttempts = 0;
    let snapshotValue: GatewayTaskSnapshot | null = null;
    const progressValue: GatewayProgressEntry[] = [];
    const progressSequences = new Set<number>();
    const eventsValue: GatewayRuntimeEvent[] = [];
    const knownEventIds = new Set<string>();

    const appendProgress = (entries: GatewayProgressEntry[]) => {
      let changed = false;
      for (const entry of entries) {
        if (progressSequences.has(entry.sequence)) {
          continue;
        }
        progressSequences.add(entry.sequence);
        progressValue.push(entry);
        changed = true;
      }
      if (!changed) {
        return;
      }
      progressValue.sort((a, b) => a.sequence - b.sequence);
      while (progressValue.length > PROGRESS_CAP) {
        const dropped = progressValue.shift();
        if (dropped) {
          progressSequences.delete(dropped.sequence);
        }
      }
      if (active) {
        setProgress([...progressValue]);
      }
    };

    const appendEvents = (items: GatewayRuntimeEvent[]) => {
      let changed = false;
      for (const item of items) {
        if (knownEventIds.has(item.id)) {
          continue;
        }
        knownEventIds.add(item.id);
        eventsValue.push(item);
        changed = true;
      }
      if (!changed) {
        return;
      }
      eventsValue.sort((a, b) => a.sequence - b.sequence);
      while (eventsValue.length > EVENTS_CAP) {
        const dropped = eventsValue.shift();
        if (dropped) {
          knownEventIds.delete(dropped.id);
        }
      }
      if (active) {
        setEvents([...eventsValue]);
      }
    };

    /** @returns snapshot, 'fatal' (task gone / client error) or null (transient). */
    const loadSnapshot = async (): Promise<SnapshotLoad> => {
      if (!taskId) {
        return null;
      }
      try {
        const response = await api.getGatewayTask(taskId);
        if (!active) {
          return response.task;
        }
        snapshotValue = response.task;
        cursorValue = calibrateCursor(cursorValue, response.task.last_event_sequence);
        setSnapshot(response.task);
        setCursor(cursorValue);
        setStreamError(null);
        return response.task;
      } catch (error) {
        if (!active) {
          return null;
        }
        if (error instanceof ApiError) {
          if (error.status === 404) {
            snapshotValue = null;
            setSnapshot(null);
            setStreamError('任务不存在或已被删除。');
            return 'fatal';
          }
          setStreamError(error.message);
          return error.status >= 400 && error.status < 500 ? 'fatal' : null;
        }
        setStreamError('无法获取任务快照。');
        return null;
      }
    };

    const backfill = async (from: number, to: number) => {
      if (!taskId) {
        return;
      }
      let after = from;
      while (after < to) {
        const page = await api.getGatewayTaskEvents(taskId, after, BACKFILL_PAGE_LIMIT);
        if (!active) {
          return;
        }
        appendEvents(page.items);
        appendProgress(
          page.items.map((item) =>
            projectProgressEvent({
              sequence: item.sequence,
              type: item.type,
              payload: item.payload,
              createdAt: item.created_at,
            }),
          ),
        );
        if (page.items.length === 0) {
          break;
        }
        after = page.next_after_sequence;
      }
      lastBackfilled = to;
    };

    const openStream = () => {
      if (!taskId || !active) {
        return;
      }
      if (typeof EventSource === 'undefined') {
        setStreamError('当前浏览器不支持 EventSource，无法接收实时事件流。');
        return;
      }
      if (isTerminal(snapshotValue?.status)) {
        setStreamConnected(false);
        return;
      }
      stream.connect(cursorValue);
    };

    const scheduleReconnect = () => {
      if (!active || !taskId) {
        return;
      }
      if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        setReconnecting(false);
        setStreamError('事件流断开且多次重连失败，请点击刷新手动恢复。');
        return;
      }
      reconnectAttempts += 1;
      setReconnecting(true);
      reconnectTimer = window.setTimeout(() => {
        void (async () => {
          const result = await loadSnapshot();
          if (!active || !taskId) {
            return;
          }
          if (result === 'fatal') {
            setReconnecting(false);
            return;
          }
          try {
            if (result !== null) {
              await backfill(lastBackfilled, cursorValue);
            }
            if (!active) {
              return;
            }
            reconnectAttempts = 0;
            setReconnecting(false);
            setStreamError(result === null ? '事件流已恢复，但任务快照仍不可用。' : null);
            openStream();
          } catch {
            // Transient failure during the gap backfill: retry the whole cycle.
            scheduleReconnect();
          }
        })();
      }, RECONNECT_DELAY_MS);
    };

    const stream = new GatewayEventStream({
      url: (afterSequence) => api.gatewayEventStreamUrl(taskId ?? '', afterSequence),
      createSource: (url) => new EventSource(url),
      onOpen: () => {
        if (!active) {
          return;
        }
        reconnectAttempts = 0;
        setStreamConnected(true);
        setReconnecting(false);
      },
      onEvent: (data) => {
        if (!active) {
          return;
        }
        // The stream class only forwards sequences greater than the cursor.
        cursorValue = calibrateCursor(cursorValue, data.sequence);
        setCursor(cursorValue);
        appendProgress([
          projectProgressEvent({
            sequence: data.sequence,
            type: data.type,
            payload: data.payload,
          }),
        ]);
      },
      onDrop: () => {
        if (!active) {
          return;
        }
        setStreamConnected(false);
        scheduleReconnect();
      },
    });

    const bootstrap = async () => {
      if (!taskId) {
        return;
      }
      const result = await loadSnapshot();
      if (!active) {
        return;
      }
      if (result === 'fatal') {
        return;
      }
      if (result !== null) {
        try {
          await backfill(0, cursorValue);
        } catch {
          // Transient backfill failure: the stream still delivers live events.
        }
        if (!active) {
          return;
        }
      }
      openStream();
    };

    controllerRef.current = {
      refresh: async () => {
        if (!taskId) {
          return;
        }
        setRefreshing(true);
        try {
          const result = await loadSnapshot();
          if (!active || result === 'fatal') {
            return;
          }
          if (result !== null) {
            try {
              await backfill(lastBackfilled, cursorValue);
            } catch {
              // Keep the previous history; loadSnapshot already surfaced the error.
            }
          }
          openStream();
        } finally {
          if (active) {
            setRefreshing(false);
          }
        }
      },
    };

    void bootstrap();

    return () => {
      active = false;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      stream.teardown();
      controllerRef.current = null;
    };
  }, [taskId]);

  const sendControl = useCallback(
    async (command: GatewayControlCommand, reason?: string) => {
      if (!taskId) {
        throw new ApiError('未选择任务', 0, 'no_task');
      }
      const response = await api.sendGatewayControl(taskId, { command, reason });
      // The §7 control response carries the authoritative post-op status.
      setSnapshot((prev) =>
        prev
          ? {
              ...prev,
              status: response.status,
              last_event_sequence: Math.max(prev.last_event_sequence, response.event_sequence),
            }
          : prev,
      );
      return response;
    },
    [taskId],
  );

  const sendTaskMessage = useCallback(
    async (conversationId: string, text: string) => {
      if (!taskId) {
        throw new ApiError('未选择任务', 0, 'no_task');
      }
      return api.sendGatewayTaskMessage(taskId, {
        message_id: newIdempotencyKey(),
        conversation_id: conversationId,
        text,
      });
    },
    [taskId],
  );

  const refresh = useCallback(async () => {
    await controllerRef.current?.refresh();
  }, []);

  return {
    snapshot,
    progress,
    events,
    cursor,
    streamConnected,
    reconnecting,
    streamError,
    refreshing,
    refresh,
    sendControl,
    sendTaskMessage,
  };
}
