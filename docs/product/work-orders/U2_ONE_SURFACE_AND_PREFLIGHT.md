# U2 — One product surface and automatic preflight

## STATUS

`PARTIAL`

## BUSINESS OUTCOME

The user starts a phone goal from one primary composer. The platform inspects model and device readiness, automatically selects the sole compatible idle target, performs safe reversible local repair, or asks one plain external-gate question.

## BASELINE FACTS AT ACTIVATION

- Current navigation exposes multiple product/runtime surfaces.
- MobileTask requires a selected Target, although the UI preselects one ready target in some cases.
- Model, ADB, and MuMu helpers exist but are manual and not GoalRun state.
- Current helpers must not be assumed safe to broaden without identity and lifecycle checks.

## IN SCOPE

- Add GoalRun `PREFLIGHT`, environment facts, repair attempts, and waiting projection.
- Create a capability/environment probe over current model, ADB, target, package, lease, and owned service facts.
- Automatically select the sole compatible idle Android target.
- Wrap existing safe MuMu/ADB/model helpers behind idempotent, verified repair actions.
- Resume the same GoalRun after readiness changes.
- Make the single goal composer the primary product page.
- Move device/settings/evidence to supporting or advanced views.
- Hide legacy runtime-specific workspaces from primary navigation without deleting them.
- Preserve v1 deep links/diagnostic access during migration.

## OUT OF SCOPE

- Executing missing-component downloads, software installation, or operating-system security changes; U2 may discover the gap and present the D12 plan-level confirmation state, while the managed installer is delivered by U9 or an explicitly scoped earlier order.
- Qwen orchestration.
- Multi-capability router.
- Kernel worker or cutover.
- Learning changes.

## DESIGN

- Inspect before asking.
- A configuration file is not readiness evidence; every automatic repair has a post-check.
- More than one materially different valid target creates a plain selection gate.
- An active GoalRun never silently changes physical target.
- A third-party lifecycle not delegated to AI-GAME becomes a gate, not a hidden process mutation.
- Primary UI never asks the user to choose runtime/profile/model/skill.

## EARLIEST REAL BREAK

The v2 goal reaches a compatibility task, but environment readiness and target selection are currently outside GoalRun truth and the user still sees split product surfaces.

## VERIFY

- Probe/assessment/repair state-machine tests.
- Sole/multiple/no-target matrices.
- Lease conflict and stale target tests.
- Repair idempotency and unrelated-process identity tests.
- Frontend tests for one composer, waiting gate, refresh, and advanced views.
- Full backend/frontend regression and build.
- Current-source normal launcher.
- Real settings/battery GoalRun from the primary composer with no runtime/profile/model/serial selection.
- Confirm its physical progress and evidence are visible but its current non-terminal projection remains `CANDIDATE_COMPLETE` until U3 supplies independent original-goal verification.
- Confirm no successful goal-level SkillMemory is promoted from this compatibility execution.
- Refresh/close/reopen preserves the same GoalRun.

## ROLLBACK

Re-enable the old navigation and disable automatic repair while retaining U1 GoalRun data and v1 paths.

## DONE WHEN

The primary goal surface, automatic preflight/selection, compatibility execution, evidence projection, and persistence pass on the real settings/battery path without a false `COMPLETED` claim. Full scenario-A goal completion remains the U3 gate; unavailable real-device proof makes U2 `PARTIAL` with the exact gate reported.

## NEXT

`U3_QWEN_AND_GOAL_VERIFICATION.md`.

## CURRENT EVIDENCE — through 2026-08-21

Implemented and verified:

- GoalRun schema v3 persists `PREFLIGHT`, an environment projection, and a
  repair-attempt ledger with
  model/runtime/discovery/target/Lease facts, selected target, and plain target
  options.
- Current production composition performs fresh model and ADB probes, excludes
  non-Android, stale, unavailable, unauthorized, and leased targets, selects
  the sole idle Android target, and honors an explicitly configured ready
  deployment serial.
- Multiple materially valid targets enter
  `WAITING_EXTERNAL/TARGET_SELECTION_REQUIRED`; selection is freshly
  revalidated and an already bound GoalRun cannot switch target.
- `/preflight/retry` resumes the same durable GoalRun after readiness changes.
- The primary page is a single GoalRun composer with no runtime/profile/model/
  serial choice. Refresh restores the active GoalRun from durable v2 history.
  MobileTask, Gateway, and Soul moved under Advanced diagnostics with preserved
  `#diagnostics/mobile|gateway|soul` deep links.
- Backend focused verification passed 46 tests before the repair slice; final
  backend regression after the repair-ledger and v2-to-v3 migration test
  changes passed 740 tests with one dependency deprecation warning in 177.68
  seconds.
- Frontend regression passed 56 tests; TypeScript and the production Vite build
  passed. The normal launcher served the current bundle and v2 API on port
  4310.
- Live safe repair evidence: the already-running MuMu 0 identity and lifecycle
  checks passed; `sync-mumu-executor.ps1` atomically refreshed the loopback
  serial to `127.0.0.1:16384`; post-check `adb get-state` returned `device`.
- Current GoalRun `2c1cffd8-ce75-4213-b857-d9296882d6c7` persisted across a
  current-source restart and preflight retry. It recorded model unavailable,
  compatibility runtime ready, ADB discovery ready, and Android targets ready,
  while creating no MobileTask.
- Every managed repair key is now durably claimed before any external action.
  Replays return the same record, an interrupted `APPLYING` action is not
  uncertainly replayed, unconfirmed ownership produces `SKIPPED_IDENTITY`, and
  a successful action without a ready post-check produces `FAILED`. These
  attempts are included in the GoalRun API projection.

Additional evidence from 2026-08-21:

- Production preflight now registers the concrete MuMu/ADB/model helpers behind
  the persistent repair manager. Identity refusal, no-unrelated-process mutation,
  idempotency, post-check, external model status formats, and same-GoalRun
  reassessment are covered by tests.
- Real settings/battery execution now exists. GoalRun
  `a3f35c7b-4ca9-48a3-b866-c4cb5ed812e5` selected MuMu 0 and bound task
  `b8e1b87f-6c9e-4b2b-9c04-a9dbec45f651`. The Qwen multimodal canary used ten
  evidence-backed attempts to open Settings, recover from its search page, verify
  the battery page, read visible battery facts, and verify Home. GoalRun remained
  `CANDIDATE_COMPLETE` as required; the compatibility task promoted no successful
  SkillMemory.
- Current regression passed 752 backend and 56 frontend tests; the production
  frontend build and normal launcher passed.

Open U2 gate:

- Interactive browser screenshot inspection remains `NOT AVAILABLE` because the
  installed browser control integration is missing `browser-service.mjs`.
  Consequently the real run is not proven to have originated from the rendered
  primary composer, and refresh/close/reopen of that rendered surface is not
  accepted merely from API persistence or automated UI tests.

Therefore U2 remains `PARTIAL`. The real-device and managed-repair gates are now
green, but the required real primary-composer visual/reopen gate is unavailable.
At this U2 closeout snapshot, formal U3 had not been activated and Qwen work was
recorded only as a user-directed, bounded compatibility canary. U3 was later
activated and completed; its current status is recorded in
`../06_IMPLEMENTATION_ROADMAP.md`.
