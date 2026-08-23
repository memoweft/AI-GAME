# U8 — Multi-capability and long-lived goal routing

## STATUS

`DONE`

Activated on 2026-08-23 after U7 completed its real cutover, rollback, and
owner-defined local observation gates. Completed on 2026-08-24 after a real
authorized, already logged-in Soul GoalRun passed the generic Android
ApplicationRuntime-to-RuntimeKernel path, bounded physical cycles, nonterminal
candidate notification, real no-response attribution, normal-launcher restart,
and explicit stop sequence. Its frozen plan selected no specialized external
owner. A stop-window ingress defect found during acceptance was repaired and
rerun against final current source before this status changed to `DONE`.

[constraint-source: USER_DECISION; ref: D22 U8 real mobile/application correction 2026-08-23]

## BUSINESS OUTCOME

The same goal composer can start a finite phone task or a long-lived/waiting-driven application purpose. Internal module/profile/owner selection is automatic and visible only as evidence; the long-lived purpose survives candidate notifications and continues until explicit user control changes it.

## CONFIRMED CONTINUOUS POLICY

Decision D14 in `../09_DECISIONS_AND_OPEN_QUESTIONS.md` keeps the long-lived GoalRun active until explicit user stop/takeover/revision. Candidate milestones notify and accumulate evidence but do not automatically pause or complete the goal.

## IN SCOPE

- Classify finite phone operation, long-lived/waiting-driven application, active-goal message/control, and language-only cases.
- Persist a CapabilityBindingPlan before side effects.
- Bind finite phone work to Kernel.
- Keep device-free local managed work as a separate ApplicationRuntime
  capability and supporting fixture, not the mobile/application business route.
- Compose a real long-lived mobile/application GoalRun so ApplicationRuntime
  supervises wait/event/continuation and RuntimeKernel performs each bounded
  physical cycle on the discovered authorized target.
- Keep `soul-reply-v1` or any specialized owner as an optional Registry
  capability. Its separate owner/account contract applies only if the frozen
  binding plan actually selects it. The retired `F:\dating-copilot` Soul-page
  project is excluded from U8.

[constraint-source: USER_DECISION; ref: D20 retired historical owner exclusion]

- Generalize delayed positive, negative, no-response, and user-feedback settlement into the Experience Service.
- Present one GoalRun projection and controls.
- Produce candidate semantics for the continuous GoalRun and keep immediate
  application effects separate from delayed human/external outcomes.

## OUT OF SCOPE

- Guaranteeing another person's behavior or a relationship outcome.
- Duplicating a specialized owner's ADB/ledger logic.
- Exposing `profile_id` or LearningJob selection to ordinary users.
- Treating engagement rate as compatibility or user satisfaction by itself.

## DESIGN

- Classification cannot cause a physical action.
- Misclassification can be rebound only before physical commitment or through explicit safe migration.
- A specialized external-owner ledger remains authoritative only for its own
  physical send.
- Incoming external events and no-response evidence settle delayed outcomes
  only when that external capability is selected; a send receipt is not reward.
- Person/conversation memory is isolated; private content never becomes cross-person generic experience.
- Open-ended GoalRuns notify candidate milestones and continue automatically;
  each wake/cycle has bounded work and backoff, while time, user-event,
  external, and configuration gates produce honest resumable waits.

## VERIFY

- Router structure, persistence, no-side-effect classification, and fallback tests.
- Finite goal still passes through Kernel.
- Local managed long-lived evidence remains a supporting lifecycle fixture.
- A real long-lived mobile/application goal discovers the authorized device and
  logged-in app state, waits for or observes a real event, completes a bounded
  RuntimeKernel observe/action/re-observe/verify cycle, and records its
  immediate effect without duplicate dispatch.
- Candidate notification does not pause the goal; an explicit stop test fences all later work, while ordinary cycles remain bounded and event-driven.
- A later reply, no-response interval, inbound event, or user-feedback outcome
  is separately attributed to the same Experience scope.
- If a frozen plan selects a specialized external owner, that adapter must also
  prove its current authorization, readiness, receipt, and reconciliation. The
  retired historical owner must not be started or used as a substitute.
- Full regression and current-source normal launcher.

## DELIVERED EVIDENCE — 2026-08-23

- Goal schema v7 persists one immutable `CapabilityBindingPlan` before an
  executor starts, including an opaque per-plan `owner_binding_ref` for an
  external owner, and records long-lived notifications separately from terminal
  state. The reference is not an account identity or owner-readiness claim.
- The classifier routes finite phone, long-lived application, language-only,
  and unknown goals without classification side effects. Unknown and
  unavailable-capability cases remain unbound and can be explicitly stopped
  without claiming a physical fence.
- The core classifier also routes `long_lived_local_goal` to
  `local-managed-v1`: an AI-GAME-owned runtime that waits for an authorized
  same-GoalRun message, commits one local candidate milestone with a durable
  receipt, then resumes waiting. It does not contact a device, network,
  third-party account, Soul scheduler, or historical project.
- ApplicationRuntime is managed by the normal `kernel_active` composition.
  Candidate notification does not pause or complete the GoalRun; bounded
  event cycles, at-most-once send, follow-up routing, restart recovery,
  delayed-outcome isolation, pause/resume/takeover/stop, and unbound-stop
  behavior have automated coverage.
- The generic ApplicationRuntime core accepts a content-free
  `ExternalOwnerEvent` only for the exact frozen `owner_binding_ref`. It uses a
  durable owner-event idempotency fence, can wake the normal bounded cycle, and
  rejects new owner events after stop. No current production owner adapter has
  supplied such an event; this is not external acceptance evidence.
- Historical current-source long-lived GoalRun
  `00f1e2d7-764d-473d-b206-a13c1ffbf773` entered through `/api/v2/goals`,
  froze the now-withdrawn generic-to-Soul route `long_lived_application` with
  binding `application_runtime`, projected `WAITING_EXTERNAL`, and then durably
  accepted explicit stop as `CANCELLED` with `physical_binding=false`. Current
  code preserves that immutable ledger but will not auto-start or replay an
  unbound plan of this legacy shape.
- Current-source finite GoalRun
  `901e081b-2e8e-460e-b48d-8065249ffc42`, bound to Kernel Task
  `c5187ab5-17e8-4eb6-a319-89188ad58ed8`, completed all three verified
  Settings/Battery/Home stages through the same ingress. The visible battery
  observation reported 91%, not charging, and approximately one hour to full.
- The earlier live normal launcher reported healthy database/runtime,
  `kernel_active`, `legacy_writable=false`, no active Legacy workers, and a
  managed long-lived runtime. Goal schema version 6 and both new tables were
  present.
- The current-source normal launcher was then started with
  `AI_GAME_SOUL_CONSOLE_URL` unset. It returned local `/health` 200, a
  truthful unconfigured-owner projection, and scheduler readiness 503 rather
  than contacting an owner; the Goal ledger migrated to schema v7 and the
  launcher stopped cleanly. This verifies current local runtime composition,
  not scheduler readiness or real external acceptance.
- The historical implicit endpoint at `127.0.0.1:5000` did not establish a
  connection during the earlier diagnostic probes. It is not a current U8
  owner, and that diagnostic is not acceptance evidence. Current source now
  requires an explicit owner configuration and otherwise reports
  `owner_not_configured` without contacting an owner.

Automated unit/integration coverage and the frontend build pass. Final full
regression counts are recorded in `../02_CURRENT_STATE.md`; automated adapters
did not substitute for the real normal-launcher sequence recorded below or for
the separate optional external-effect branch.

## SUPPORTING LOCAL CONTINUITY EVIDENCE — 2026-08-23

The final current-source normal launcher served a newly created local fixture
GoalRun
`7e3a1d78-99d6-4e43-922d-490268629813` through `/api/v2/goals`.

- The local model froze the user's natural-language request as
  `long_lived_local_goal`; no device, account, third-party service, network,
  or Soul owner was requested by that goal.
- Its immutable plan selected `local_managed_application`,
  `local-managed-v1`, `owner_kind=local_runtime`, and no
  `owner_binding_ref`. The durable `goal_binding_plan_frozen` event had cursor
  `2940`, before `application_instance_bound` at cursor `2942` for instance
  `5f868344-8ea8-42c2-8fdf-eac230db5d1a`.
- The instance entered the durable `TIME_OR_INBOUND_EVENT` wait. A same-GoalRun
  user message was accepted as the authorized inbound event and caused exactly
  one local managed checkpoint intent/receipt/outcome. Its nonterminal verified
  outcome emitted exactly one candidate notification (source event sequence
  `8`) and the same instance then persisted its next wait (source event
  sequence `9`). A later same-GoalRun message completed a separate bounded
  no-action cycle (`command_accepted` sequence `10` to `wait_scheduled`
  sequence `11`): it retained exactly two inputs, one intent, one outcome, and
  one candidate notification, with no second checkpoint dispatch.
- A normal `scripts/console.ps1 stop` and `start -NoBrowser` restored the same
  instance in `waiting` with the same two inputs, one intent, one outcome, and
  one candidate notification; it did not replay the checkpoint.
- Explicit `stop` made the GoalRun `CANCELLED`, the instance `stopped`, and
  cleared `wake_at`. A later same-GoalRun message returned `409
  goal_state_conflict`; counts remained two inputs, one intent, one outcome,
  and one candidate notification.
- The created Experience episode is scoped to the same GoalRun/instance with
  `application_id=local-managed`, `goal_family=long-lived/local`, and
  `device_class=local-runtime`. It has zero delayed-outcome signals because the
  selected local capability produces no reply/no-response/user-feedback fact;
  no external outcome is claimed.
- Supporting-run validation before the route correction passed backend `872`
  tests (one pre-existing Starlette/httpx deprecation warning), frontend `59`
  tests, and `npm run build`.

This sequence used no device, network, target application state, real
application event, physical send, reply/no-response fact, or delayed-outcome
signal. It proves local lifecycle plumbing and stop/restart invariants only; it
does not satisfy the U8 mobile/application business outcome. The Soul
specialized branch was not started, configured, or accepted, and no external
result is claimed.

After the generic-to-Soul default was removed, corrected-source verification
passed backend `874` tests (the same single existing warning), frontend `59`
tests, the focused long-lived/Soul/repair/cutover suite (`69` tests), the
GoalWorkspace suite (`7` tests), and `npm run build`. This cleanup started no
console, device, application, account, scheduler, or external owner; these
results therefore do not advance the missing real-device acceptance gate.

## REAL LONG-LIVED MOBILE ACCEPTANCE — 2026-08-24

The normal `scripts/console.ps1` launcher served GoalRun
`5f42cc67-4baa-4e6e-ba58-42e0730000f7` for the user's authorized, already
logged-in Soul account. The GoalRun bound instance
`51f5dd4d-78f9-4a12-ae54-e2e31f1c2a63` to
`adb:127.0.0.1:16384` and foreground application `cn.soulapp.android`.

- CapabilityBindingPlan revision 1 selected route
  `long_lived_mobile_application`, binding
  `long_lived_mobile_composition`, and capabilities `long_lived.wait`,
  `android.observe`, `android.action`, `local.goal_verification`, and
  `experience.record`. `owner_kind`, `owner_binding_ref`, and `profile_id`
  were all null. `goal_binding_plan_frozen` cursor `2968` preceded
  `application_instance_bound` cursor `2975`; no specialized owner was a
  prerequisite or dispatch participant.
- Kernel Task `8581c1ff-85b0-48bb-af3b-7d597db70b71` assessed the frozen Soul
  application as ready, authenticated, and goal-matching. It proposed and
  executed exactly one tap (`57f4a588-5b0c-4b45-b004-bcd7e18d213e`, execution
  `5851caf2-c06a-477c-bcb0-cc538cd59da2`), captured fresh post-action evidence
  `7420554531204c3ab3067218d4e82ee9`, and independently verified entry into the
  Chavio conversation as `SUCCESS`. The durable ApplicationRuntime receipt
  accepted that one bounded Kernel task and recorded a nonterminal
  `confirmed_success` outcome.
- Candidate notification `15359636-dbba-5a50-9768-4e0fa57e8615` referenced the
  same post-action evidence. Goal event cursor `2986` recorded
  `continues=true`; the GoalRun returned to `WAITING_EXTERNAL` with no pause or
  terminal state.
- Later bounded Kernel tasks
  `68472d0c-1462-4d23-a9f2-8a6fbf0f4e6a`,
  `ca587bef-2ece-4b69-8c29-d6028c264230`,
  `1e20fdf0-7d54-4568-b59c-9cf90b65fb7a`, and
  `aafc91c2-20bf-4549-8ec4-a53e6f047c50` each retained the one-action bound and
  durable execution receipt. Their no-progress verification failures remained
  nonterminal to the parent GoalRun. Timer cycle Task
  `bdd8d44e-e8a2-437e-a945-66d38c11f979` later captured fresh observation
  `b1bab7f2-5f96-4bf4-9205-a4d175fb9b7f` and evidence
  `bf4a0e0aed4340c0a609de7be1662ce0` without forming a physical action.
- The 00:34 real Soul frame showed only the user's 00:14, 00:24, and subsequent
  sleeping-emoji greetings in that conversation, with no inbound reply. Event
  `u8-no-response-20260824-001` recorded this as a separate `no_response`
  signal `f187d173-552f-5325-bd9a-05028f568258` in Experience episode
  `b0354f49-126b-47fe-9aa6-3be3d8236a57`. The signal has
  `transition_id=null`, confidence 1.0, both fresh evidence references, and a
  GoalRun/application/conversation attribution scope; it is not an immediate
  action receipt or a claim about another person's intent.
- Multiple normal launcher stop/start cycles restored the same GoalRun and
  ApplicationRuntime instance. The five previously executed physical actions
  retained the same five execution receipts; no recovery replay added a task,
  action, or execution. After the final current-source restart the stopped
  primary GoalRun remained `CANCELLED`, the same instance remained `stopped`
  with `wake_at=null`, `terminal_at` stayed stable, and RuntimeKernel totals
  remained 14 tasks, 18 actions, and 18 executions.
- Explicit stop request `u8-live-stop-001` fenced all further physical work and
  settled the primary instance without adding a Kernel task, action, or
  execution. A diagnostic same-millisecond message exposed that the Goal
  message ingress did not yet reject the transient `STOP_REQUESTED` state; it
  was accepted into the command ledger but produced no physical work. Final
  source now rejects both messages and application outcomes as soon as
  `STOP_REQUESTED` is durable, and preserves the first terminal timestamp.
  Real final-source fence GoalRun `600a0041-8f5e-45e2-b284-2b2a8d8bb64b`
  / instance `2f6a5fc3-2039-480a-9180-43b10388b5f0` was stopped in the activation
  window: both later ingress attempts returned 409, `wake_at` remained null,
  and it created zero Kernel tasks, actions, or executions.
- Final verification passed backend `884` tests with the one existing
  Starlette/httpx deprecation warning, frontend `59` tests, focused U8 routing
  and composition `16` tests, TypeScript checking, and the production Vite
  build. The normal launcher was then restarted in `kernel_active` mode for the
  final durable-state check.

This closes the generic long-lived mobile/application acceptance. It does not
claim a reply, relationship outcome, packaged deployment, production SLO, or
specialized-owner acceptance; those semantics remain separate.

## ROLLBACK

Disable the new generic long-lived-mobile route and leave finite Kernel and
device-free local managed goals unchanged. For a separately selected
specialized-owner run, stop/reconcile that owner through its authoritative API;
do not start a competing ADB worker.

## DONE WHEN

At least two materially different goal families use the same ingress and
projection while preserving their real outcome semantics and one active owner
per mutation. For one newly created long-lived mobile/application GoalRun
through the normal launcher, evidence must show:

- a CapabilityBindingPlan persisted before runtime or physical execution;
- automatic discovery of the authorized device, application, and logged-in
  readiness state;
- ApplicationRuntime wait/event/continuation lifecycle;
- one real bounded RuntimeKernel observe/action/re-observe/verify cycle with an
  at-most-once physical effect and durable immediate receipt/evidence;
- one real later inbound, reply, no-response, or user-feedback outcome
  attributed separately in Experience;
- candidate notification without pause or termination;
- at least one later bounded cycle;
- normal-launcher restart restoring the same GoalRun/instance without replay;
  and
- explicit stop fencing later messages, events, wakes, Kernel actions, and any
  selected specialized-owner dispatch.

If the frozen plan actually selects a specialized external owner, its current
authorization/account, readiness, receipt, and ledger reconciliation are
additional mandatory evidence. They are not a universal prerequisite for the
generic Android route.

[constraint-source: USER_DECISION; ref: D22 U8 real mobile/application correction 2026-08-23]

## NEXT

U8 is complete. `U9_PRODUCTION_HARDENING` is now the active work order under the
ordinary advancement rule. U9 starts from this generic Android evidence and
inherits no specialized-owner, relationship-outcome, packaged-deployment, or
production-SLO claim.
