# Mobile Task HTTP contract v1

## Scope and transport

This contract defines the generic `MobileTask` Interface exposed by the local
AI-GAME control plane. It is an Android task contract, not a Chat, Game
Learning, Soul, Workflow, Run, or Approval contract.

All routes are loopback-only JSON routes below `/api/v1`. Every `POST` requires:

```http
X-AI-Game-Client: console-v1
Content-Type: application/json
```

Missing or incorrect write-client identity returns `403
console_client_required`. Successful writes return `202 Accepted` after the
request is durably accepted; model and device work continues asynchronously.

## Resources

```text
POST /api/v1/tasks
GET  /api/v1/tasks?limit=100
GET  /api/v1/tasks/{task_id}
POST /api/v1/tasks/{task_id}/inputs
POST /api/v1/tasks/{task_id}/stop
```

`GET /tasks` accepts `limit` from 1 through 500 and defaults to 100. It returns
newest tasks first:

```json
{
  "items": [],
  "count": 0
}
```

`GET /tasks/{task_id}` is the inspect route and returns one full projected
TaskState. Reads use a read-only `MobileTaskArchive`; they remain available from
`mobile-tasks.db` when the active runtime cannot be composed and do not call
GUI-Owl, ADB, or the verifier.

## Create

`POST /api/v1/tasks` accepts:

```json
{
  "goal": "Open the current game's daily page and claim the available reward",
  "client_request_id": "task-start-20260810-001",
  "target_id": "adb-persisted-target-id",
  "skill_id": null
}
```

Validation:

- `goal`: required non-blank string, at most 10,000 characters;
- `client_request_id`: required inert identifier, at most 128 characters;
- `target_id`: optional identifier, at most 256 characters;
- `skill_id`: optional identifier, at most 256 characters.

Whitespace around these values is normalized. `target_id=null` means the
production Adapter attempts to bind the task to the currently configured
Android serial. MobileTask can be composed without that fallback serial, so a
compatibility caller that omits `target_id` may receive durable `202 queued` and
later observe terminal `executor_not_configured`. The primary UI requires a
ready selected Target and therefore does not use that ambiguous path.
When `target_id` is supplied, it must identify a ready discovered Android
Target. Discovery reports `emulator`, `usb`, or `wireless` connection type and
capabilities including `android_adb`, `screen_capture`, `touch_input`, and
`ascii_text_input`. The Adapter retains the configured ADB executable but binds
a target-specific executor to the selected Target's serial. That binding is
fixed for the TaskSession; it is never silently redirected to a different
device. The configured serial remains a fallback, not a restriction to MuMu or
loopback-only emulator serials.

The primary one-sentence UI omits `skill_id`. Production derives a stable,
internal automatic skill scope from the normalized goal; Soul is excluded,
率土之滨 uses target-independent task-kind scopes, and other goals use a
redacted exact-goal hash. Advanced callers may still provide `skill_id`, which
is isolated in a legacy namespace. The internal scope is not exposed by v1 and
does not change the request or response schema.

Acceptance means the task is durable and queued. It does not prove that the
target, model, device action, Subgoal, or whole goal is ready or successful.

## Inspectable TaskState

Every create, input, stop, and inspect response uses this shape:

```json
{
  "id": "8bcdff73-9fcb-4c55-9634-a77a5fc28975",
  "goal": "Open the current game's daily page and claim the available reward",
  "target_id": "adb-persisted-target-id",
  "skill_id": null,
  "status": "running",
  "input_revision": 1,
  "plan": {
    "revision": 1,
    "subgoals": [
      {
        "index": 0,
        "description": "The daily page is visibly open",
        "status": "completed"
      },
      {
        "index": 1,
        "description": "The available reward is visibly claimed",
        "status": "active"
      }
    ]
  },
  "active_subgoal_index": 1,
  "strategy": "open the daily icon from the current home screen",
  "no_progress_count": 0,
  "reflection_count": 0,
  "attempt_count": 2,
  "cancel_requested": false,
  "verification_satisfied": false,
  "detail": null,
  "error_code": null,
  "skill_memory_version": 0,
  "inputs": [
    {
      "revision": 1,
      "content": "Dismiss the event notice first",
      "lifecycle": "applied",
      "client_request_id": "task-input-20260810-001",
      "created_at": "2026-08-10T00:00:02Z",
      "applied_at": "2026-08-10T00:00:03Z"
    }
  ],
  "attempts": [
    {
      "id": "attempt-opaque-id",
      "sequence": 1,
      "subgoal_index": 0,
      "action_type": "tap",
      "transport_status": "accepted",
      "verification": {
        "satisfied": true,
        "progress": true,
        "uncertain": false,
        "evidence": "The daily page title is visible in the after frame"
      },
      "created_at": "2026-08-10T00:00:00Z",
      "finalized_at": "2026-08-10T00:00:01Z"
    }
  ],
  "reflections": [],
  "events": [
    {
      "sequence": 1,
      "event_type": "task_accepted",
      "created_at": "2026-08-10T00:00:00Z"
    }
  ],
  "created_at": "2026-08-10T00:00:00Z",
  "updated_at": "2026-08-10T00:00:03Z",
  "finished_at": null
}
```

The initial owner `goal` is not a follow-up input, so a newly created task has
`input_revision=0` and an empty `inputs` list. Each accepted follow-up increments
the revision by one.

### Status

`status` is one of:

- `queued`: durably accepted and waiting for the sole internal coordinator;
- `planning`: claimed and creating the initial TaskPlan;
- `running`: planning is complete and the sequential task loop is active;
- `stopping`: stop is durable and the worker is approaching a safe terminal
  check;
- `completed`: every Subgoal was satisfied by fresh verification;
- `failed`: the task ended without verified completion and without unresolved
  physical uncertainty;
- `stopped`: the accepted stop took effect;
- `uncertain`: a physical intent may have crossed the device seam, but its
  durable outcome cannot be established safely.

Only the last four states are terminal. `verification_satisfied=true` is
reserved for verified whole-task completion. `transport_status=accepted` does
not set it.

### Plan and Subgoals

`plan=null` while no TaskPlan is committed. A plan has a monotonically
increasing `revision`; each Subgoal has an ordered zero-based index and status
`pending`, `active`, or `completed`. Reflection may replace the plan with a new
revision. An atomic action is never represented as a completed Subgoal without
fresh verification.

`active_subgoal_index` points at the current Subgoal. On a completed task it may
equal the number of plan Subgoals.

### Inputs

Input lifecycle is:

- `accepted`: durable but not yet incorporated into a model-decision snapshot;
- `applied`: incorporated at a planning, ActionAttempt, or Reflection decision
  seam; `applied_at` records the first such incorporation.

`applied` does not imply an action, transport, verification, or success.

### ActionAttempts

An ActionAttempt represents one proposed atomic decision and its outcome.
`action_type` is the safe action name or decision kind. `transport_status` is
nullable while the attempt is open, otherwise one of `not_sent`, `accepted`,
`rejected`, or `uncertain`.

Verification has three independent facts:

- `satisfied`: the fresh after observation proves the active Subgoal;
- `progress`: visible progress occurred, even if the Subgoal is not complete;
- `uncertain`: the admissible visual evidence cannot support a reliable result.

`satisfied=true` requires `progress=true` and cannot coexist with
`uncertain=true`.

### Reflection and SkillMemory

After three consecutive no-progress verifications, the runtime records a
Reflection. A Reflection exposes the previous and new strategy, short reason,
the triggering consecutive count, sequence, and timestamp. The current
Implementation requires a changed strategy, a replacement plan, or termination
and production uses 64 Reflections and 2,048 ActionAttempts only as final
runaway guards for long tasks.

`skill_memory_version` is zero unless this completed task promoted a new
version in its automatic or explicit internal skill scope. Promotion happens
only with verified completion. Because automatic `skill_scope_id` is hidden,
`skill_id` may be null while a completed response has a positive
`skill_memory_version`. The response does not expose SkillMemory contents.
SkillMemory is separate from Game Learning `PolicyMemory` and from model
weights.

## Add owner input

`POST /api/v1/tasks/{task_id}/inputs` accepts:

```json
{
  "content": "Dismiss the event notice first",
  "client_request_id": "task-input-20260810-001"
}
```

Both fields are required and non-blank. `content` is at most 10,000 characters;
`client_request_id` is at most 128 characters. A non-terminal task accepts the
input, increments `input_revision`, and returns the updated TaskState with
`202`.

The same task and worker continue. A model result based on an older revision is
discarded. The runtime checks the revision again immediately before physical
dispatch; if the new input won that fence, the old ActionAttempt is finalized
as `not_sent` and the next decision incorporates the new revision. Input that
arrives after dispatch cannot recall that atomic action and applies at the next
decision seam.

Input to a terminal task returns `409 mobile_task_state_conflict`.

## Stop

`POST /api/v1/tasks/{task_id}/stop` accepts:

```json
{
  "client_request_id": "task-stop-20260810-001"
}
```

A queued, unclaimed task becomes `stopped` immediately. A claimed task becomes
`stopping`; the worker prevents later work at the next safe check. Stop and
dispatch are serialized at the final device fence. If stop wins, the open
intent is finalized as `not_sent`; if dispatch already won, the atomic action
cannot be undone. Calling stop for an already terminal task is an idempotent
read of that task state when the request ID is new.

## Idempotency

Every create, input, and stop request requires a `client_request_id`. It is
global to the MobileTask store and bound to the normalized operation and body.

- same ID, same operation, same payload: return the original owning task;
- same ID with another operation, task, or payload: return `409
  mobile_task_request_id_conflict`;
- a retry never creates another Task, input revision, stop request, worker, or
  physical action.

## Execution and recovery invariants

- One internal coordinator worker consumes the bounded MobileTask queue; tasks
  execute sequentially.
- One task holds its process-local `DeviceExecutionLease` for its whole
  TaskSession, including planning, ActionAttempts, verification, and Reflection.
- Every physical intent is durably stored before transport.
- A stop/input revision fence runs immediately before the Adapter dispatches an
  atomic action.
- Every accepted non-wait physical action has a one-second settle delay before
  the fresh AFTER observation. A `wait` intent uses its requested 0–10 seconds.
  Settling is not verification or proof that the application is idle.
- Verification uses three sequential local-model requests: a BEFORE summarizer
  sees exactly the BEFORE image, an AFTER summarizer sees exactly the AFTER image
  and returns visible facts plus `goal_obstructed`, then a zero-image Verifier
  compares both text summaries and the local exact-frame flag. No request sees
  both images.
- The Adapter also compares local BEFORE/AFTER dimensions and PNG bytes. An
  exact match suppresses a claimed `progress=true` only when the Verifier has
  not established satisfaction. If the visible summaries prove an
  already-satisfied static final state, `satisfied=true / progress=true` remains
  valid even with identical frames; exact identity is not failure evidence.
  Visible AFTER obstruction still prevents satisfaction. This exact check is
  not a perceptual similarity claim, and changed bytes still need visible
  verifier evidence.
- Transport acceptance is not progress, Subgoal satisfaction, or task
  completion.
- Three consecutive no-progress results trigger Reflection rather than an
  unbounded identical retry loop.
- The next Executor sees at most eight recent attempts as redacted action
  fingerprints: taps/long presses use a 4×4 region, swipes use direction, and
  typed text is `text(redacted)`. Exact coordinates and content are not included
  in that prompt history.
- Production runaway guards are 2,048 ActionAttempts and 64 Reflections.
- Process `shutdown` rejects later writes, fences new dispatch, lets an already-
  dispatched action settle and persist its real AFTER/Verification, then leaves
  unfinished work `queued` at a safe checkpoint. This is not owner `stop`.
- Recovery applies fixed precedence. A restart that finds an open physical
  intent first sets terminal `uncertain`, uses
  `error_code=restart_open_intent`, records `replayed=false`, and never replays
  the action.
- Otherwise a recovered `stopping` task becomes `stopped`; only another safe
  active task with no open physical intent resumes.

## HTTP redaction

The HTTP projection deliberately differs from the internal SQLite record:

- attempts omit physical-intent arguments, typed text, raw model output,
  before/after evidence IDs, and screenshot bytes;
- events omit internal `data_json` and expose only sequence, type, and time;
- owner `goal` and input `content` remain visible because they are caller-owned
  task data;
- follow-up input `client_request_id` values are exposed with those inputs;
  create and stop request IDs remain internal;
- verification exposes only bounded evidence text, not frame bytes;
- API keys and credentials are never included.

Raw frames are local evidence under
`runtime/sessions/mobile-tasks/evidence/`. Durable TaskState and internal
ActionAttempt data are in schema-v2 `runtime/console/mobile-tasks.db`. The
database and evidence directory are sensitive local runtime data, not source
artifacts. On each new frame the evidence store performs best-effort global
pruning with defaults of 256 complete frame pairs, 1 GiB total, seven days, and
16 MiB per PNG. It removes incomplete pairs and retains the newly written pair,
so count/aggregate/age retention is best-effort rather than an absolute cap on
that newest pair. There is no background cleanup timer. SQLite task history can
outlive its raw frames.

The schema-v1 to schema-v2 migration adds the internal `skill_scope_id` and
namespaces existing explicit SkillMemory/task keys with `legacy:`. It does not
call a model, inspect a device, or replay an action.

## Errors

Errors use the control-plane envelope:

```json
{
  "error": {
    "code": "mobile_task_not_found",
    "message": "未找到该智能任务。"
  }
}
```

Stable MobileTask errors are:

| HTTP | code | meaning |
|---:|---|---|
| 403 | `console_client_required` | missing or incorrect write-client header |
| 404 | `mobile_task_not_found` | task ID does not exist |
| 409 | `mobile_task_request_id_conflict` | request ID was bound to different content or operation |
| 409 | `mobile_task_state_conflict` | current terminal or internal state rejects the requested operation |
| 409 | `mobile_task_runtime_closed` | a write reached a runtime whose shutdown has begun |
| 429 | `mobile_task_queue_full` | bounded queue cannot accept another task |
| 503 | `mobile_task_runtime_not_configured` | create/input/stop cannot run because the local GUI model or Android executor composition is unavailable |

Schema validation failures return `422 invalid_mobile_task_request` without
echoing the rejected field value. Execution failures accepted after
creation are represented in TaskState through terminal `status`, `detail`, and
`error_code`; they are not retroactively converted into an HTTP failure for the
already accepted create call. Examples include target mismatch, `target_busy`,
model response failure, attempt budget exhaustion, and transport uncertainty.

## Contract exclusions

This v1 contract does not claim that Mobile-Agent is installed, that legacy
Runs are executed, that Soul state is copied into MobileTask, or that GUI-Owl is
ready for continuous real-time games. It provides a low-frequency sequential
screenshot/action/verification loop only. It also does not define a universal
Run/router that turns arbitrary text into Chat, LearningJob, or Soul operations.
Support for emulator, USB, and Wi-Fi Target transport is not evidence that a
tablet was live-tested, nor that a Soul or game objective passed current
real-device acceptance. See
[`../docs/mobile-task-runtime.md`](../docs/mobile-task-runtime.md) for Module
ownership and [`../docs/gameplay-readiness.md`](../docs/gameplay-readiness.md)
for future gameplay evidence gates.
