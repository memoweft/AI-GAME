# U6 — Canonical Kernel autonomous worker canary

## STATUS

`DONE`

Activated 2026-08-23 by explicit owner direction. U5 remains truthfully
`PARTIAL`; its remaining STZB-specific stabilization is non-blocking for this
bounded, reversible settings/battery canary.

Completed 2026-08-23. This closes the explicit U6 canary slice only. At the U6
completion snapshot, default cutover remained U7; its current status is tracked
by `../00_INDEX.md` and `U7_REAL_KERNEL_CUTOVER.md`.

## BUSINESS OUTCOME

A v2 GoalRun can use the new RuntimeKernel—not the Legacy MobileTask worker—to complete one full settings/battery goal on a real Android target with planning, action, verification, controls, experience, and honest completion.

## IN SCOPE

- Add model role ports and adapters to the Kernel boundary.
- Add a serial Goal coordinator/worker.
- Wire the production Android observation and action executor.
- Hold task-session physical ownership with heartbeat/deadline behavior.
- Implement Stage planning, one-action decisions, immediate verification, recovery, final Goal verification, and result projection.
- Integrate v2 events/SSE and Experience Service.
- Implement physical pause/resume/cancel/takeover fences.
- Restart with mandatory new observation and no uncertain replay.
- Add an explicit canary binding selected by deployment/test configuration.
- Reuse proven MobileTask model/device behavior through adapters where clean.

## OUT OF SCOPE

- Default runtime cutover.
- Soul or other long-lived capability migration.
- Removing MobileTask.
- Multi-device scheduling.
- Running the same goal in both engines.

## DESIGN

- Gateway task creation must start or enqueue a real worker, not remain `CREATED`.
- RuntimeKernel owns facts and state; model adapters only propose.
- Production composition must be the same code used by canary runtime evidence.
- One GoalRun has exactly one execution binding.
- Resume observes before acting.
- Candidate completion passes the independent Goal Completion Verifier.
- Experience writes use the same canonical ledger as U4.

## EARLIEST REAL BREAK

At activation, the default Gateway was not mounted and even an explicitly built
Gateway task had no worker or production action executor. The implementation
first made one canary task progress from `CREATED` under controlled fake ports,
then wired current-source real execution.

## VERIFY

- Worker lifecycle, seriality, crash, control, lease, revision, and no-replay tests.
- Plan/observe/act/verify/recover/final-complete component tests.
- Gateway API/SSE projections.
- Full backend/frontend regression.
- Normal current-source server with explicit canary composition.
- Real settings/battery GoalRun through Kernel, including one pause/resume and final Home proof.
- Failure injection after action transport and before settlement.
- Confirm Legacy worker never owns the canary target concurrently.

## COMPLETION EVIDENCE — 2026-08-23

- Artifact/source: `kernel_canary.py` adds the serial coordinator over the
  existing RuntimeKernel facts, Qwen role port, Android observation provider,
  typed ADB executor, process device lease, Gateway worker hook, GoalRun
  `runtime_kernel_canary` binding, pause/resume/cancel/takeover fences,
  restart recovery, Experience retrieval/write, and final Goal-gated Task
  commit. `AI_GAME_KERNEL_CANARY_ENABLED=1` is the explicit switch; unset/`0`
  keeps `mobile_task_compat` as rollback.
- Automated: the final backend suite passed `844` tests with one pre-existing
  Starlette/httpx deprecation warning. The frontend suite passed `57` tests and
  the TypeScript + production Vite build passed. New tests cover candidate vs
  final completion, pause-before-dispatch, accepted-unverified restart to an
  explicit uncertain failure without replay, Experience gating, and GoalRun
  canary controls.
- Current-source runtime: the normal launcher restarted with the explicit
  canary switch, mounted `/api/v1` Gateway routes, reported both discovered
  Android targets, and exposed `runtime_kernel_canary` as the selected v2
  preflight capability. The service remained in `legacy` mode because U7
  default cutover is out of scope.
- Real device: GoalRun `ed42b714-4106-4332-b134-b3d8a1b8e6be`, Kernel Task
  `2bc95172-d1dd-4959-b513-a05f690736ca`, bound MuMu target
  `adb:127.0.0.1:16384`. It froze five criteria, completed three Kernel Stages
  with four actions, visibly proved Settings, the Battery page at 95%, and the
  Home screen, then reached GoalRun `COMPLETED / verified` and Kernel
  `TaskCompleted`. Before the first action, pause produced `PAUSED`; three
  seconds later event count and proposed-action count were unchanged. Resume
  recorded a new observation before the first proposal. Runtime mode reported
  zero active Legacy tasks before the canary.
- Learning: current-source GoalRun `01a7a497-5854-405a-9512-07653a390f23`
  opened a canonical Experience Episode, recorded its verified transition,
  then promoted candidate `665aafd5-6f8c-40f9-a1e6-eecf3a2e8e13` only after
  Goal completion. The next same-scene GoalRun
  `86ca39b9-823d-4197-895f-b3e4ea36fb68` retrieved that exact candidate before
  its decision and still performed fresh observation, verification, and final
  Goal gating; both Task and Goal completed.
- Failure injection: an accepted, unverified Kernel action persisted across the
  simulated restart boundary. Recovery emitted `TaskUncertain`, represented by
  frozen Task status `FAILED` plus `failure_state.last_verdict=UNCERTAIN`, and
  created no second Action. This preserves the existing SQLite Task-state
  contract while proving no uncertain replay.
- Evidence boundary: this is current-source, automated, and local real-device
  canary evidence. It is not packaged deployment, default Kernel cutover,
  multi-device scheduling, Soul migration, browser-window acceptance, or
  external-world outcome proof.

## ROLLBACK

Switch v2 execution binding back to `mobile_task_compat`. Preserve Kernel task/evidence for diagnosis; do not copy or replay an unresolved action into Legacy.

## DONE WHEN

One complete business GoalRun—not atomic smoke—passes all required evidence through the Kernel canary, controls are physical, recovery is safe, and compatibility fallback remains valid.

## NEXT

`U7_REAL_KERNEL_CUTOVER.md`.
