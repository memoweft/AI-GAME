# U7 — Real Kernel cutover and compatibility retirement

## STATUS

`NOT_STARTED`

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
- Observation-period or deployment ownership is unspecified.

## VERIFY

Follow the exact cutover acceptance in `../07_ACCEPTANCE_AND_EVIDENCE.md`, including normal launcher, real goal, controls, failure injection, actual restore, non-truncated logs, rollback, and zero dual ownership.

## ROLLBACK

Stop new admission, settle/inspect any Kernel physical uncertainty, restore the prior explicit binding/configuration and data copy, start Legacy only after ownership is clear, then verify the read/write and device state. Never use a blind service restart as rollback.

## DONE WHEN

Kernel is the real normal execution path with completed real-goal evidence and a demonstrated safe rollback. Until then the status is canary or partial, not production deployment.

## NEXT

`U8_LONG_LIVED_ROUTING.md`.
