import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  calibrateCursor,
  GATEWAY_SSE_EVENT_NAME,
  GatewayEventStream,
  parseSseData,
} from '../../frontend/src/gateway/eventStream';
import type { GatewaySseData } from '../../frontend/src/types';

/** 可驱动的 EventSource 替身：捕获实例、支持手动 emit。 */
class FakeSource {
  static instances: FakeSource[] = [];
  readonly url: string;
  closed = false;
  private listeners: Record<string, Array<(event: { data?: string }) => void>> = {};

  constructor(url: string) {
    this.url = url;
    FakeSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: { data?: string }) => void): void {
    (this.listeners[type] ??= []).push(listener);
  }

  removeEventListener(type: string, listener: (event: { data?: string }) => void): void {
    this.listeners[type] = (this.listeners[type] ?? []).filter((l) => l !== listener);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, event: { data?: string } = {}): void {
    (this.listeners[type] ?? []).slice().forEach((listener) => listener(event));
  }
}

function frame(sequence: number, type = 'ActionExecuted', payload: Record<string, unknown> = {}): string {
  return JSON.stringify({ sequence, type, payload });
}

function makeStream(handlers: { onEvent?: (e: GatewaySseData) => void; onDrop?: () => void; onOpen?: () => void } = {}) {
  return new GatewayEventStream({
    url: (after) => `https://console.local/api/v1/tasks/t1/events/stream?after_sequence=${after}`,
    createSource: (url) => new FakeSource(url),
    onEvent: handlers.onEvent ?? (() => {}),
    onDrop: handlers.onDrop,
    onOpen: handlers.onOpen,
  });
}

beforeEach(() => {
  FakeSource.instances = [];
});

describe('Gateway 事件流客户端（§11）', () => {
  it('游标单调递增：重放与乱序重复被去重', () => {
    const received: number[] = [];
    const stream = makeStream({ onEvent: (e) => received.push(e.sequence) });
    stream.connect(0);
    const source = FakeSource.instances.at(-1)!;
    expect(source.url).toContain('after_sequence=0');

    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(1) });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(1) });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(0) });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(2) });

    expect(received).toEqual([1, 2]);
    expect(stream.lastSequence).toBe(2);
  });

  it('connect 只能推进游标，永远不回退', () => {
    const stream = makeStream();
    stream.connect(5);
    expect(FakeSource.instances.at(-1)!.url).toContain('after_sequence=5');

    stream.connect(2);
    expect(FakeSource.instances.at(-1)!.url).toContain('after_sequence=5');
    expect(stream.lastSequence).toBe(5);

    stream.connect(7);
    expect(FakeSource.instances.at(-1)!.url).toContain('after_sequence=7');
  });

  it('connect 拒绝非法游标', () => {
    const stream = makeStream();
    expect(() => stream.connect(-1)).toThrow(TypeError);
    expect(() => stream.connect(1.5)).toThrow(TypeError);
    expect(FakeSource.instances).toHaveLength(0);
  });

  it('断线：关闭源、回调 onDrop，随后按当前游标重开', () => {
    let drops = 0;
    const stream = makeStream({ onDrop: () => { drops += 1; } });
    stream.connect(0);
    const source = FakeSource.instances.at(-1)!;
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(7) });
    expect(stream.lastSequence).toBe(7);

    source.emit('error');
    expect(source.closed).toBe(true);
    expect(stream.connected).toBe(false);
    expect(drops).toBe(1);

    // 旧源上的后续帧不再被接受。
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(8) });
    expect(stream.lastSequence).toBe(7);

    // 重连时即使请求更小的游标，也按已接受的最大序列打开。
    stream.connect(3);
    expect(FakeSource.instances.at(-1)!.url).toContain('after_sequence=7');
    expect(stream.connected).toBe(true);
  });

  it('非法 data 帧被忽略，连接保持打开', () => {
    const received: number[] = [];
    const stream = makeStream({ onEvent: (e) => received.push(e.sequence) });
    stream.connect(0);
    const source = FakeSource.instances.at(-1)!;

    source.emit(GATEWAY_SSE_EVENT_NAME, { data: 'not-json' });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: JSON.stringify({ sequence: 1, type: 'X' }) });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: JSON.stringify({ sequence: 1, type: 'X', payload: [] }) });
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: JSON.stringify({ sequence: 1.5, type: 'X', payload: {} }) });
    source.emit(GATEWAY_SSE_EVENT_NAME);

    expect(received).toEqual([]);
    expect(source.closed).toBe(false);
    expect(stream.connected).toBe(true);

    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(1) });
    expect(received).toEqual([1]);
  });

  it('teardown 后不再触发任何回调', () => {
    const received: number[] = [];
    let opens = 0;
    const stream = makeStream({ onEvent: (e) => received.push(e.sequence), onOpen: () => { opens += 1; } });
    stream.connect(0);
    const source = FakeSource.instances.at(-1)!;
    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(1) });
    expect(received).toEqual([1]);

    stream.teardown();
    expect(source.closed).toBe(true);
    expect(stream.connected).toBe(false);

    source.emit(GATEWAY_SSE_EVENT_NAME, { data: frame(2) });
    source.emit('open');
    expect(received).toEqual([1]);
    expect(opens).toBe(0);
  });

  it('parseSseData 严格校验冻结的三键 data 帧', () => {
    expect(parseSseData(frame(3, 'TaskPaused', { reason: 'x' }))).toEqual({
      sequence: 3,
      type: 'TaskPaused',
      payload: { reason: 'x' },
    });
    expect(parseSseData(undefined)).toBeNull();
    expect(parseSseData('')).toBeNull();
    expect(parseSseData('[1, 2]')).toBeNull();
    expect(parseSseData(JSON.stringify({ sequence: '3', type: 'T', payload: {} }))).toBeNull();
    expect(parseSseData(JSON.stringify({ sequence: 3, type: 4, payload: {} }))).toBeNull();
    expect(parseSseData(JSON.stringify({ sequence: 3, type: 'T', payload: null }))).toBeNull();
  });

  it('calibrateCursor 取本地游标与快照序列的最大值', () => {
    expect(calibrateCursor(3, 5)).toBe(5);
    expect(calibrateCursor(7, 2)).toBe(7);
    expect(calibrateCursor(4, 4)).toBe(4);
  });
});
