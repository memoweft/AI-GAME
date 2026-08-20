// ---------------------------------------------------------------------------
// Gateway event stream client (Phase 6, contract §11).
//
// The server ignores the browser's Last-Event-ID header on reconnect, so the
// client owns the resume cursor and always re-opens the connection with an
// explicit `after_sequence` query parameter. The cursor is strictly
// monotonic:
//   * an event is accepted only when its sequence is GREATER than the cursor
//     (this deduplicates the replay the server performs on every fresh
//     connection, and survives snapshot/event calibration);
//   * `connect(afterSequence)` can never move the cursor backwards.
//
// Heartbeat frames are SSE comment lines (`: heartbeat`) and never reach
// message listeners, so they have zero cursor impact by construction.
// ---------------------------------------------------------------------------

import type { GatewaySseData } from '../types';

/** The exact event name the gateway emits on the stream. */
export const GATEWAY_SSE_EVENT_NAME = 'runtime_event';

export interface GatewayEventMessage {
  data?: string;
}

/** Minimal EventSource surface — injectable so tests can drive the stream. */
export interface GatewayEventSourceLike {
  addEventListener(type: string, listener: (event: GatewayEventMessage) => void): void;
  removeEventListener(type: string, listener: (event: GatewayEventMessage) => void): void;
  close(): void;
}

export interface GatewayEventStreamOptions {
  /** Build the stream URL for a cursor; the cursor travels in the query string. */
  url: (afterSequence: number) => string;
  /** Create the underlying source. */
  createSource: (url: string) => GatewayEventSourceLike;
  /** Called once per accepted (deduplicated) event, in arrival order. */
  onEvent: (event: GatewaySseData) => void;
  /** Called when a connection is lost or fails, after the source is torn down. */
  onDrop?: () => void;
  /** Called when a fresh connection reports `open`. */
  onOpen?: () => void;
}

export class GatewayEventStream {
  private source: GatewayEventSourceLike | null = null;
  private cursor = 0;
  private ownerClosed = false;

  constructor(private readonly options: GatewayEventStreamOptions) {}

  /** Current resume cursor (last accepted sequence). */
  get lastSequence(): number {
    return this.cursor;
  }

  /** Whether a source is currently open (or still connecting). */
  get connected(): boolean {
    return this.source !== null;
  }

  /**
   * Open (or re-open) the stream from an explicit cursor. The requested
   * cursor can only advance the local one — never regress it.
   */
  connect(afterSequence: number): void {
    this.teardown();
    if (!Number.isInteger(afterSequence) || afterSequence < 0) {
      throw new TypeError('afterSequence must be a non-negative integer');
    }
    if (afterSequence > this.cursor) {
      this.cursor = afterSequence;
    }
    this.ownerClosed = false;
    const source = this.options.createSource(this.options.url(this.cursor));
    this.source = source;
    source.addEventListener(GATEWAY_SSE_EVENT_NAME, this.handleMessage);
    source.addEventListener('open', this.handleOpen);
    source.addEventListener('error', this.handleError);
  }

  /** Close the stream; no further callbacks fire after this. */
  teardown(): void {
    this.ownerClosed = true;
    this.closeSource();
  }

  private closeSource(): void {
    const source = this.source;
    this.source = null;
    if (!source) {
      return;
    }
    source.removeEventListener(GATEWAY_SSE_EVENT_NAME, this.handleMessage);
    source.removeEventListener('open', this.handleOpen);
    source.removeEventListener('error', this.handleError);
    source.close();
  }

  private handleOpen = (event: GatewayEventMessage): void => {
    void event;
    if (!this.ownerClosed) {
      this.options.onOpen?.();
    }
  };

  private handleMessage = (event: GatewayEventMessage): void => {
    if (this.ownerClosed) {
      return;
    }
    const parsed = parseSseData(event.data);
    if (parsed === null) {
      // Malformed frame: ignore silently; the connection stays open.
      return;
    }
    if (parsed.sequence <= this.cursor) {
      // Replay or out-of-order duplicate: dedup by the monotonic cursor.
      return;
    }
    this.cursor = parsed.sequence;
    this.options.onEvent(parsed);
  };

  private handleError = (event: GatewayEventMessage): void => {
    void event;
    if (this.ownerClosed) {
      return;
    }
    // The server ignores Last-Event-ID: the built-in browser retry cannot
    // resume correctly, so tear down and let the owner reconnect with an
    // explicit cursor (after re-calibrating against a fresh Snapshot).
    this.closeSource();
    this.options.onDrop?.();
  };
}

/**
 * Parse a frozen §11 `data:` frame. Exactly three keys —
 * `sequence: number`, `type: string`, `payload: object` — are accepted;
 * anything else returns null.
 */
export function parseSseData(raw: string | undefined): GatewaySseData | null {
  if (typeof raw !== 'string' || raw.length === 0) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const candidate = parsed as Record<string, unknown>;
  if (
    typeof candidate['sequence'] !== 'number' ||
    !Number.isInteger(candidate['sequence']) ||
    typeof candidate['type'] !== 'string' ||
    typeof candidate['payload'] !== 'object' ||
    candidate['payload'] === null ||
    Array.isArray(candidate['payload'])
  ) {
    return null;
  }
  return {
    sequence: candidate['sequence'],
    type: candidate['type'],
    payload: candidate['payload'] as Record<string, unknown>,
  };
}

/**
 * §11 snapshot/event calibration: after a reconnect the client re-reads the
 * Snapshot and merges the two cursors. The result is the MAX of the local
 * (already-accepted) cursor and the snapshot's `last_event_sequence` —
 * never a regression.
 */
export function calibrateCursor(
  localSequence: number,
  snapshotLastEventSequence: number,
): number {
  return Math.max(localSequence, snapshotLastEventSequence);
}
