# Generic Mobile Task Runtime

## Product scope

AI-GAME is a general, local phone Operator and generic `ApplicationRuntime`
platform. A `MobileTask` is one durable owner objective executed against one
Android target across many atomic GUI actions. Soul is the production
Application Profile `soul-reply-v1`; it does not define the product or the
generic task state machine. `dating-copilot` remains its sole device and
physical-ledger owner, while AI-GAME owns the application cycle.

`MobileTask`, `ChatTurn`, `LearningJob`, and the legacy control-plane `Run` are
different domain records. They are not aliases and are not flattened into a
universal status object:

- a `MobileTask` owns durable TaskState, planning, execution, verification,
  Reflection, and optional SkillMemory reuse;
- a `ChatTurn` owns dialogue and same-Turn input revision semantics;
- a `LearningJob` owns one bounded `LearningEpisode`, its `Transition` ledger,
  `Phase`, `Result`, `Outcome`, and `PolicyState`;
- a legacy `Run` is compatibility saved work and has no MobileTask executor.

Mobile-Agent informed the design, but Mobile-Agent is not installed as an
enclosing framework and is not required at runtime. The existing local
GUI-Owl and ADB execution core remains the Implementation.

`ApplicationRuntime` and `MobileTaskRuntime` are sibling deep Modules, not
aliases. `ApplicationRuntime` supplies the generic
Observation→Policy→ExecutionOwner→Verification cycle for a named Profile,
including durable application intents, revision/dispatch fencing, inspect-only
reconciliation and optional memory gating. `MobileTaskRuntime` remains the
specialized Module for arbitrary owner goals, TaskPlan/Subgoal planning,
Reflection and SkillMemory. A Soul sentence is not silently converted into a
MobileTask; its typed workspace starts `profile_id=soul-reply-v1` through
`/api/v1/application-instances`.

## Deep Module and Interface

`MobileTaskRuntime` is the deep Module. Its task Interface is deliberately
small, with a separate process-lifecycle hook:

```text
start(goal, client_request_id, target_id?, skill_id?) -> MobileTaskState
send(task_id, content, client_request_id)             -> MobileTaskState
stop(task_id, client_request_id)                      -> MobileTaskState
inspect(task_id)                                      -> MobileTaskState
list(limit=100)                                       -> list[MobileTaskState]
shutdown(timeout=5s)                                  -> None
```

The first five methods accept or inspect durable intent; `shutdown` quiesces
the coordinator and preserves safe unfinished work for restart. None of these
methods make the caller
coordinate screenshots, model roles, ADB actions, retries, verification,
Reflection, or memory promotion. This gives the Module Depth: a small Interface
controls a long-running stateful Implementation. It also gives Locality: action
ordering, uncertainty, restart recovery, and evidence rules live in one Module
instead of being repeated in the browser, Chat, games, and application
callers.

The loopback HTTP routes are an Adapter at this external seam. The browser and
tests use the same projected TaskState; they do not call internal role methods
or the device directly. A read-only `MobileTaskArchive` implements only
`inspect` and `list` over the same store, so history remains readable when the
active runtime cannot be composed and no worker/device Adapter is started.

The Implementation has four internal seam groups:

- `TaskDriver.open(...) -> TaskSession` binds one target and holds its
  `DeviceExecutionLease`; the production `MobileTaskAndroidDriver` is the ADB
  Adapter, can derive a target-bound executor for a discovered serial, and tests
  use an in-memory Adapter;
- `RoleModel` provides `plan`, `decide`, `verify`, and `reflect`; production uses
  one `OpenAICompatibleMobileRoleModel` backed by the configured local GUI-Owl
  endpoint, while tests use scripted Adapters;
- `SkillScopeResolver` derives a stable internal reuse scope when the ordinary
  caller omits `skill_id`;
- the SQLite task store and local evidence store persist state and frame bytes.
  They are internal Implementation details, not additions to the caller
  Interface.

These are real seams because production and test Adapters exist for each
varying dependency. `RoleModel` has four logical Interface methods, but the
production `verify` Adapter expands into BEFORE summary, AFTER summary, and
zero-image comparison calls. The names do not imply resident model processes or
concurrent Agents.

Target discovery is a read-only Adapter over `adb devices -l`. Ready Android
Targets may represent an emulator, USB device, or wireless device and currently
report `android_adb`, `screen_capture`, `touch_input`, and
`ascii_text_input` capabilities. A supplied `target_id` must resolve to a ready
Android Target; the driver creates a target-bound executor for that Target's
serial while retaining the configured ADB executable. Omitting `target_id`
uses the configured default serial as a compatibility fallback. MobileTask can
be composed without that fallback, so such an API request may first be durably
accepted and then fail asynchronously with `executor_not_configured`; the
primary UI requires a ready selected Target. Once the TaskSession opens, the
chosen target and lease key are fixed for that open session and cannot silently
move to another discovered device. Recovery resolves the persisted `target_id`
again when it opens a later session.

## Durable TaskState

TaskState is the inspectable source of task truth. It contains:

- immutable task identity, owner goal, target and optional `skill_id`;
- task status and terminal timestamps;
- monotonically increasing owner-input revision and input lifecycle;
- current TaskPlan revision, ordered Subgoals, and active Subgoal index;
- current strategy, no-progress count, Reflection count, and attempt count;
- stop intent and whether final task verification was satisfied;
- sanitized detail and stable error code;
- ActionAttempt, Reflection, and event projections;
- the SkillMemory version promoted by this task, if any.

Task status is one of:

```text
queued | planning | running | stopping |
completed | failed | stopped | uncertain
```

Only `completed`, `failed`, `stopped`, and `uncertain` are terminal.
`completed` means every Subgoal in the active TaskPlan was satisfied by a fresh
verification. It is never inferred from model prose, an ADB acceptance, or a
changed image alone.

A TaskPlan is a revisioned list of observable Subgoals. A Subgoal is
`pending`, `active`, or `completed`; a click is an ActionAttempt, not a
Subgoal. Reflection may replace the remaining plan and increment the plan
revision.

## Sequential Planner, Executor, evidence-summary, Verifier, and Reflection loop

The Module owns one internal coordinator worker. Accepted tasks enter a bounded
queue and are processed one at a time by that worker. The current production
composition uses queue capacity 32, at most 2,048 ActionAttempts per task, and
at most 64 Reflections. These are runaway guards for long tasks, not a promise
of real-time throughput. Three consecutive no-progress results still trigger a
Reflection immediately; the larger limits do not authorize blind repetition.

One task follows this sequence:

```text
claim durable TaskState
-> open one target-bound TaskSession and acquire its session-wide device lease
-> capture a fresh planning observation
-> Planner creates an observable, revisioned TaskPlan
-> capture a fresh BEFORE observation for the active Subgoal
-> Executor proposes exactly one atomic PhysicalIntent, finish, or terminate
-> durably record the ActionAttempt intent
-> re-check stop and owner-input revision immediately before device dispatch
-> ADB Adapter transports at most one accepted atomic action
-> after an accepted physical input, wait the configured 1-second settle delay
-> capture a fresh AFTER observation
-> summarize visible facts from the single BEFORE image
-> summarize visible facts and obstruction from the single AFTER image
-> Verifier receives both text summaries and exact-frame state, with no image
-> advance only on verified satisfaction; otherwise continue or Reflect
-> close the TaskSession and release the lease at terminal state or safe shutdown suspension
```

Planner, Executor, BEFORE/AFTER summarizers, Verifier, and Reflection use the
same configured local GUI-Owl endpoint under different role prompts. Calls are
sequential. The Planner returns observable Subgoals rather than coordinates.
The Executor alone proposes a typed physical intent. Verification is a
three-call Adapter sequence: the BEFORE and AFTER summarizers each receive only
their own fresh single image and return bounded visible facts; AFTER additionally
returns `goal_obstructed`. The final Verifier receives no image and compares both
text summaries, that obstruction signal, and a local exact-frame-match flag. No
model request receives both images.

The Adapter also loads both local PNGs and compares dimensions and bytes. When
they are exactly identical and the Verifier did not establish satisfaction, a
claimed `progress=true` is deterministically overridden to `false`. Exact
identity is not itself failure evidence: when the summaries prove an
already-satisfied static final state, `satisfied=true / progress=true` is
preserved. Visible AFTER obstruction separately prevents satisfaction. This is
an exact byte check, not perceptual equivalence; non-identical frames still
require the Verifier's visible-evidence judgment.

An accepted non-wait physical action sleeps for one second before the fresh
AFTER capture. A `wait` intent instead sleeps its validated requested duration
from 0 through 10 seconds. Settling gives the UI time to change; it is not an
idle detector, transport proof, or application-success claim.

To reduce blind repetition without leaking typed content or exact coordinates,
the next Executor receives at most eight recent attempts as redacted action
fingerprints with transport and verification result. Tap and long-press
coordinates become one of 16 coarse `r{row}c{column}` regions, a swipe becomes
only its direction, text becomes `text(redacted)`, and wait/keyevent retain only
their safe action identity. Raw PhysicalIntent arguments remain in local SQLite
for recovery truth but are neither placed in this prompt history nor projected
over HTTP.

Before physical execution, the complete intent is persisted as an open
ActionAttempt. A last dispatch fence then checks both `cancel_requested` and the
input revision. If stop or newer input won the race, the attempt is finalized
as `not_sent`; a stale proposal is discarded and the worker makes a new model
decision. Once the dispatch side wins that fence, a later input or stop cannot
recall the already-dispatched atomic action and applies at the next decision
boundary.

Three consecutive verifications with no visible progress trigger persisted
Reflection. Reflection must change strategy, replace the plan, or terminate;
an unchanged retry is rejected as `reflection_no_strategy_change`. Progress
resets the consecutive no-progress counter. Exhausting the Reflection or
ActionAttempt budget ends the task as `failed`, never as verified completion.

## Owner input, stop, and idempotency

`send` appends a revisioned owner instruction to any non-terminal task. Input
lifecycle is:

- `accepted`: durably stored but not yet incorporated at a model-decision seam;
- `applied`: incorporated into a plan, ActionAttempt, or Reflection snapshot,
  with `applied_at` set once.

New input increments `input_revision`. Plans, executor decisions, and
Reflections use compare-and-set fences against that revision so stale model
output cannot cross the device seam. The task keeps one worker; a follow-up
does not create another task or another worker.

`stop` is cooperative and idempotent. A task not yet claimed becomes
`stopped` immediately. A claimed task becomes `stopping`; the worker finalizes
it at the next safe check. If stop wins the pre-dispatch fence, the open intent
is recorded as not sent. If an atomic transport already won the fence, stop
prevents later work but cannot undo that physical input.

`shutdown(timeout=5)` is process lifecycle, not an owner stop. It closes the
write Interface, signals the sole coordinator, serializes with the dispatch
seam, and joins the worker. Queued or planning work is released as `queued` at a
safe checkpoint. An intent persisted but not dispatched is finalized
`not_sent` with non-uncertain shutdown evidence. If an action already crossed
the seam, the worker finishes its settle, fresh AFTER observation, Verification,
and durable attempt settlement before exiting; that already-current result may
advance TaskState, but no new action begins afterward. A timeout raises rather
than pretending shutdown completed, and a later call can finish the same
shutdown. `inspect`/`list` remain reads, while subsequent `start`/`send`/`stop`
raise `mobile_task_runtime_closed`. FastAPI invokes this hook during lifespan
teardown before shutting down the compatibility GameLearning and Chat
coordinators.

Every state-changing Interface call has a caller-supplied
`client_request_id`. Request IDs are global to the MobileTask store and bound
to the operation plus normalized payload. An exact retry returns the owning
task; reuse for another operation or payload returns
`mobile_task_request_id_conflict`.

## Queueing and DeviceExecutionLease

There is one MobileTask coordinator worker, not one worker per task. This keeps
model and device use sequential even when several tasks are durably queued.
Queue admission is bounded and fails with `mobile_task_queue_full` instead of
silently losing work.

The production `TaskSession` acquires the process-local
`DeviceExecutionLease` before its first observation and holds it through
planning, every ActionAttempt, verification, and Reflection until that session
closes. In an uninterrupted run this is the task's whole active execution; an
orderly shutdown may close it at a safe nonterminal checkpoint and recovery later
opens and leases a new session. The same lease instance is shared with ordinary
Android Chat and Game Learning Adapters; another in-process consumer of that
serial receives `target_busy` before using the device or model.

The lease does not coordinate another AI-GAME process, direct ADB commands, or
the external `dating-copilot` controller. Soul remains an ApplicationRuntime
Profile rather than a MobileTask hidden behind this lease. AI-GAME never opens
a competing Soul ADB Adapter; all Soul physical work crosses the dating-copilot
owner Interface.

## Persistence, evidence, and restart recovery

Production state is split by ownership:

```text
runtime/console/mobile-tasks.db
    schema v2 TaskState, request deduplication, plans, inputs,
    ActionAttempts, Reflections, events, internal skill scopes,
    and versioned SkillMemory metadata

runtime/sessions/mobile-tasks/evidence/
    opaque-id PNG observations plus dimensions metadata
```

User text is never used in evidence filenames. SQLite stores only opaque
evidence identifiers and bounded summaries in observations; the raw local
frames remain under the evidence directory. The local database is sensitive:
it contains owner goals and inputs and may contain internal physical-intent
arguments, including exact text intended for the device.

The evidence store enforces best-effort global retention whenever a new frame
is recorded. It prunes toward defaults of 256 complete PNG/JSON pairs, 1 GiB
total, and seven days; each PNG is independently capped at 16 MiB. It removes
incomplete orphan pairs and trims the oldest evidence while always retaining the
just-recorded pair, so count/aggregate/age bounds are not absolute for that
newest pair. There is no background cleanup timer, so age pruning occurs on the
next record. Durable SQLite attempts may therefore outlive their raw frames; the
HTTP archive stays readable but has no evidence-download route.

Schema v2 adds `mobile_tasks.skill_scope_id`. The in-place v1 migration rewrites
existing SkillMemory keys and task scopes into the `legacy:` namespace before
setting the schema version to 2. It performs no model or device work.

On process start, recovery applies this precedence to every non-terminal task:

- any open physical intent whose transport/finalization did not durably finish:
  finalize the task as `uncertain` with `restart_open_intent`, record
  `replayed=false`, and never replay that intent;
- otherwise, an accepted stop or `stopping`: finalize as `stopped`;
- otherwise, with no open physical intent: clear the old worker claim, return
  the task to `queued`, and continue from the persisted checkpoint;
- more recoverable tasks than the bounded queue can admit: fail the excess
  task with `recovery_queue_full` rather than dropping it.

Clean shutdown produces the first case deliberately: non-dispatched open
decisions are finalized `not_sent`, worker claims are released, and Tasks stay
`queued`. Constructor recovery then re-enqueues them. `MobileTaskArchive` can
read these checkpoints even when runtime dependencies are unavailable.

Transport exceptions, transport status `uncertain`, missing fresh post-action
observation, or verifier failure after an accepted transport likewise preserve
uncertainty and never cause automatic replay. An accepted transport remains
different from visible progress, Subgoal satisfaction, and whole-task
completion.

## Versioned SkillMemory

The default one-sentence UI does not send `skill_id`. The production
`SkillScopeResolver` derives an internal stable scope from the normalized goal:

- Soul goals return no MobileTask scope because the `soul-reply-v1`
  Application Profile owns its separate delayed-outcome reply learning;
- 率土之滨 goals use target-independent `stzb/daily-rewards/v1`,
  `stzb/tutorial/v1`, `stzb/launch/v1`, or `stzb/general/v1` scopes according
  to explicit goal terms;
- other goals use a redacted SHA-256-derived `generic/exact-goal/v1/...` scope
  after whitespace normalization and case folding.

The resolver deliberately excludes `target_id`, so verified procedure memory
can be offered on another compatible Android Target. This is reuse policy, not
evidence that a real tablet has been exercised successfully.

The store prefixes automatic scopes with `auto:`. A caller-supplied legacy
`skill_id` remains supported and is isolated under `legacy:`, so an automatic
scope never aliases an explicit value. The internal `skill_scope_id` is not in
the HTTP TaskState; consequently `skill_id` may be null while
`skill_memory_version` is positive. The latest matching SkillMemory is supplied
to Planner, Executor, and Reflection as a hint. The current fresh screen and
owner inputs remain authoritative; SkillMemory is not a coordinate macro.

A new immutable version is inserted only in the same transaction that marks a
task `completed` after every Subgoal was freshly verified. The stored procedure
comes from the verified TaskPlan, with the final strategy and satisfied
verification evidence. A failed, stopped, uncertain, merely transported, or
partially verified task cannot promote SkillMemory. This does not update or
fine-tune model weights and is separate from Game Learning `PolicyMemory`.

## HTTP projection and redaction

The HTTP Adapter exposes enough information to understand progress while
hiding transport internals:

- ActionAttempts expose ID, sequence, Subgoal index, action type, transport
  status, bounded verification, and timestamps;
- ActionAttempt arguments, typed text, raw model output, BEFORE/AFTER frame
  identifiers, and screenshot bytes are omitted;
- events expose only sequence, event type, and timestamp; internal event data
  is omitted;
- owner goal and owner-input content are intentionally visible because they
  are part of the task Interface, not telemetry;
- follow-up input `client_request_id` values are visible with those inputs;
  create and stop request IDs remain internal;
- API keys and credentials are never part of TaskState.

The exact HTTP shape, validation limits, idempotency, and stable errors are in
[`../contracts/mobile-task-v1.md`](../contracts/mobile-task-v1.md).

## Application Adapters and exclusions

Games and ordinary Android applications use the generic task Interface. The
production resolver normally supplies an internal automatic skill scope; an
advanced caller may still provide legacy `skill_id`. Callers start, inspect,
update, and stop a MobileTask through the same seam either way.

Soul is one application capability, not AI-GAME itself. `ApplicationRuntime`
owns its long-lived instance, local-vision/cloud-policy cycle and dispatch
revision fence. Conversation observation and physical-operation truth remain
owned by `dating-copilot` and are reached through owner
observation/reserve/dispatch/inspect calls. AI-GAME does not copy that physical
ledger into `mobile-tasks.db`, and an unfinished Soul intent is inspected for
convergence rather than replayed.

The external dating-copilot managed scheduler separately owns all-day matching,
immediate match openers, bounded unread discovery, and zero-quota Planet
fallback; it does not draft ordinary replies. Application Start/Pause/Resume/
Stop contributes to a content-free durable scheduler target and a
GET-before-PUT reconciler. All nonterminal Soul instances are aggregated:
running demand wins over paused demand, and `stopping` retains that instance's
pre-Stop target. Only with no running/paused demand and a Stop durably settled
by the Application core does the matcher target stopped; failed/completed
instances do not imply stop. AI-GAME process shutdown never changes that target
implicitly. None of this scheduler lifecycle is a MobileTask command or record
in `mobile-tasks.db`.

The primary browser information architecture has exactly four entries:
one-sentence start, Soul, Device, and Settings. The first is the MobileTask
composer/inbox; Soul is the typed ApplicationRuntime workspace.
Chat and Game Learning APIs/history, plus legacy Workflow, Run, and Approval
routes/records, remain compatibility/advanced surfaces rather than primary
navigation. The MobileTask Interface does not automatically route a sentence
into Chat, a LearningJob, or a Soul command, and its worker never claims those
compatibility records. There is no universal Run domain object.

One supplied live record establishes only that MobileTask
`daac81a7-1af9-47e3-9566-66e73509a0fd` completed after 23 ActionAttempts under
skill scope `auto:stzb/tutorial/v1`. That fact does not establish broader game
competence and does not accept the separate Soul profile. The new Soul reply
chain still awaits this round's live validation.

This Module does not establish any of the following:

- that Mobile-Agent is installed;
- that a configured model or ADB target has completed a task;
- that an emulator, USB phone, Wi-Fi phone, or tablet was live-tested merely
  because its Target transport is implemented;
- that another game objective or the new Soul reply chain has passed current
  real-device acceptance;
- that transport acceptance proves an application outcome;
- semantic protection for credentials, payments, permissions, CAPTCHA, or
  other consequential screens;
- continuous visual control, multi-pointer input, low-latency control, or
  readiness to play real-time action games.

Real-time gameplay remains outside the current Implementation and has separate
future evidence gates in [`gameplay-readiness.md`](gameplay-readiness.md).
