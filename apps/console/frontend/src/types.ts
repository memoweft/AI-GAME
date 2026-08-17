/**
 * API contract types live in one place so the UI can follow backend contract
 * changes without leaking snake_case field knowledge through every component.
 */

export type RunStatus =
  | 'draft'
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'blocked';

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'withdrawn';
export type TargetStatus = 'ready' | 'offline' | 'unauthorized' | 'unknown';
export type WorkflowStatus = 'available' | 'external' | 'future' | 'disabled' | 'not_configured';
export type RuntimeStatus =
  | 'ready'
  | 'starting'
  | 'paused'
  | 'stopped'
  | 'not_configured'
  | 'unavailable'
  | 'error'
  | 'unknown';
export type EventLevel = 'info' | 'success' | 'warning' | 'error' | 'debug';

export interface Blocker {
  code: string;
  message: string;
}

export interface RuntimeComponent {
  status: RuntimeStatus | string;
  message?: string | null;
  version?: string | null;
}

export interface RuntimeCapability {
  id: string;
  name: string;
  status: RuntimeStatus | string;
  configured: boolean;
  detail?: string | null;
  blocker?: Blocker | string | null;
}

export interface RuntimeInfo {
  overall_status: RuntimeStatus | string;
  capabilities: RuntimeCapability[];
  /** Compatibility fields retained for early local builds. */
  status?: RuntimeStatus | string;
  message?: string | null;
  api?: RuntimeComponent | null;
  model?: RuntimeComponent | null;
  executor?: RuntimeComponent | null;
  model_status?: RuntimeStatus | string | null;
  executor_status?: RuntimeStatus | string | null;
  started_at?: string | null;
  updated_at?: string | null;
}

export interface OverviewCounts {
  targets?: number;
  ready_targets?: number;
  runs?: number;
  active_runs?: number;
  pending_approvals?: number;
  events?: number;
}

export interface OverviewSummary {
  workflow_count: number;
  target_count: number;
  active_run_count: number;
  pending_approval_count: number;
}

export interface Target {
  id: string;
  name: string;
  kind: string;
  status: TargetStatus | string;
  address: string | null;
  detail: string | null;
  capabilities: string[];
  source: string;
  external_id: string | null;
  details: {
    address?: string | null;
    detail?: string | null;
    capabilities?: string[];
    adb_state?: string | null;
    connection_type?: 'emulator' | 'usb' | 'wireless' | string | null;
    properties?: Record<string, string>;
    [key: string]: unknown;
  };
  discovered_at: string;
  last_seen_at: string;
  updated_at: string;
  /** Compatibility aliases retained for older local snapshots. */
  target_kind?: string | null;
  platform?: string | null;
  created_at?: string | null;
}

export interface Workflow {
  id: string;
  name: string;
  description?: string | null;
  status?: string | null;
  integration_status?: string | null;
  enabled?: boolean;
  target_kind?: string | null;
  target_kinds?: string[];
  requires_approval?: boolean;
  steps_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface Run {
  id: string;
  workflow_id: string;
  target_id: string;
  name: string;
  instruction?: string | null;
  exact_text?: string | null;
  has_exact_text?: boolean;
  exact_text_length?: number;
  exact_text_sha256?: string | null;
  requires_approval?: boolean;
  status: RunStatus | string;
  blockers?: Blocker[];
  blocker?: Blocker | string | null;
  workflow_name?: string | null;
  target_name?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface Approval {
  id: string;
  run_id: string;
  title?: string | null;
  description?: string | null;
  reason?: string | null;
  risk_level?: string | null;
  status: ApprovalStatus | string;
  note?: string | null;
  created_at?: string | null;
  decided_at?: string | null;
}

export interface EventRecord {
  id: string;
  level?: EventLevel | string;
  type?: string | null;
  event_type?: string | null;
  title?: string | null;
  message: string;
  data?: Record<string, unknown> | null;
  run_id?: string | null;
  target_id?: string | null;
  created_at?: string | null;
}

export interface OverviewResponse {
  summary: OverviewSummary;
  run_status_counts: Partial<Record<RunStatus, number>>;
  recent_runs: Run[];
  runtime: RuntimeInfo;
  /** Compatibility fields retained for early local builds. */
  counts?: OverviewCounts;
  targets?: Target[];
  runs?: Run[];
  approvals?: Approval[];
  events?: EventRecord[];
}

export interface ListResponse<T> {
  items: T[];
  count: number;
}

export interface TargetDiscovery {
  adb_status: string;
  adb_path?: string | null;
  message: string;
  device_count: number;
}

export interface TargetDiscoveryResponse extends ListResponse<Target> {
  discovery: TargetDiscovery;
}

export interface CreateRunRequest {
  workflow_id: string;
  target_id: string;
  name: string | null;
  instruction: string;
  exact_text: string | null;
  requires_approval: boolean;
}

export type RunAction = 'pause' | 'resume' | 'cancel';

export interface RunActionRequest {
  action: RunAction;
}

export type ApprovalDecision = 'approved' | 'rejected';

export interface ApprovalDecisionRequest {
  decision: ApprovalDecision;
  note: string;
}

export interface ApprovalDecisionResponse {
  approval: Approval;
  run: Run;
}

export interface ApiErrorPayload {
  error?: string | { code?: string; message?: string };
  code?: string;
  message?: string;
  detail?: string;
}

export type MobileTaskStatus =
  | 'queued'
  | 'planning'
  | 'running'
  | 'stopping'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'uncertain';

export type MobileSubgoalStatus = 'pending' | 'active' | 'completed';

export interface MobileTaskSubgoal {
  index: number;
  description: string;
  status: MobileSubgoalStatus | string;
}

export interface MobileTaskPlan {
  revision: number;
  subgoals: MobileTaskSubgoal[];
}

export interface MobileTaskInput {
  revision: number;
  content: string;
  lifecycle: 'accepted' | 'applied' | string;
  client_request_id: string;
  created_at: string;
  applied_at: string | null;
}

export interface MobileTaskVerification {
  satisfied: boolean;
  progress: boolean;
  uncertain: boolean;
  evidence: string;
}

/**
 * Deliberately projected attempt summary. Raw intent arguments and text never
 * cross the console HTTP seam.
 */
export interface MobileTaskAttempt {
  id: string;
  sequence: number;
  subgoal_index: number;
  action_type: string;
  transport_status: string | null;
  verification: MobileTaskVerification | null;
  created_at: string;
  finalized_at: string | null;
}

export interface MobileTaskReflection {
  sequence: number;
  previous_strategy: string;
  strategy: string;
  reason: string;
  consecutive_no_progress: number;
  created_at: string;
}

/** Event payload data is intentionally omitted from the console projection. */
export interface MobileTaskEvent {
  sequence: number;
  event_type: string;
  created_at: string;
}

export interface MobileTaskState {
  id: string;
  goal: string;
  target_id: string | null;
  skill_id: string | null;
  status: MobileTaskStatus | string;
  input_revision: number;
  plan: MobileTaskPlan | null;
  active_subgoal_index: number;
  strategy: string;
  no_progress_count: number;
  reflection_count: number;
  attempt_count: number;
  cancel_requested: boolean;
  verification_satisfied: boolean;
  detail: string | null;
  error_code: string | null;
  skill_memory_version: number;
  inputs: MobileTaskInput[];
  attempts: MobileTaskAttempt[];
  reflections: MobileTaskReflection[];
  events: MobileTaskEvent[];
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface MobileTaskListResponse extends ListResponse<MobileTaskState> {}

export interface CreateMobileTaskRequest {
  goal: string;
  client_request_id: string;
  target_id?: string | null;
  skill_id?: string | null;
}

export interface SendMobileTaskInputRequest {
  content: string;
  client_request_id: string;
}

export interface StopMobileTaskRequest {
  client_request_id: string;
}

export type ChatMode = 'local_chat' | 'cloud_execute';

export type ChatTurnStatus =
  | 'accepted'
  | 'queued'
  | 'thinking'
  | 'planning'
  | 'executing'
  | 'awaiting_user'
  | 'stopping'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface ChatSession {
  id: string;
  title: string;
  mode: ChatMode;
  target_id?: string | null;
  auto_execute: boolean;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  turn_id?: string | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  delivery_status?: 'queued' | 'applied' | 'rejected' | null;
  input_revision?: number | null;
  applied_at?: string | null;
  provider?: string | null;
  model?: string | null;
  created_at: string;
}

export interface ChatTurn {
  id: string;
  session_id: string;
  mode: ChatMode;
  target_id?: string | null;
  auto_execute: boolean;
  status: ChatTurnStatus | string;
  reply_status?: string | null;
  execution_status?: string | null;
  step_count: number;
  input_revision: number;
  blocker?: string | null;
  detail?: string | null;
  error_code?: string | null;
  cancel_requested?: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChatStep {
  id: number | string;
  turn_id: string;
  step_index: number;
  state: string;
  action_type?: string | null;
  summary: string;
  created_at: string;
}

export interface ChatSessionListResponse extends ListResponse<ChatSession> {}

export interface ChatTranscriptResponse {
  session: ChatSession;
  messages: ChatMessage[];
  turns: ChatTurn[];
  steps: ChatStep[];
}

export interface CreateChatSessionRequest {
  title: string | null;
  mode: ChatMode;
  target_id: string | null;
  auto_execute: boolean;
}

export interface CreateChatTurnRequest {
  content: string;
  client_request_id: string;
}

export type CloudConfigStatus = 'not_configured' | 'unknown' | 'ready' | 'error';

export interface CloudChatConfig {
  endpoint: string | null;
  model: string | null;
  has_api_key: boolean;
  configured: boolean;
  credential_source: 'none' | 'startup' | 'console';
  status: CloudConfigStatus;
  detail: string;
  revision: number;
  updated_at: string | null;
}

export interface SaveCloudChatConfigRequest {
  endpoint: string;
  model: string;
  api_key?: string;
  expected_revision: number;
}

export interface CloudConnectionTestResult {
  ok: boolean;
  status: 'ready' | 'error';
  detail: string;
  latency_ms?: number | null;
}

export interface LearningBudget {
  max_transitions?: number | null;
  max_steps?: number | null;
  max_duration_seconds?: number | null;
  max_episode_seconds?: number | null;
  max_training_jobs?: number | null;
  [key: string]: unknown;
}

export interface LearningProfile {
  id: string;
  name?: string | null;
  display_name?: string | null;
  version?: number | string | null;
  revision?: number | string | null;
  game?: string | null;
  target_id?: string | null;
  default_target_id?: string | null;
  target_name?: string | null;
  enabled?: boolean;
  status?: string | null;
  budget?: LearningBudget | string | null;
  budget_summary?: string | null;
  safety_boundary?: string | string[] | null;
  safety_summary?: string | null;
  max_transitions?: number | null;
  max_actions?: number | null;
  max_duration_seconds?: number | null;
  no_progress_limit?: number | null;
  allowed_actions?: string[] | null;
  detail?: string | null;
  [key: string]: unknown;
}

export interface LearningMetrics {
  transition_count?: number | null;
  transitions_used?: number | null;
  total_reward?: number | null;
  verified_successes?: number | null;
  policy_memory_revision?: number | string | null;
  policy_memory_count?: number | null;
  [key: string]: unknown;
}

export interface LearningPolicyMemory {
  revision?: number | string | null;
  count?: number | null;
  [key: string]: unknown;
}

export interface LearningJob {
  id: string;
  profile_id?: string | null;
  profile_name?: string | null;
  target_id?: string | null;
  instruction?: string | null;
  status?: string | null;
  phase?: string | null;
  result?: string | null;
  outcome?: string | null;
  control_state?: string | null;
  policy_state?: string | null;
  transition_count?: number | null;
  transitions_used?: number | null;
  total_reward?: number | null;
  verified_successes?: number | null;
  policy_memory_revision?: number | string | null;
  policy_memory_count?: number | null;
  metrics?: LearningMetrics | null;
  policy_memory?: LearningPolicyMemory | null;
  detail?: string | null;
  error?: string | null;
  error_code?: string | null;
  cancel_requested?: boolean;
  policy_version?: number | string | null;
  max_transitions?: number | null;
  elapsed_ms?: number | null;
  wall_clock_budget_ms?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  [key: string]: unknown;
}

export interface LearningProfileListResponse extends ListResponse<LearningProfile> {}

export interface LearningJobListResponse extends ListResponse<LearningJob> {}

export interface CreateLearningJobRequest {
  instruction: string;
  client_request_id: string;
  profile_id?: string;
  target_id?: string;
}

export type ApplicationInstanceStatus =
  | 'queued'
  | 'running'
  | 'waiting'
  | 'paused'
  | 'stopping'
  | 'stopped'
  | 'completed'
  | 'failed';

export type ApplicationOutcomeStatus =
  | 'confirmed_success'
  | 'confirmed_failure'
  | 'unconfirmed'
  | 'uncertain';

export type ApplicationCommandName = 'Input' | 'Pause' | 'Resume' | 'Stop';

export type SoulSchedulerState =
  | 'running'
  | 'paused'
  | 'stopped'
  | 'degraded';

export type SoulSchedulerDesiredState = 'running' | 'paused' | 'stopped';

export type SoulSchedulerEffectiveState =
  | 'running'
  | 'paused'
  | 'stopping'
  | 'stopped';

export interface SoulSchedulerStatus {
  profile_id: 'soul-reply-v1';
  state: SoulSchedulerState;
  desired_state: SoulSchedulerDesiredState;
  effective_state: SoulSchedulerEffectiveState;
  controller_matches: boolean;
  code: string;
  observed_at: string;
}

export interface ApplicationIntentSummary {
  id: string;
  cycle: number;
  revision: number;
  phase: string;
  hard_risk: boolean;
  created_at: string;
  finalized_at: string | null;
}

export interface ApplicationOutcomeSummary {
  cycle: number;
  status: ApplicationOutcomeStatus;
  hard_risk: boolean;
  terminal: boolean;
  created_at: string;
}

export interface ApplicationInstance {
  id: string;
  profile_id: string;
  status: ApplicationInstanceStatus;
  revision: number;
  degraded: boolean;
  hard_risk: boolean;
  error_code: string | null;
  memory_version: number;
  input_count: number;
  intent_count: number;
  outcome_count: number;
  event_count: number;
  intents: ApplicationIntentSummary[];
  outcomes: ApplicationOutcomeSummary[];
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  wake_at: string | null;
}

export interface ApplicationInstanceListResponse {
  items: ApplicationInstance[];
  count: number;
}

export interface CreateApplicationInstanceRequest {
  profile_id: string;
  client_request_id: string;
  target_id?: string;
  initial_input?: string;
}

export interface ApplicationCommandRequest {
  command: ApplicationCommandName;
  client_request_id: string;
  content?: string;
}
