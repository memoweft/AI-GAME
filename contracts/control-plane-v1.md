# Control-plane HTTP contract v1

The console exposes a loopback-only JSON API under `/api/v1`. The browser UI and
typed resource Adapters use this Interface; SQLite, ADB discovery, model probes,
and process details stay behind the control-plane Module.

All state-changing requests use JSON and must include
`X-AI-Game-Client: console-v1`. The service does not enable cross-origin access
and accepts only localhost Host values.

## Truth boundaries

- `console=ready` means only the HTTP service and local data store are ready.
- Runtime `executor=not_configured` means the live GUI action channel cannot run.
- `MobileTask` is the primary generic Android task Interface. It is distinct
  from Chat, GameLearning, Soul, and legacy Run records.
- `ApplicationRuntime` is the generic long-lived application-cycle Interface.
  Soul is one production Profile (`soul-reply-v1`), not the AI-GAME product
  boundary and not a special universal Run.
- `202 Accepted` from a MobileTask POST means the request is durably accepted;
  it is not proof that planning began, an action was transported, a Subgoal was
  verified, or the Task completed.
- `202 Accepted` from an ApplicationRuntime POST means the instance or command
  was durably accepted. It is not proof that observation, local vision, cloud
  policy, owner dispatch, verification, or an application Outcome succeeded.
- MobileTask recovery resumes only a safe checkpoint. A persisted open physical
  intent becomes terminal `uncertain` and is never replayed.
- ApplicationRuntime persists a physical intent before owner reserve/dispatch.
  Recovery and nonterminal owner results use inspect-only reconciliation; an
  unfinished, active, or uncertain physical operation is never automatically
  reserved or dispatched again.
- A late `Input`, `Pause`, or `Stop` cannot rewrite a physical operation that
  already crossed the owner dispatch fence as not sent. Owner settlement or
  physical uncertainty wins, and later policy cycles observe the command.
- Soul matcher scheduling and Soul reply execution are separate facts. The
  managed dating-copilot scheduler owns all-day matching and immediate match
  openers; the `soul-reply-v1` Application owns ordinary replies and delayed
  reply-strategy learning.
- A confirmed Soul send is delivery proof, not a learning reward. Only later
  admissible interaction or no-response evidence may update reply strategy;
  an uncertain send is neither a positive example nor permission to resend.
- A queued run with `workflow_executor_not_connected` is saved work for the
  separate traditional task queue, not an active run and not evidence that the
  live chat/ADB executor is unavailable.
- An accepted request is never reported as a verified external action.
- Events are append-only operator evidence. They are not editable through v1.
- `local_chat` never captures a screenshot, calls a cloud provider, or invokes
  an executor.
- `cloud_execute` sends conversation text to the configured cloud provider, but
  raw device screenshots only go to the loopback local GUI model.
- `transported` means ADB accepted an atomic input. It does not mean the UI or
  business goal completed.
- A user Message with `delivery_status=applied` was included in a model-decision
  snapshot. It does not mean the model replied, an Android action was sent, or
  any goal succeeded.
- Cloud Settings responses never contain an API key. `has_api_key=true` means a
  credential is available to the runtime; it is not proof that the provider is
  reachable or that a model request has succeeded.
- MobileTask HTTP ActionAttempts and Events are redacted projections of a more
  detailed local journal. Owner goals and inputs remain visible in TaskState.

## Resources

The primary Interfaces are MobileTask, ApplicationRuntime, Targets, and
Settings. Chat and GameLearning are retained advanced/compatibility surfaces.
Workflow, Run, Approval, and the old Soul command bridge are grouped in the
legacy appendix and are not extension seams for new Adapters.

### Overview

`GET /api/v1/overview` returns the overview-specific snapshot:

```json
{
  "summary": {
    "workflow_count": 3,
    "target_count": 1,
    "active_run_count": 0,
    "pending_approval_count": 0
  },
  "run_status_counts": {},
  "recent_runs": [],
  "runtime": {
    "overall_status": "ready",
    "capabilities": []
  }
}
```

`summary.active_run_count` is retained for v1 compatibility, but means
**unfinished runs**: `awaiting_approval`, `queued`, `running`, or `paused`.
It must not be interpreted as a count of GUI actions currently running. The
primary frontend does not use it as the MobileTask activity count.

The other Workflow/Run/Approval counters and `recent_runs` are likewise legacy
compatibility fields. `/overview` is not the aggregate source of truth for
MobileTask, ApplicationRuntime, or the Soul scheduler; the browser reads those
typed resources directly.

The primary frontend presents a one-sentence MobileTask composer/inbox, a
typed Soul Application entry, and Device/Settings resource views. Chat and
GameLearning APIs/history, plus Workflow, Run, and Approval fields and routes,
remain compatibility or advanced data rather than first-level navigation. Those
collections are not embedded in `/overview`. There is no universal Run resource
or natural-language router behind this information architecture.

### Launcher shutdown (internal)

- `POST /api/v1/shutdown`

This is not a browser control. It is enabled only for a process constructed by
`ai_game_console.main`, requires both `X-AI-Game-Client: console-v1` and the
per-start `X-AI-Game-Shutdown-Token`, and accepts no request body. The token is
created by `scripts/console.ps1`, is stored only in ignored runtime state and
the child process environment, and is never returned by the API.

`202 {"status":"accepted"}` means Uvicorn has been asked to exit; it does not
claim that runtime cleanup has already completed. The launcher waits for process
exit and uses an identity-checked hard-stop fallback only after the graceful
window expires. Direct ASGI use has no callback and returns
`503 console_shutdown_unavailable` when a valid token is supplied; a missing
or invalid token returns `403 console_shutdown_forbidden`.

### Targets

- `GET /api/v1/targets`
- `POST /api/v1/targets/discover`

Discovery is read-only. The Windows host is always represented as a target. The
Android Adapter may only invoke `adb devices -l` and maps each row to `ready`,
`offline`, `unauthorized`, or `unknown`.

Each Android Target preserves its opaque ADB serial as `external_id`, classifies
`connection_type` as `emulator`, `usb`, or `wireless`, and reports capabilities.
A ready target currently reports `android_adb`, `screen_capture`, `touch_input`,
and `ascii_text_input`; non-ready targets report no usable capabilities.
Discovery does not start/connect/pair a device, approve debugging, run shell
commands, or send input.

### Restricted direct executor (legacy/testing only)

- `POST /api/v1/executor/actions`

This compatibility surface transports exactly one explicitly structured Android
input through the configured ADB executor. It is not a Workflow/Run transition,
does not observe the resulting UI, and must never be reported as task success.
The request body is one of:

```json
{"target_id":"adb:serial","action":"tap","x":540,"y":1200}
```

```json
{"target_id":"adb:serial","action":"keyevent","keycode":"KEYCODE_BACK"}
```

```json
{"target_id":"adb:serial","action":"text","text":"ASCII text"}
```

The target must be the currently configured, ready Android target. Keycodes,
coordinates, and text are strictly validated; malformed requests return a
redacted `422 invalid_executor_action` response. `accepted` means only that
ADB accepted the atomic transport.

This endpoint is a legacy/testing surface, not a DeviceExecutionLease owner and
not a bypass around an active MobileTask, Chat, GameLearning, or future Runtime
Kernel device owner. A Kernel device-ownership cutover must reject it for every
Kernel-managed device.

### Application instances

- `POST /api/v1/application-instances`
- `GET /api/v1/application-instances?limit=100`
- `GET /api/v1/application-instances/{instance_id}`
- `POST /api/v1/application-instances/{instance_id}/commands`

This is the generic long-lived application-cycle Interface. It is independent
from MobileTask, Chat, GameLearning, and legacy Run records. Create body:

```json
{
  "profile_id": "soul-reply-v1",
  "client_request_id": "application-start-unique-id",
  "target_id": null,
  "initial_input": "可选的长期交流要求"
}
```

Command body is one of:

```json
{
  "command": "Input",
  "client_request_id": "application-input-unique-id",
  "content": "中途补充的要求"
}
```

```json
{
  "command": "Pause",
  "client_request_id": "application-pause-unique-id"
}
```

`command` is exactly `Input`, `Pause`, `Resume`, or `Stop`. Only `Input`
requires and accepts non-blank `content`. Create and command return
`202 Accepted` with the current durable instance projection. The accepted
request may still be `queued`, `waiting`, `paused`, or `stopping`; it is not a
claim that an application action or Outcome completed.

Instance status is one of `queued`, `running`, `waiting`, `paused`, `stopping`,
`stopped`, `completed`, or `failed`. `stopped`, `completed`, and `failed` are
terminal. Outcome status is `confirmed_success`, `confirmed_failure`,
`unconfirmed`, or `uncertain`; an Outcome also reports whether it is terminal.
`confirmed_success` means the Profile's Verifier accepted owner evidence for
that cycle, not that a later application or relationship goal was achieved.

The public response is an allow-list of lifecycle facts:

```json
{
  "id": "application-instance-id",
  "profile_id": "soul-reply-v1",
  "status": "waiting",
  "revision": 2,
  "degraded": false,
  "hard_risk": false,
  "error_code": null,
  "memory_version": 0,
  "input_count": 1,
  "intent_count": 1,
  "outcome_count": 1,
  "event_count": 4,
  "intents": [
    {
      "id": "intent-opaque-id",
      "cycle": 1,
      "revision": 2,
      "phase": "finalized",
      "hard_risk": false,
      "created_at": "2026-08-10T01:00:00Z",
      "finalized_at": "2026-08-10T01:00:03Z"
    }
  ],
  "outcomes": [
    {
      "cycle": 1,
      "status": "confirmed_success",
      "hard_risk": false,
      "terminal": false,
      "created_at": "2026-08-10T01:00:03Z"
    }
  ],
  "created_at": "2026-08-10T00:59:59Z",
  "updated_at": "2026-08-10T01:00:03Z",
  "finished_at": null,
  "wake_at": "2026-08-10T01:01:03Z"
}
```

It never returns `target_id`, `initial_input`, Input content, observations,
conversation identity, message bodies, reply drafts, screenshots, prompt text,
owner reservation/receipt detail, raw evidence, or event payloads. List accepts
`limit=1..500`, defaults to `100`, and returns `{ "items": [], "count": 0 }`.
List and inspect use the read-only archive even when execution dependencies are
offline; writes then return a stable `503` rather than accepting deferred work
for an absent runtime.

Request IDs are durable, global to the ApplicationRuntime database, and bound
to normalized operation and payload. An exact retry returns the same owning
instance at its current state. Reuse for another instance, command, or body
returns `409 application_runtime_request_id_conflict`. Stable error mappings
also include `404 application_runtime_not_found`,
`429 application_runtime_queue_full`, `503 application_runtime_closed`, and
redacted `409`/`503` rejected-or-unavailable operation codes.

ApplicationRuntime persists every physical intent before owner reserve and
dispatch. `Input`, `Pause`, and `Stop` share the last dispatch fence with the
owner operation. Recovery of an `open`, reserved, active-dispatch, dispatched,
or otherwise unresolved intent calls only the Profile's inspect/reconcile path.
An owner `active_dispatch` produces a durable interruptible wait whose next
operation is another owner GET/inspect only; that path cannot reserve, dispatch,
or generate a replacement draft. When the original dispatch receipt is
`active_dispatch`, the first after-observation GET is always a defer trigger,
even if that GET already reports confirmed, terminal, or uncertain state; the
second and any later GET reconcile the authoritative settlement. Without
credible proof of confirmed delivery or definite non-dispatch, the cycle
remains `uncertain` or closes as `recovery_no_replay`.

### Soul `soul-reply-v1` Profile and matcher lifecycle

Soul is one ApplicationRuntime Profile, not the platform identity. AI-GAME owns
the long-lived reply cycle, local visual-fact extraction, transient cloud reply
policy, revision fence, owner-proof verification, and delayed reply-strategy
learning. `F:\dating-copilot` remains the sole Soul device and physical-ledger
owner. AI-GAME calls only its loopback owner Interface; it does not import that
project, open its database, or issue competing Soul ADB input.

The owner captures one stable transcript/revision/PNG observation. The raw PNG
is consumed only by the loopback local visual Adapter and removed before cloud
or persistence handoff. The cloud provider receives transcript text,
identity-free structured visual facts, the current strategy/persona, and the
latest Application Input—not the screenshot. Reserve and dispatch bind the
same immutable draft and `conversation_revision`; owner preflight rechecks both
before at most one physical send. Active or uncertain operations are inspected,
never sent again.

The managed dating-copilot scheduler remains responsible for:

- matching throughout the day, with preferred hours changing cadence rather
  than becoming a hard off-window stop;
- an immediate opener after a successful match;
- bounded unread discovery needed to surface reply work; and
- Planet outreach only after trustworthy same-day match quota exhaustion.

It does not draft or send ordinary replies. `soul-reply-v1` owns those replies
and their learning. A confirmed send stores hash-bound trial lineage and send
proof, but does not immediately promote strategy. Only later admissible inbound
engagement, or a complete delayed no-response result, may change strategy.

The browser controls the reply instance and matcher together only through
Application lifecycle commands. Each nonterminal Soul instance contributes a
demand to the one shared matcher target:

- `queued`, `running`, or `waiting` contributes `running`;
- `paused` contributes `paused`; a cold paused restore may have
  `desired_state=paused` and `effective_state=stopped` without briefly starting
  a worker;
- Input changes reply guidance but does not change the scheduler target;
- Stop first lets the Application core settle any in-flight owner operation.
  While core status is `stopping`, that instance contributes the target it had
  immediately before Stop. Only durable core `stopped` permits its Stop event
  to contribute `stopped`;
- `failed` or `completed` reply instances do not implicitly stop the all-day
  matcher; and
- process shutdown closes AI-GAME's activation retry, loaded runtime, and
  reconciler. It never sends an implicit scheduler `stopped` command.

The content-free singleton receipt in `soul-scheduler-lifecycle.db` stores
`requested_state`, effective durable `desired_state`, `source_instance_id`, a
hashed `transition_ref`, monotonic `generation`, and `updated_at`. The archive
scan covers every Soul instance rather than only the latest 500 records. The
aggregate selects `running` when any nonterminal instance contributes running;
otherwise it selects `paused` when any contributes paused. When no nonterminal
instance remains, the newest explicit lifecycle evidence is authoritative, but
an unsettled Stop is ignored: a failed/completed instance preserves its latest
explicit target and cannot revive an older stopped target. Only the absence of
a running/paused demand plus a Stop settled by the core selects `stopped`.
Monotonic generation and the hash-bound aggregate transition prevent stale
idempotent lifecycle replay from rolling that decision back.

One interruptible reconciler performs owner GET before any necessary idempotent
PUT. It rechecks both shutdown state and receipt generation after GET, and sends
no PUT when closed or when owner state already matches. A lost PUT response is
resolved by the next GET rather than compensation or blind repetition. Owner
outage uses bounded backoff; when dating-copilot returns or loses its in-memory
worker after a reverse restart, the durable target converges without creating a
new Application instance. This matcher reconciliation remains active even when
cloud reply or local-vision dependencies are temporarily unavailable. If reply
dependencies are unavailable during cold recovery, the reconciler may ask the
Application core to settle only a genuinely idle `stopping` instance. That
dependency-free compare-and-set never clears worker ownership or an unfinished
physical intent; such work stays `stopping` and retains its pre-Stop matcher
demand.

The owner scheduler Interface is:

```text
GET /api/application-owner/v1/soul/scheduler
PUT /api/application-owner/v1/soul/scheduler
```

GET returns exactly `contract_version`, `controller_ref`, `desired_state`,
`effective_state`, `reply_owner`, and `scheduler_mode`. PUT body is exactly:

```json
{
  "contract_version": "v1",
  "desired_state": "running",
  "controller_ref": "ai-game-soul-reply-v1"
}
```

The public browser-safe scheduler read is:

```text
GET /api/v1/application-profiles/soul-reply-v1/scheduler
```

Example response:

```json
{
  "profile_id": "soul-reply-v1",
  "state": "running",
  "desired_state": "running",
  "effective_state": "running",
  "controller_matches": true,
  "code": "scheduler_running",
  "observed_at": "2026-08-10T01:00:00Z"
}
```

`state` is `running`, `paused`, `stopped`, or `degraded`; `effective_state` is
`running`, `paused`, `stopping`, or `stopped`. A cold pause uses
`state=paused/code=scheduler_paused_cold`. Stop drain, controller/reply-owner/
mode mismatch, or desired/effective mismatch uses `degraded` with a stable code.
The public response deliberately omits `controller_ref`, `reply_owner`,
`scheduler_mode`, identity, messages, and drafts. It is read-only; the browser
does not expose a second scheduler write control.

There is no observation DELETE/abandon endpoint. Before reserve, a fresh owner
observation may atomically replace only the process-local unreserved scope;
the old scope then returns `observation_scope_stale`. A reserved or active
pipeline is never replaced. `foreground_action_owned` is a retryable wait and
never authorizes dispatch of an old draft.

A direct `legacy_scheduler_active` or
`soul_execution_runtime_unavailable` dispatch reply is definite-not-sent, but
still requires owner GET/inspect to reach `terminal_no_replay`. That settlement
closes the attempt as nonterminal `confirmed_failure` so a later cycle may take
a fresh observation and re-plan; it never retries the old intent or draft.

The remote owner ledger is authoritative for delivery. Failure to create the
local learning trial before reserve is retryable and occurs before any owner
material exists. After owner reserve or confirmed delivery, a SQLite, file, or
timeout failure while binding owner lineage or recording send proof leaves the
learning row pending for later inspection repair. It cannot downgrade delivery,
suppress the one core-fenced dispatch, or authorize another reserve/dispatch.

### Mobile tasks

- `POST /api/v1/tasks`
- `GET /api/v1/tasks?limit=100`
- `GET /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/inputs`
- `POST /api/v1/tasks/{task_id}/stop`

This is the primary generic local-phone Operator Interface. It does not
automatically route text into Chat, GameLearning, or Soul. The create body is:

```json
{
  "goal": "Open the app and verify the current account status",
  "client_request_id": "mobile-task-create-unique-id",
  "target_id": null,
  "skill_id": null
}
```

Create and both command POSTs return `202 Accepted` with the complete current
TaskState. `GET /tasks/{task_id}` is the inspect operation; there is no separate
`/inspect` suffix. List accepts `limit=1..500`, default `100`. List and inspect
use the read-only `MobileTaskArchive` and remain available against
`mobile-tasks.db` when the active runtime was not composed. Create, input, and
stop then return `503 mobile_task_runtime_not_configured`; the API does not
accept deferred writes for an absent worker.

When `target_id` names a ready discovered Android Target, the MobileTask Adapter
derives an executor for that Target's serial while retaining the configured ADB
executable. Emulator, USB, and wireless serials are supported. The chosen
serial remains fixed for the whole TaskSession and lease. If `target_id` is
omitted, the configured default serial is used; it is a fallback rather than a
MuMu-only or loopback-only restriction. MobileTask can be composed without that
fallback: an API caller that omits `target_id` may receive durable `202 queued`
and later observe `executor_not_configured`, while the primary UI requires a
ready selected Target. Ready discovery reports Android screen-capture,
touch-input, and ASCII-text capabilities; discovery or successful transport
alone is not task acceptance evidence.

Task status is one of `queued`, `planning`, `running`, `stopping`, `completed`,
`failed`, `stopped`, or `uncertain`. The public TaskState contains the owner
goal, target/skill identifiers, input revision, current revisioned TaskPlan and
ordered Subgoals, active strategy/index, bounded-progress counters, stop and
verification state, inputs, ActionAttempts, Reflections, Events, timestamps,
and optional error/detail and SkillMemory version. `completed`, `failed`,
`stopped`, and `uncertain` are terminal. A Subgoal is `pending`, `active`, or
`completed`. Public `skill_id` is the optional caller value; the internal
schema-v2 `skill_scope_id` is deliberately not projected. When `skill_id` is
omitted, the production resolver may select a versioned automatic scope from
the normalized goal, so a completed Task can report a positive
`skill_memory_version` while `skill_id` remains null. Soul objectives receive no
MobileTask scope because the `soul-reply-v1` Application Profile owns a separate
delayed-outcome reply-learning store; dating-copilot remains the sole device and
physical-ledger owner.

Mid-task input uses the plural `/inputs` route:

```json
{
  "content": "Also confirm whether notifications are enabled",
  "client_request_id": "mobile-task-input-unique-id"
}
```

The initial goal starts at `input_revision=0`; accepted follow-up inputs begin
at revision 1. Their lifecycle is only `accepted` or `applied`. `accepted`
means durably appended and revisioned; `applied` means a plan, decision/attempt,
or Reflection for that revision was durably recorded. It does not mean a
physical action or success. This lifecycle must not be confused with Chat
Message `queued`/`applied`/`rejected`; a MobileTask input may remain `accepted`
when a terminal condition wins before a later decision.

Stop accepts only a content-bound request ID:

```json
{
  "client_request_id": "mobile-task-stop-unique-id"
}
```

An unclaimed queued Task can become `stopped` immediately; a claimed Task first
becomes `stopping`. Stop and input are serialized with the final physical-
dispatch seam. If either wins before dispatch, the persisted intent is
finalized `not_sent`. An atomic action already inside that seam cannot be
recalled, but its stale result cannot advance the Subgoal or promote
SkillMemory. Transport or verifier uncertainty takes precedence and terminates
the Task as `uncertain`.

The Module uses one internal coordinator for a bounded task queue and holds the
shared, process-local `DeviceExecutionLease` for the whole TaskSession. The same
local GUI-Owl-compatible endpoint is called sequentially as Planner, Executor,
BEFORE summarizer, AFTER summarizer, zero-image Verifier, and Reflection. Every
physical decision is stored as an open intent before at most one ADB action. An
accepted physical action settles for one second before the fresh AFTER capture;
that delay is not application-idle or success proof. Verification uses two single-image summary
requests followed by a zero-image decision: the BEFORE and AFTER summarizers
each see only their own fresh frame, while the Verifier receives both bounded
text summaries, the AFTER obstruction signal, and the local exact-frame result.
No request contains both images. If dimensions and PNG bytes match exactly, the
Adapter forces a claimed `progress=true` to `false` only when the Verifier did
not establish satisfaction. A static final state that visible facts already
prove may remain `satisfied=true / progress=true` even when the frames match;
exact identity is not failure evidence. Visible AFTER obstruction still
prevents satisfaction, and changed bytes still require visible-evidence
judgment. Transport acceptance never satisfies a Subgoal.
Three consecutive no-progress verifications cause Reflection. The
production limits of 2,048 ActionAttempts and 64 Reflections are final runaway
guards, not a substitute for the three-no-progress decision boundary.

The next Executor prompt receives at most eight recent redacted action
fingerprints and their results. Tap/long-press retains only a coarse 4x4 screen
region, swipe only direction, text is always `text(redacted)`, and allowed
keyevents retain only their key identity. Exact coordinates and typed content
remain in the trusted local journal, not in this prompt history or HTTP attempt
projection.

Recovery gives an open physical `act` intent first precedence: the Task becomes
terminal `uncertain` with `restart_open_intent` and is never replayed. Otherwise
an accepted stop becomes `stopped`; only another safe active checkpoint is
requeued. Task request IDs are durable,
global to the MobileTask database, and bound to operation and payload: an exact
retry returns the owning Task, while reuse for another operation, task, or body
returns `409 mobile_task_request_id_conflict`.

Orderly process shutdown closes the mutating runtime Interface, fences new
dispatch, and lets an already-dispatched action finish its settle, fresh AFTER
capture, Verification, and durable settlement before the worker exits. A safe
unfinished Task is left `queued` for recovery; a persisted but undispatched
intent is finalized `not_sent`. A shutdown timeout is reported rather than
pretending quiescence. Reads through `MobileTaskArchive` remain available.

The HTTP ActionAttempt omits intent arguments, coordinates/typed text,
BEFORE/AFTER observations and evidence IDs, transport receipt ID/detail, and
internal state-effect arbitration. Events omit their payload. Owner goal and
input content, follow-up input request IDs, plan/strategy/Reflection text,
verifier evidence, and sanitized detail remain visible. Create and stop request
IDs stay internal. Complete schemas, validation constraints,
stable errors, redaction, storage, and recovery rules are normative in
[`mobile-task-v1.md`](mobile-task-v1.md); the deeper Implementation design is in
[`../docs/mobile-task-runtime.md`](../docs/mobile-task-runtime.md).

`mobile-tasks.db` uses its own schema v2; migration namespaces prior explicit
SkillMemory/task keys under `legacy:` and adds the internal scope without model
or device work. Raw PNG/size evidence lives in
`runtime/sessions/mobile-tasks/evidence/`. On each new record the evidence-store
Implementation best-effort removes incomplete/expired pairs and trims oldest
pairs to the default global bounds of 256 frames, 1 GiB, and 7 days, always
retaining the newly written pair. These are therefore best-effort rather than
absolute bounds for that newest pair. There is no background cleanup timer and
no HTTP evidence-download route; durable SQLite history can outlive raw frames.

### Chat sessions and turns (compatibility)

These routes and persisted histories remain supported, but Chat is not a
first-level workspace in the simplified primary information architecture and is
not selected automatically by the MobileTask route.

- `GET /api/v1/chat/sessions`
- `POST /api/v1/chat/sessions`
- `GET /api/v1/chat/sessions/{session_id}`
- `POST /api/v1/chat/sessions/{session_id}/turns`
- `POST /api/v1/chat/turns/{turn_id}/cancel`

Create a local-only session:

```json
{
  "title": "Ask the local model",
  "mode": "local_chat",
  "target_id": null,
  "auto_execute": false
}
```

Create a cloud-dialogue/local-execution session:

```json
{
  "title": "Operate MuMu",
  "mode": "cloud_execute",
  "target_id": "adb-<persisted-target-id>",
  "auto_execute": true
}
```

The mode is immutable for the lifetime of a session. `cloud_execute` requires a
separately configured cloud endpoint, model and API key from the active runtime
cloud configuration, plus a ready Android target when `auto_execute=true`.

Submit a turn:

```json
{
  "content": "Open the current game's event page",
  "client_request_id": "one-client-generated-unique-id"
}
```

The Session-scoped submission route always returns `202 Accepted` with a
`ChatTurn` after durable acceptance. Its behavior is atomic:

- if the Session is idle, it creates one Turn at `input_revision=1`, stores the
  user Message as `queued`, and schedules one background worker;
- if its current Turn is `accepted`, `queued`, `thinking`, `planning`, or
  `executing`, it appends the user Message to that same Turn, increments
  `ChatTurn.input_revision`, and schedules no second worker;
- if the current Turn is `stopping`, it rejects the request with
  `409 chat_session_busy`.

Worker and pending-queue concurrency remain configured resource bounds for new
Turns, but joining a running Turn does not consume another worker slot. The
production chat device loop itself has no fixed action-count limit. The same
`client_request_id` and same content return the same owning Turn; reuse with
different content returns `409 client_request_id_conflict`.

`GET /api/v1/chat/sessions/{session_id}` returns one snapshot:

```json
{
  "session": {},
  "messages": [],
  "turns": [],
  "steps": []
}
```

The browser polls this snapshot in v1. Turn status is one of `accepted`,
`queued`, `thinking`, `planning`, `executing`, `awaiting_user`, `stopping`,
`completed`, `failed`, or `cancelled`. `reply_status` and `execution_status`
remain separate so a successful cloud reply cannot hide a failed or cancelled
device operation. `awaiting_user` remains in the v1 schema for persisted legacy
records and Adapter compatibility; the current production chat loop does not
enter it because of page-content classification.

`ChatTurn.input_revision` is required in both submission and transcript
responses. Each `ChatMessage` also exposes nullable `client_request_id`,
`content_sha256`, `input_revision`, `delivery_status`, and `applied_at`. User
Messages populate the request, hash, revision, and delivery state;
`applied_at` remains null until application. All five fields are null for
assistant or system Messages. For user Messages:

- `queued`: durably stored, but not yet read into a model-decision snapshot;
- `applied`: included in such a snapshot, with `applied_at` recording that
  transition; it does not assert a reply, action, transport, or success;
- `rejected`: never read because stop, cancellation, failure, shutdown, or
  restart finalized the Turn first.

Before each provider call, the worker atomically claims queued input and binds
the call to that `input_revision`. The reply is persisted only if the revision
is still current. If a later Message wins the race, the stale provider output is
discarded and the same worker takes a new snapshot and replans.

Cloud planners must return a JSON object with this normalized shape:

```json
{
  "assistant_text": "I will open the event page.",
  "execution_goal": {
    "goal": "Open the event page in the current Android app",
    "exact_text": null
  }
}
```

The cloud provider never returns ADB commands or screen coordinates. Only the
local GUI model receives a fresh screenshot and produces one official
`mobile_use` tool call. Timeline steps distinguish current observation,
proposal, transport acceptance, post-action observation, wait, redirection and
termination. A legacy `interact` response produces `redirected` and a fresh
replan rather than `awaiting_user`; persisted older records may still contain a
pause step.

During Android execution, applied Messages after revision 1 are cumulative goal
updates in revision order. GUI proposals are revision-checked after model output
and immediately before dispatch, and `terminate` is checked before it can end
the run. A stale proposal or termination is discarded and the same worker
replans from a fresh observation. An update accepted after the final
pre-dispatch check affects the next decision; it cannot recall an atomic input
already dispatched to ADB.

All recognized Android actions auto-run without per-step approval or a
content-based hard stop. Credentials/passwords, OTP/biometrics, identity
verification, payment, CAPTCHA, permission authorization, legal confirmation
and ambiguous pages do not automatically pause the current test-mode loop. The
loop has no fixed action-count limit and ends only after explicit user
cancellation, a device/ADB or model failure, or the local model's `terminate`
action. This is an intentionally open testing contract, not a safety guarantee
for consequential real-world flows.

Cancellation prevents subsequent actions after the cancellation check and marks
any still-queued user Messages `rejected`. It cannot undo an atomic input that
ADB already accepted. Failure, shutdown, and restart likewise reject unread
Messages, and a process restart never replays an interrupted physical action.

### Events and runtime

- `GET /api/v1/events?limit=100&run_id=<optional>`
- `GET /api/v1/runtime`
- `GET /health`

JSON field names are `snake_case`; timestamps are UTC ISO-8601 strings.

### Cloud model settings

- `GET /api/v1/settings/cloud`
- `POST /api/v1/settings/cloud`
- `POST /api/v1/settings/cloud/test`
- `POST /api/v1/settings/cloud/clear`

The read response is deliberately redacted:

```json
{
  "endpoint": "https://provider.example/v1",
  "model": "compatible-model",
  "has_api_key": true,
  "configured": true,
  "credential_source": "console",
  "status": "unknown",
  "detail": "配置已保存；首次发送或连接测试时验证。",
  "revision": 1,
  "updated_at": "2026-01-01T00:00:00Z"
}
```

It never includes `api_key`. Save accepts an endpoint, model, optional new key,
and the last observed revision:

```json
{
  "endpoint": "https://provider.example/v1",
  "model": "compatible-model",
  "api_key": "<operator-supplied secret>",
  "expected_revision": 0
}
```

The placeholder above is not a usable credential. The first console save needs
a key; later endpoint/model changes may omit it to retain the protected key.
The backend protects the credential with Windows DPAPI for the current Windows
user before persistence. Validation errors, Settings responses, messages,
events and provider errors must not reflect it. A stale `expected_revision`
returns a conflict instead of overwriting another page's change.

Save and clear install a new runtime provider generation for subsequent cloud
requests without a process restart. Work already executing is not replayed or
retrospectively changed. `config/cloud-runtime.env` and a process-environment
key are startup bootstrap inputs only when there is no console-managed record;
the environment file itself remains non-secret.

The connection-test endpoint makes one explicit model request and returns only
sanitized status, detail and optional `latency_ms`; it may incur provider cost.
`GET /api/v1/runtime` itself does not make a billable probe. Clear requires
`{ "expected_revision": <last-observed revision> }`, removes the active cloud
route, and leaves `local_chat` available.

## Legacy compatibility appendix

These routes remain because persisted older clients and records may still read
them. They are not first-level browser workspaces, are not claimed by the
MobileTask or ApplicationRuntime coordinators, and must not be used as the seam
for a new Adapter. There is no universal Run/router across these resources.

### Workflows

- `GET /api/v1/workflows`

The catalog contains generic Windows, generic Android, and a disabled
traditional Soul entry that points clients toward the typed Soul workspace. It
does not create or execute a legacy Run.

### Runs

- `GET /api/v1/runs`
- `POST /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `POST /api/v1/runs/{run_id}/actions`

These routes persist compatibility saved-work records; they are not the generic
MobileTask or ApplicationRuntime Interface. Create body:

```json
{
  "name": "Open the report",
  "workflow_id": "generic-windows",
  "target_id": "windows-local",
  "instruction": "Open the weekly report and inspect the first page",
  "exact_text": null,
  "requires_approval": false
}
```

`exact_text` is immutable. Run actions use `{ "action": "pause" }`,
`{ "action": "resume" }`, or `{ "action": "cancel" }`; invalid transitions
conflict. No current device worker automatically claims a queued legacy Run.

### Approvals

- `GET /api/v1/approvals`
- `POST /api/v1/approvals/{approval_id}/decision`

Decision body is `{ "decision": "approved", "note": "..." }` or
`{ "decision": "rejected", "note": "..." }`. Approval is scoped to one legacy
Run and has no bulk operation. MobileTask and ApplicationRuntime have no
per-action Approval resource.

### Old Soul diagnostics and disabled command bridge

- `GET /api/v1/integrations/soul`
- `GET /api/v1/integrations/soul/conversations/{conversation_id}`
- `POST /api/v1/integrations/soul/commands`
- `POST /api/v1/soul/commands`

The two GET routes retain normalized, read-only compatibility diagnostics. The
workspace separates upstream reachability from its last application snapshot;
an optional section failure does not erase other readable sections. The
conversation route exposes its legacy normalized match summary and requested
transcript. Neither GET is the current Soul Application or scheduler source of
truth.

Both POST routes always return HTTP `410` with stable code
`legacy_soul_write_disabled`. The former `start/pause/resume/stop`, mode, and
inventory command bodies and their receipt ledger are not a supported write
path. Current code must use `/api/v1/application-instances` and Profile
`soul-reply-v1`.

### Legacy Run state machine

```text
create without approval -> queued [workflow_executor_not_connected]
create with approval    -> awaiting_approval
awaiting_approval --approve--> queued [workflow_executor_not_connected]
awaiting_approval --reject---> cancelled
queued --pause----------> paused
paused --resume---------> queued [workflow_executor_not_connected]
queued|paused|awaiting_approval --cancel--> cancelled
```

`running`, `completed`, and verified outcome states are reserved for a future
executor Adapter. This console does not synthesize them.

`workflow_executor_not_connected` belongs only to this traditional saved-run
state machine. `executor_not_configured` is reserved for a missing live GUI/ADB
executor configuration and is not used as a queued-run blocker.
