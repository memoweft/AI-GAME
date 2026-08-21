# U6 — Canonical Kernel autonomous worker canary

## STATUS

`NOT_STARTED`

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

Current default Gateway is not mounted; even an explicitly built Gateway task does not have a worker or production action executor. First make one canary task progress from `CREATED` under controlled fake ports, then wire current-source real execution.

## VERIFY

- Worker lifecycle, seriality, crash, control, lease, revision, and no-replay tests.
- Plan/observe/act/verify/recover/final-complete component tests.
- Gateway API/SSE projections.
- Full backend/frontend regression.
- Normal current-source server with explicit canary composition.
- Real settings/battery GoalRun through Kernel, including one pause/resume and final Home proof.
- Failure injection after action transport and before settlement.
- Confirm Legacy worker never owns the canary target concurrently.

## ROLLBACK

Switch v2 execution binding back to `mobile_task_compat`. Preserve Kernel task/evidence for diagnosis; do not copy or replay an unresolved action into Legacy.

## DONE WHEN

One complete business GoalRun—not atomic smoke—passes all required evidence through the Kernel canary, controls are physical, recovery is safe, and compatibility fallback remains valid.

## NEXT

`U7_REAL_KERNEL_CUTOVER.md`.
