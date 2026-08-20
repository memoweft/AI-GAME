import type {
  Approval,
  ApprovalDecisionResponse,
  ApprovalDecisionRequest,
  CreateRunRequest,
  EventRecord,
  ListResponse,
  OverviewResponse,
  Run,
  RunActionRequest,
  RuntimeInfo,
  Target,
  TargetDiscoveryResponse,
  Workflow,
  ApiErrorPayload,
  ChatSessionListResponse,
  ChatSession,
  ChatTranscriptResponse,
  ChatTurn,
  CreateChatSessionRequest,
  CreateChatTurnRequest,
  CloudChatConfig,
  CloudConnectionTestResult,
  SaveCloudChatConfigRequest,
  CreateLearningJobRequest,
  LearningJob,
  LearningJobListResponse,
  LearningProfileListResponse,
  CreateMobileTaskRequest,
  MobileTaskListResponse,
  MobileTaskState,
  SendMobileTaskInputRequest,
  StopMobileTaskRequest,
  ApplicationCommandRequest,
  ApplicationInstance,
  ApplicationInstanceListResponse,
  CreateApplicationInstanceRequest,
  SoulSchedulerStatus,
  GatewayControlCommand,
  GatewayControlResponse,
  GatewayDeviceListResponse,
  GatewayEventPage,
  GatewayTaskCreatedResponse,
  GatewayTaskListResponse,
  GatewayTaskMessageResponse,
  GatewayTaskResponse,
} from './types';

/** §16: the Console identifies itself as one stable Gateway client. */
export const GATEWAY_CLIENT_ID = 'ai-game-console';

/**
 * Fresh idempotency key per write (§14). A failed request is NOT retried
 * with the same key by this client — the user explicitly retries.
 *
 * Falls back to a non-crypto key in environments without
 * `crypto.randomUUID` (e.g. jsdom in tests) — the key only needs to be
 * unique per request, not cryptographically strong.
 */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function writeHeaders(clientId: boolean): Record<string, string> {
  return {
    'Idempotency-Key': newIdempotencyKey(),
    ...(clientId ? { 'X-Client-Id': GATEWAY_CLIENT_ID } : {}),
  };
}

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
export const API_BASE = (configuredBase || '/api/v1').replace(/\/$/, '');

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function errorMessage(payload: ApiErrorPayload | null, fallback: string): string {
  if (!payload) return fallback;
  if (typeof payload.error === 'object' && payload.error?.message) return payload.error.message;
  if (typeof payload.error === 'string') return payload.error;
  return payload.message || payload.detail || fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isPost = init?.method?.toUpperCase() === 'POST';
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(isPost ? { 'X-AI-Game-Client': 'console-v1' } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError('无法连接本地控制服务，请确认服务已启动。', 0, 'network_error');
  }

  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? ((await response.json()) as T | ApiErrorPayload)
    : null;

  if (!response.ok) {
    const apiError = payload as ApiErrorPayload | null;
    const nestedCode = apiError && typeof apiError.error === 'object'
      ? apiError.error.code
      : undefined;
    throw new ApiError(
      errorMessage(apiError, `请求失败（${response.status}）`),
      response.status,
      apiError?.code || nestedCode,
    );
  }

  return payload as T;
}

export const api = {
  getOverview: () => request<OverviewResponse>('/overview'),
  getTargets: () => request<ListResponse<Target>>('/targets'),
  discoverTargets: () =>
    request<TargetDiscoveryResponse>('/targets/discover', { method: 'POST' }),
  getWorkflows: () => request<ListResponse<Workflow>>('/workflows'),
  getRuns: () => request<ListResponse<Run>>('/runs'),
  createRun: (body: CreateRunRequest) =>
    request<Run>('/runs', { method: 'POST', body: JSON.stringify(body) }),
  runAction: (id: string, body: RunActionRequest) =>
    request<Run>(`/runs/${encodeURIComponent(id)}/actions`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getApprovals: () => request<ListResponse<Approval>>('/approvals'),
  decideApproval: (id: string, body: ApprovalDecisionRequest) =>
    request<ApprovalDecisionResponse>(`/approvals/${encodeURIComponent(id)}/decision`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getEvents: (limit = 100) =>
    request<ListResponse<EventRecord>>(`/events?limit=${limit}`),
  getRuntime: () => request<RuntimeInfo>('/runtime'),
  getMobileTasks: (limit = 100) =>
    request<MobileTaskListResponse>(`/tasks?limit=${limit}`),
  createMobileTask: (body: CreateMobileTaskRequest) =>
    request<MobileTaskState>('/tasks', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getMobileTask: (id: string) =>
    request<MobileTaskState>(`/tasks/${encodeURIComponent(id)}`),
  sendMobileTaskInput: (id: string, body: SendMobileTaskInputRequest) =>
    request<MobileTaskState>(`/tasks/${encodeURIComponent(id)}/inputs`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  stopMobileTask: (id: string, body: StopMobileTaskRequest) =>
    request<MobileTaskState>(`/tasks/${encodeURIComponent(id)}/stop`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getCloudChatConfig: () => request<CloudChatConfig>('/settings/cloud'),
  saveCloudChatConfig: (body: SaveCloudChatConfigRequest) =>
    request<CloudChatConfig>('/settings/cloud', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  testCloudChatConfig: () =>
    request<CloudConnectionTestResult>('/settings/cloud/test', {
      method: 'POST',
    }),
  clearCloudChatConfig: (expectedRevision: number) =>
    request<CloudChatConfig>('/settings/cloud/clear', {
      method: 'POST',
      body: JSON.stringify({ expected_revision: expectedRevision }),
    }),
  getChatSessions: () => request<ChatSessionListResponse>('/chat/sessions'),
  createChatSession: (body: CreateChatSessionRequest) =>
    request<ChatSession>('/chat/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getChatSession: (id: string) =>
    request<ChatTranscriptResponse>(`/chat/sessions/${encodeURIComponent(id)}`),
  createChatTurn: (sessionId: string, body: CreateChatTurnRequest) =>
    request<ChatTurn>(`/chat/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  cancelChatTurn: (turnId: string) =>
    request<ChatTurn>(`/chat/turns/${encodeURIComponent(turnId)}/cancel`, {
      method: 'POST',
    }),
  getLearningProfiles: () =>
    request<LearningProfileListResponse>('/learning/profiles'),
  getLearningJobs: () =>
    request<LearningJobListResponse>('/learning/jobs'),
  createLearningJob: (body: CreateLearningJobRequest) =>
    request<LearningJob>('/learning/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getLearningJob: (jobId: string) =>
    request<LearningJob>(`/learning/jobs/${encodeURIComponent(jobId)}`),
  stopLearningJob: (jobId: string) =>
    request<LearningJob>(`/learning/jobs/${encodeURIComponent(jobId)}/stop`, {
      method: 'POST',
    }),
  getApplicationInstances: (limit = 100) =>
    request<ApplicationInstanceListResponse>(`/application-instances?limit=${limit}`),
  getSoulSchedulerStatus: () =>
    request<SoulSchedulerStatus>('/application-profiles/soul-reply-v1/scheduler'),
  createApplicationInstance: (body: CreateApplicationInstanceRequest) =>
    request<ApplicationInstance>('/application-instances', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getApplicationInstance: (instanceId: string) =>
    request<ApplicationInstance>(
      `/application-instances/${encodeURIComponent(instanceId)}`,
    ),
  commandApplicationInstance: (instanceId: string, body: ApplicationCommandRequest) =>
    request<ApplicationInstance>(
      `/application-instances/${encodeURIComponent(instanceId)}/commands`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
    ),
  // -- Gateway contract (Phase 6) ----------------------------------------
  getGatewayDevices: () => request<GatewayDeviceListResponse>('/devices'),
  listGatewayTasks: () => request<GatewayTaskListResponse>('/tasks'),
  getGatewayTask: (taskId: string) =>
    request<GatewayTaskResponse>(`/tasks/${encodeURIComponent(taskId)}`),
  getGatewayTaskEvents: (taskId: string, afterSequence: number, limit = 100) =>
    request<GatewayEventPage>(
      `/tasks/${encodeURIComponent(taskId)}/events?after_sequence=${afterSequence}&limit=${limit}`,
    ),
  createGatewayTask: (input: {
    goal: string;
    device_id: string;
    conversation_id: string;
    message_id: string;
  }) =>
    request<GatewayTaskCreatedResponse>('/tasks', {
      method: 'POST',
      body: JSON.stringify(input),
      headers: writeHeaders(true),
    }),
  sendGatewayTaskMessage: (
    taskId: string,
    input: { message_id: string; conversation_id: string; text: string },
  ) =>
    request<GatewayTaskMessageResponse>(
      `/tasks/${encodeURIComponent(taskId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify(input),
        headers: writeHeaders(false),
      },
    ),
  sendGatewayControl: (
    taskId: string,
    input: { command: GatewayControlCommand; reason?: string },
  ) =>
    request<GatewayControlResponse>(
      `/tasks/${encodeURIComponent(taskId)}/controls`,
      {
        method: 'POST',
        body: JSON.stringify(input),
        headers: writeHeaders(false),
      },
    ),
  sendGatewayConversationMessage: (
    conversationId: string,
    input: { message_id: string; text: string; device_id?: string },
  ) =>
    request<GatewayTaskCreatedResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify(input),
        headers: writeHeaders(true),
      },
    ),
  gatewayEventStreamUrl: (taskId: string, afterSequence: number) =>
    `${API_BASE}/tasks/${encodeURIComponent(taskId)}/events/stream?after_sequence=${afterSequence}`,
};
