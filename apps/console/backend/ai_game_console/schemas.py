from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .text_input import is_valid_text_input


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class WorkflowSchema(ApiModel):
    id: str
    name: str
    description: str
    status: str
    target_kinds: list[str]
    requires_approval: bool
    target_kind: str
    enabled: bool
    integration_status: str
    created_at: str
    updated_at: str


class TargetSchema(ApiModel):
    id: str
    name: str
    kind: str
    status: str
    address: str | None
    detail: str | None
    capabilities: list[str]
    source: str
    external_id: str | None
    details: dict[str, Any]
    discovered_at: str
    last_seen_at: str
    updated_at: str


class BlockerSchema(ApiModel):
    code: str
    message: str


class RunSummarySchema(ApiModel):
    id: str
    name: str
    workflow_id: str
    workflow_name: str
    target_id: str
    target_name: str
    instruction: str
    requires_approval: bool
    status: str
    blocker: str | None
    blockers: list[BlockerSchema]
    has_exact_text: bool
    exact_text_length: int
    exact_text_sha256: str | None
    created_at: str
    updated_at: str


class RunDetailSchema(RunSummarySchema):
    exact_text: str | None


class ApprovalSchema(ApiModel):
    id: str
    run_id: str
    status: str
    note: str | None
    created_at: str
    decided_at: str | None
    updated_at: str


class EventSchema(ApiModel):
    id: int
    run_id: str | None
    type: str
    event_type: str
    message: str
    level: str
    data: dict[str, Any]
    created_at: str


class CapabilitySchema(ApiModel):
    id: str
    name: str
    status: str
    configured: bool
    detail: str
    blocker: BlockerSchema | None


class RunCreate(ApiModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=256)
    instruction: str = Field(min_length=1, max_length=10_000)
    exact_text: str | None = Field(default=None, max_length=10_000)
    requires_approval: StrictBool = False
    name: str | None = Field(default=None, max_length=200)

    @field_validator("workflow_id", "target_id", "instruction")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("name")
    @classmethod
    def optional_name_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class RunActionRequest(ApiModel):
    action: Literal["pause", "resume", "cancel"]


_ALLOWED_ADB_KEYCODES = {
    "KEYCODE_APP_SWITCH",
    "KEYCODE_BACK",
    "KEYCODE_DEL",
    "KEYCODE_DPAD_CENTER",
    "KEYCODE_DPAD_DOWN",
    "KEYCODE_DPAD_LEFT",
    "KEYCODE_DPAD_RIGHT",
    "KEYCODE_DPAD_UP",
    "KEYCODE_ENTER",
    "KEYCODE_HOME",
    "KEYCODE_TAB",
}


class ExecutorActionRequest(ApiModel):
    """Validated shape for exactly one low-level Android input action."""

    target_id: str = Field(min_length=1, max_length=256)
    action: Literal["tap", "keyevent", "text"]
    x: StrictInt | None = Field(default=None, ge=0, le=10_000)
    y: StrictInt | None = Field(default=None, ge=0, le=10_000)
    keycode: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("target_id")
    @classmethod
    def executor_target_id_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def require_only_the_parameters_for_the_selected_action(self) -> "ExecutorActionRequest":
        if self.action == "tap":
            if self.x is None or self.y is None or self.keycode is not None or self.text is not None:
                raise ValueError("tap requires x and y only")
        elif self.action == "keyevent":
            if (
                self.keycode not in _ALLOWED_ADB_KEYCODES
                or self.x is not None
                or self.y is not None
                or self.text is not None
            ):
                raise ValueError("keyevent requires one allowed keycode only")
        else:
            if self.text is None or self.x is not None or self.y is not None or self.keycode is not None:
                raise ValueError("text requires text only")
            if not is_valid_text_input(self.text):
                raise ValueError("text must use supported printable characters")
        return self


class ApprovalDecisionRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=2_000)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class WorkflowListResponse(ApiModel):
    items: list[WorkflowSchema]
    count: int


class TargetListResponse(ApiModel):
    items: list[TargetSchema]
    count: int


class RunListResponse(ApiModel):
    items: list[RunSummarySchema]
    count: int


class ApprovalListResponse(ApiModel):
    items: list[ApprovalSchema]
    count: int


class EventListResponse(ApiModel):
    items: list[EventSchema]
    count: int


class DiscoverySummary(ApiModel):
    adb_status: str
    adb_path: str | None
    message: str
    device_count: int


class TargetDiscoveryResponse(TargetListResponse):
    discovery: DiscoverySummary


class ApprovalDecisionResponse(ApiModel):
    approval: ApprovalSchema
    run: RunSummarySchema


class RuntimeResponse(ApiModel):
    overall_status: str
    capabilities: list[CapabilitySchema]


class ExecutorActionResponse(ApiModel):
    target_id: str
    action: Literal["tap", "keyevent", "text"]
    transport_status: Literal["accepted"]
    detail: str


class OverviewSummary(ApiModel):
    workflow_count: int
    target_count: int
    active_run_count: int
    pending_approval_count: int


class OverviewResponse(ApiModel):
    summary: OverviewSummary
    run_status_counts: dict[str, int]
    recent_runs: list[RunSummarySchema]
    runtime: RuntimeResponse


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str
    database: str


class CloudChatConfigUpdate(ApiModel):
    endpoint: str = Field(min_length=1, max_length=2_048)
    model: str = Field(min_length=1, max_length=256)
    api_key: str | None = Field(default=None, min_length=1, max_length=8_192)
    expected_revision: int = Field(ge=0)

    @field_validator("endpoint", "model")
    @classmethod
    def normalize_cloud_config_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("api_key")
    @classmethod
    def reject_blank_cloud_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class CloudChatConfigRevision(ApiModel):
    expected_revision: int = Field(ge=0)


class CloudChatConfigResponse(ApiModel):
    endpoint: str | None
    model: str | None
    has_api_key: bool
    configured: bool
    credential_source: Literal["none", "startup", "console"]
    status: Literal["not_configured", "unknown", "ready", "error"]
    detail: str
    revision: int
    updated_at: str | None


class CloudConnectionTestResponse(ApiModel):
    ok: bool
    status: Literal["ready", "error"]
    detail: str
    latency_ms: int | None = None


class ChatSessionCreate(ApiModel):
    title: str | None = Field(default=None, max_length=200)
    mode: Literal["local_chat", "cloud_execute"]
    target_id: str | None = Field(default=None, max_length=256)
    auto_execute: StrictBool = False

    @field_validator("title", "target_id")
    @classmethod
    def normalize_optional_chat_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ChatTurnCreate(ApiModel):
    content: str = Field(min_length=1, max_length=20_000)
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def chat_content_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str) -> str:
        normalized = value.strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        if not normalized or any(character not in allowed for character in normalized):
            raise ValueError("client_request_id contains unsupported characters")
        return normalized


class ChatSessionSchema(ApiModel):
    id: str
    title: str
    mode: Literal["local_chat", "cloud_execute"]
    target_id: str | None
    auto_execute: bool
    status: str
    created_at: str
    updated_at: str


class ChatMessageSchema(ApiModel):
    id: str
    session_id: str
    turn_id: str | None
    role: Literal["user", "assistant", "system"]
    content: str
    client_request_id: str | None
    content_sha256: str | None
    input_revision: int | None
    delivery_status: Literal["queued", "applied", "rejected"] | None
    applied_at: str | None
    provider: str | None
    model: str | None
    created_at: str


class ChatTurnSchema(ApiModel):
    id: str
    session_id: str
    mode: Literal["local_chat", "cloud_execute"]
    target_id: str | None
    auto_execute: bool
    input_revision: int
    status: str
    reply_status: str | None
    execution_status: str | None
    step_count: int
    blocker: str | None
    detail: str | None
    error_code: str | None
    cancel_requested: bool
    provider: str | None
    model: str | None
    created_at: str
    updated_at: str


class ChatStepSchema(ApiModel):
    id: int
    turn_id: str
    step_index: int
    state: str
    action_type: str | None
    summary: str
    created_at: str


class ChatSessionListResponse(ApiModel):
    items: list[ChatSessionSchema]
    count: int


class ChatTranscriptResponse(ApiModel):
    session: ChatSessionSchema
    messages: list[ChatMessageSchema]
    turns: list[ChatTurnSchema]
    steps: list[ChatStepSchema]


class LearningJobCreate(ApiModel):
    instruction: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=128)
    profile_id: str | None = Field(default=None, max_length=128)
    target_id: str | None = Field(default=None, max_length=256)

    @field_validator("instruction")
    @classmethod
    def learning_instruction_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("client_request_id")
    @classmethod
    def validate_learning_client_request_id(cls, value: str) -> str:
        normalized = value.strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        if not normalized or any(character not in allowed for character in normalized):
            raise ValueError("client_request_id contains unsupported characters")
        return normalized

    @field_validator("profile_id", "target_id")
    @classmethod
    def normalize_optional_learning_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class LearningProfileSchema(ApiModel):
    id: str
    name: str
    game: str
    scope_summary: str
    safety_summary: str
    budget_summary: str
    revision: int
    allowed_actions: list[str]
    max_transitions: int
    max_duration_seconds: float
    default_target_id: str | None


class LearningJobSchema(ApiModel):
    id: str
    client_request_id: str
    profile_id: str
    profile_revision: int
    target_id: str | None
    phase: Literal["accepted", "preflight", "collecting", "distilling", "validating", "stopping", "terminal"]
    result: Literal["pending", "learned", "not_learned", "failed", "stopped", "stopped_uncertain"]
    outcome: Literal["unknown", "confirmed_success", "confirmed_failure", "unconfirmed"]
    control_state: Literal["neutral", "active", "neutralizing", "uncertain"]
    policy_state: Literal["unchanged", "candidate", "promoted", "rejected"]
    transition_count: int
    max_transitions: int
    total_reward: float | None
    verified_successes: int | None
    policy_memory_revision: int
    policy_memory_count: int | None
    cancel_requested: bool
    detail: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


class LearningProfileListResponse(ApiModel):
    items: list[LearningProfileSchema]
    count: int


class LearningJobListResponse(ApiModel):
    items: list[LearningJobSchema]
    count: int


MobileTaskStatus = Literal[
    "queued",
    "planning",
    "running",
    "stopping",
    "completed",
    "failed",
    "stopped",
    "uncertain",
]


class MobileTaskCreate(ApiModel):
    goal: str = Field(min_length=1, max_length=10_000)
    client_request_id: str = Field(min_length=1, max_length=128)
    target_id: str | None = Field(default=None, max_length=256)
    skill_id: str | None = Field(default=None, max_length=256)

    @field_validator("goal")
    @classmethod
    def mobile_task_goal_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("client_request_id")
    @classmethod
    def validate_mobile_task_client_request_id(cls, value: str) -> str:
        return _validated_inert_request_id(value)

    @field_validator("target_id", "skill_id")
    @classmethod
    def normalize_optional_mobile_task_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class MobileTaskInputCreate(ApiModel):
    content: str = Field(min_length=1, max_length=10_000)
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def mobile_task_input_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("client_request_id")
    @classmethod
    def validate_mobile_task_input_request_id(cls, value: str) -> str:
        return _validated_inert_request_id(value)


class MobileTaskStopRequest(ApiModel):
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("client_request_id")
    @classmethod
    def validate_mobile_task_stop_request_id(cls, value: str) -> str:
        return _validated_inert_request_id(value)


class MobileTaskSubgoalSchema(ApiModel):
    index: int
    description: str
    status: Literal["pending", "active", "completed"]


class MobileTaskPlanSchema(ApiModel):
    revision: int
    subgoals: list[MobileTaskSubgoalSchema]


class MobileTaskInputSchema(ApiModel):
    revision: int
    content: str
    lifecycle: Literal["accepted", "applied"]
    client_request_id: str
    created_at: str
    applied_at: str | None


class MobileTaskVerificationSchema(ApiModel):
    satisfied: bool
    progress: bool
    uncertain: bool
    evidence: str


class MobileTaskAttemptSchema(ApiModel):
    id: str
    sequence: int
    subgoal_index: int
    action_type: str
    transport_status: Literal["not_sent", "accepted", "rejected", "uncertain"] | None
    verification: MobileTaskVerificationSchema | None
    created_at: str
    finalized_at: str | None


class MobileTaskReflectionSchema(ApiModel):
    sequence: int
    previous_strategy: str
    strategy: str
    reason: str
    consecutive_no_progress: int
    created_at: str


class MobileTaskEventSchema(ApiModel):
    sequence: int
    event_type: str
    created_at: str


class MobileTaskSchema(ApiModel):
    id: str
    goal: str
    target_id: str | None
    skill_id: str | None
    status: MobileTaskStatus
    input_revision: int
    plan: MobileTaskPlanSchema | None
    active_subgoal_index: int
    strategy: str
    no_progress_count: int
    reflection_count: int
    attempt_count: int
    cancel_requested: bool
    verification_satisfied: bool
    detail: str | None
    error_code: str | None
    skill_memory_version: int
    inputs: list[MobileTaskInputSchema]
    attempts: list[MobileTaskAttemptSchema]
    reflections: list[MobileTaskReflectionSchema]
    events: list[MobileTaskEventSchema]
    created_at: str
    updated_at: str
    finished_at: str | None


class MobileTaskListResponse(ApiModel):
    items: list[MobileTaskSchema]
    count: int


ApplicationInstanceStatus = Literal[
    "queued",
    "running",
    "waiting",
    "paused",
    "stopping",
    "stopped",
    "completed",
    "failed",
]

ApplicationOutcomeStatus = Literal[
    "confirmed_success",
    "confirmed_failure",
    "unconfirmed",
    "uncertain",
]

ApplicationCommandName = Literal["Input", "Pause", "Resume", "Stop"]
SoulSchedulerState = Literal["running", "paused", "stopped", "degraded"]
SoulSchedulerDesiredState = Literal["running", "paused", "stopped"]
SoulSchedulerEffectiveState = Literal[
    "running", "paused", "stopping", "stopped"
]


class ApplicationInstanceCreate(ApiModel):
    profile_id: str = Field(min_length=1, max_length=256)
    client_request_id: str = Field(min_length=1, max_length=128)
    target_id: str | None = Field(default=None, max_length=256)
    initial_input: str | None = Field(default=None, min_length=1, max_length=10_000)

    @field_validator("profile_id")
    @classmethod
    def application_profile_id_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("client_request_id")
    @classmethod
    def validate_application_start_request_id(cls, value: str) -> str:
        return _validated_inert_request_id(value)

    @field_validator("target_id")
    @classmethod
    def normalize_optional_application_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("initial_input")
    @classmethod
    def application_initial_input_must_not_be_blank(
        cls, value: str | None
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class ApplicationCommandCreate(ApiModel):
    command: ApplicationCommandName
    client_request_id: str = Field(min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=10_000)

    @field_validator("client_request_id")
    @classmethod
    def validate_application_command_request_id(cls, value: str) -> str:
        return _validated_inert_request_id(value)

    @model_validator(mode="after")
    def validate_application_command_content(self):
        if self.command == "Input":
            normalized = self.content.strip() if isinstance(self.content, str) else ""
            if not normalized:
                raise ValueError("Input requires non-blank content")
            self.content = normalized
        elif self.content is not None:
            raise ValueError(f"{self.command} does not accept content")
        return self


class ApplicationIntentSchema(ApiModel):
    id: str
    cycle: int
    revision: int
    phase: str
    hard_risk: bool
    created_at: str
    finalized_at: str | None


class ApplicationOutcomeSchema(ApiModel):
    cycle: int
    status: ApplicationOutcomeStatus
    hard_risk: bool
    terminal: bool
    created_at: str


class ApplicationInstanceSchema(ApiModel):
    id: str
    profile_id: str
    status: ApplicationInstanceStatus
    revision: int
    degraded: bool
    hard_risk: bool
    error_code: str | None
    memory_version: int
    input_count: int
    intent_count: int
    outcome_count: int
    event_count: int
    intents: list[ApplicationIntentSchema]
    outcomes: list[ApplicationOutcomeSchema]
    created_at: str
    updated_at: str
    finished_at: str | None
    wake_at: str | None


class ApplicationInstanceListResponse(ApiModel):
    items: list[ApplicationInstanceSchema]
    count: int


class SoulSchedulerStatusSchema(ApiModel):
    profile_id: Literal["soul-reply-v1"]
    state: SoulSchedulerState
    desired_state: SoulSchedulerDesiredState
    effective_state: SoulSchedulerEffectiveState
    controller_matches: bool
    code: str
    observed_at: str


SoulCommandName = Literal[
    "start",
    "pause",
    "resume",
    "stop",
    "queue_inventory",
    "chat_mode",
    "match_mode",
]


class SoulHealthSchema(ApiModel):
    status: str | None
    current_action: str | None
    heartbeat_at: str | None
    scheduler_heartbeat_at: str | None
    pause_reason: str | None
    last_error: str | None
    last_success: str | None
    next_retry_at: str | None


class SoulCountsSchema(ApiModel):
    pending_count: int | None
    pending_inbound_count: int | None
    human_count: int | None
    send_verifying_count: int | None
    today_matches: int | None
    today_match_acquisitions: int | None
    llm_calls: int | None
    llm_limit: int | None


class SoulMatchBalanceSchema(ApiModel):
    state: str | None
    remaining: int | None
    observed_at: str | None
    planet_unlocked: bool | None


class SoulStatusSchema(ApiModel):
    platform: str | None
    running: bool | None
    paused: bool | None
    stop_requested: bool | None
    run_state: str | None
    mode: str | None
    operation_mode: str | None
    health: SoulHealthSchema
    counts: SoulCountsSchema
    send_verification_counts: dict[str, int | None]
    match_balance: SoulMatchBalanceSchema


class SoulMatchSchema(ApiModel):
    id: int | None
    platform: str | None
    nickname: str | None
    status: str | None
    updated_at: str | None
    msg_count: int | None
    pending_inbound: bool | None
    send_verification_state: str | None


class SoulRankingSchema(ApiModel):
    match_id: int | None
    overall_score: float | None
    compatibility_score: float | None
    engagement_score: float | None
    evidence: list[Any] | None
    summary: str | None
    confidence: float | None
    status: str | None
    updated_at: str | None


class SoulMetricSchema(ApiModel):
    match_id: int | None
    active_days: int | None
    incoming_count: int | None
    response_rate: float | None
    conversation_depth: int | None
    engagement_state: str | None
    calculated_at: str | None


class SoulAutomationJobSchema(ApiModel):
    name: str | None
    status: str | None
    updated_at: str | None
    detail: str | None


class SoulWorkspaceResponse(ApiModel):
    connection: Literal["ready", "unavailable", "incompatible"]
    console_url: str
    observed_at: str | None
    available_commands: list[SoulCommandName]
    status: SoulStatusSchema | None
    matches: list[SoulMatchSchema]
    rankings: list[SoulRankingSchema]
    metrics: list[SoulMetricSchema]
    automation_jobs: list[SoulAutomationJobSchema]
    section_errors: dict[str, str]


class SoulConversationMessageSchema(ApiModel):
    role: str | None
    content: str | None
    created_at: str | None


class SoulConversationResponse(ApiModel):
    id: str
    match: dict[str, Any]
    messages: list[SoulConversationMessageSchema]


class SoulCommandRequest(ApiModel):
    command: SoulCommandName
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("client_request_id")
    @classmethod
    def soul_client_request_id_is_inert(cls, value: str) -> str:
        normalized = value.strip()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        if not normalized or any(character not in allowed for character in normalized):
            raise ValueError("client_request_id contains unsupported characters")
        return normalized


class SoulCommandResponse(ApiModel):
    command: SoulCommandName
    client_request_id: str
    status: Literal["accepted", "rejected", "uncertain"]
    detail: str
    workspace: SoulWorkspaceResponse | None


def _validated_inert_request_id(value: str) -> str:
    normalized = value.strip()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if not normalized or any(character not in allowed for character in normalized):
        raise ValueError("client_request_id contains unsupported characters")
    return normalized
