# U7 — Real Kernel cutover and compatibility retirement

## STATUS

`DONE`

The local normal launcher is cut over to the real Kernel composition. The real
goal, controls, restore, rollback, restart, and D17 30-minute local observation
all passed on 2026-08-23. Packaged or production deployment acceptance remains
U9 scope.

## BUSINESS OUTCOME

The normal AI-GAME launcher starts the canonical v2 Goal/Kernel execution path. KernelActive means a real autonomous worker can complete phone goals, not merely that Legacy writes are rejected.

## IN SCOPE

- Make default/selected normal startup construct Goal API, Gateway, Kernel coordinator, model roles, ADB executor, ownership, environment, and experience bindings.
- Use existing Legacy/Draining/KernelActive gates with corrected semantics.
- Drain existing Legacy work and snapshot before cutover.
- Expose Legacy history through an explicit read-only compatibility namespace.
- Execute a real v2 GoalRun after cutover.
- Preserve and archive mode logs instead of truncating evidence.
- Perform a controlled actual snapshot restore.
- Document install/start/observe/rollback commands and evidence.

## OUT OF SCOPE

- Row-copying old tasks into the new domain.
- Deleting old databases.
- Broad multi-capability router.
- Production declaration without observation period and rollback proof.

## DESIGN

- One public contract per explicit namespace; never infer version from payload shape.
- `kernel_active` startup fails closed if the autonomous composition is unavailable.
- Legacy physical writers are disabled only when Kernel ownership is ready.
- Old MobileTask data remains read-only archive.
- A cutover never reconciles an uncertain Kernel physical action by replaying it in Legacy.

## MUST ASK IF

- The intended target is a real production account/device not already authorized for cutover.
- A database transformation is destructive or cannot roll back from a verified copy.
- The current task would claim U7 `DONE`, declare production acceptance, or
  apply the ordinary U7-to-U8 advancement rule while observation duration or
  its pass criterion is still unspecified.

[constraint-source: PRODUCT_SPEC; ref: `../06_IMPLEMENTATION_ROADMAP.md` U7 required sequence and `../09_DECISIONS_AND_OPEN_QUESTIONS.md` section 2]

This question is scoped to U7 closeout or U8 advancement. It does not activate
U7 and is not a blocker for unrelated user-authorized work.

## VERIFY

Follow the exact cutover acceptance in `../07_ACCEPTANCE_AND_EVIDENCE.md`, including normal launcher, real goal, controls, failure injection, actual restore, non-truncated logs, rollback, and zero dual ownership.

### Completed local observation

[constraint-source: USER_DECISION; ref: U7 observation execution instruction 2026-08-23]

`kernel-observation.cmd` executed the D17 definition in
`../09_DECISIONS_AND_OPEN_QUESTIONS.md`: 1,801.493 seconds, 61/61 passing
samples, and a 3.675-second midpoint restart. The evidence is recorded below.
This closes only the U7 local cutover observation; U9 owns packaged and
production long-run acceptance.

## ROLLBACK

Stop new admission, settle/inspect any Kernel physical uncertainty, restore the prior explicit binding/configuration and data copy, start Legacy only after ownership is clear, then verify the read/write and device state. Never use a blind service restart as rollback.

## DONE WHEN

Kernel is the real normal execution path with completed real-goal evidence and a demonstrated safe rollback. Until then the status is canary or partial, not production deployment.

## NEXT

Finish the owner-defined observation period for this work order. Under the
ordinary advancement rule, U8 follows U7 `DONE`; advancing while U7 remains
`PARTIAL` requires a new explicit owner decision under roadmap section 5.

## DELIVERY EVIDENCE — 2026-08-23

### Source and operator surface

- `Settings.from_env()` and `scripts/console.ps1` default to `kernel_active`;
  unknown mode values fail closed.
- Normal Kernel-active startup constructs RuntimeKernel, Gateway,
  `KernelCanaryCoordinator` in production-binding mode, the configured Qwen
  role, ADB observation/action adapters, Experience service, and v2 Goal API.
  It fails startup if that autonomous composition is unavailable.
- New production GoalRuns bind as `runtime_kernel` and record
  `KernelTaskAccepted`. Historical U6 `KernelCanaryAccepted` events remain
  recoverable.
- Legacy physical-write surfaces return 403 in Kernel-active mode. Historical
  MobileTask data is exposed only under `GET /api/compat/v1/mobile-tasks...`.
- `kernel-cutover.cmd` and `scripts/kernel-cutover.ps1` implement status,
  drain, snapshot, controlled restore, activation, and guarded rollback.
- Console mode logs are append-only and previous stdout/stderr files are moved
  to `runtime/logs/archive/` instead of being truncated.

### Cutover, restore, and rollback

The executed local sequence was:

```text
legacy -> draining -> zero active Legacy tasks
-> SQLite snapshot -> controlled restored copy with integrity/digest check
-> kernel_active -> real GoalRun and controls
-> guarded rollback to legacy -> verified legacy writable
-> draining -> second snapshot/restore check -> final kernel_active
```

The final snapshot is
`runtime/backups/kernel-cutover/mobile-tasks-20260823T053140919830.db`.
The controlled restored copy is
`runtime/backups/kernel-cutover/restore-exercises/mobile-tasks-restored-20260823T053141394869.db`.
SQLite integrity was `ok` and both logical dumps had SHA-256
`daf0c8935eec5892e46d4cdf98ff7b0584faf07cf7e21ec6f0be39e8f67f1b71`.
`runtime/logs/kernel-cutover.jsonl` records the zero-drain, snapshots, restore
checks, activation, and successful rollback without overwriting earlier rows.

### Current-source runtime and real device

- GoalRun `aeddc7a8-9f8f-4f1d-8dd5-f1bbcafbfc09` bound Kernel Task
  `1e98f1f1-6671-4d88-a7b4-2c4f9cb62471`. Owner pause held event/action counts
  stable for three seconds; resume continued from the pre-execution fence.
- The task used two proposed/executed/freshly observed/verified actions. It
  verified all four frozen criteria: Settings open, visible Battery details,
  user report, and return Home. Goal Completion committed `COMPLETED / verified`
  before Kernel `TaskCompleted`. Visible evidence reported 78%, charging with
  about one hour remaining, and Battery Saver off, then a Home-screen frame.
- GoalRun `a6f6c79e-957c-4627-9016-a060d75784bf` bound Kernel Task
  `6c4d06b4-51ef-4fca-b7a5-2ca2a9714169`; pause then stop produced durable
  `CANCELLED`, and event/action counts remained stable with zero proposals.
- After the final source edit, default startup without a `RuntimeMode` argument
  reported `kernel_active`, Legacy writable false, and zero active Legacy work.
  A new GoalRun `4d2437cd-fc6f-43b3-a5ef-4d0ef62c2087` bound
  `runtime_kernel`, recorded `KernelTaskAccepted`, and was immediately
  pause/cancelled with zero proposed actions.
- Restart preserved the successful task at 34 events/two actions and the
  cancelled task at four events/zero actions; no uncertain action was replayed.

One diagnostic GoalRun (`5c6be977-149c-4005-bd5e-5603c4383fa2`) intentionally
used a no-op wording that the local goal model could not turn into frozen
success criteria. It correctly remained `WAITING_CONFIGURATION` with
`goal_specification_unavailable`, no task binding, and no device action. It is
not counted as an execution pass or failure.

An earlier real cutover attempt (GoalRun
`d58cd3e2-084c-4735-af9e-aa1775d2a130`, Task
`75d7076e-0c63-4ec1-a678-d8e24e258eb4`) exposed that pause/resume rejected a
newly created Kernel task. That attempt was stopped and both GoalRun and Task
were verified `CANCELLED`. Current source extends the durable control state machine to
pause `CREATED`/`PLANNING` and resume to the recorded prior state; the later
real-device runs and regression suite verify the repair.

### Automated evidence

- Backend: `849 passed`, one pre-existing Starlette/httpx deprecation warning.
- Frontend: `57 passed` across 10 files.
- `npm run build`: TypeScript no-emit check and Vite production build passed.
- Focused cutover/control/Goal/Kernel set: `62 passed`.
- Restart failure injection for an accepted, unverified action: `1 passed`; the
  action becomes uncertain and is not replayed.
- `git diff --check`: no whitespace errors; only Windows LF-to-CRLF notices.

### Observation closeout

The final observation ran from `2026-08-23T06:45:11.0180213Z` through
`2026-08-23T07:15:12.5140444Z`. All 61 samples reported healthy
console/database, ready runtime, `kernel_active`, Legacy read-only, and zero
active Legacy/Kernel tasks or Leases. The midpoint restart changed listener PID
35380 to 22484 in 3.675 seconds and appended one complete
stop/composition/start sequence with `runtime_kernel` and Legacy workers off.
Legacy writes returned `403 / LEGACY_DEVICE_WRITE_DISABLED` before and after;
read counts remained Legacy archive 1, Kernel tasks 7, GoalRuns 86; fatal error
matches were zero. Evidence:
`runtime/logs/u7-observation-20260823T0645110180213Z.jsonl`, SHA-256
`AE346166DA7FD6A386C5A27F6710C60811DD91FBD5AE4D8BCE1BD52181E01B8D`.

Current-source closeout passed 98 focused tests, 849 complete backend tests,
57 frontend tests, TypeScript checking, the Vite production build, and
`git diff --check`. U7 is `DONE`; U8 is active under the ordinary rule.

The live role endpoint identified itself as `qwen3.8-27b-u` with the current
machine-local 65,536-token configuration, whereas the earlier accepted U3/U6
snapshot documented `qwen3.8-27b` at 32,768. U7 did not change that external
configuration and the real goal passed, but no controlled comparison was run;
this run therefore does not promote the drifted binding as a new accepted
visual-model baseline.
