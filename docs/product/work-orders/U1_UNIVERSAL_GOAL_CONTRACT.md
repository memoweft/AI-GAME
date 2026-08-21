# U1 — Universal Goal contract and compatibility facade

## STATUS

`DONE`

## BUSINESS OUTCOME

A client can submit one ordinary-language purpose and receive one durable GoalRun without choosing a runtime, profile, skill, model, device serial, or internal endpoint. The first implementation honestly binds supported Android work to the existing MobileTask execution path.

## CURRENT FACTS

- `POST /api/v1/tasks` is the current real natural-language Android path.
- `POST /api/v1/application-instances` and `/learning/jobs` are separate explicit domains.
- Gateway tasks do not autonomously execute.
- The default server runs Legacy mode and does not mount a production Gateway composition.
- v1 history must remain readable and writable as before during U1.

## IN SCOPE

- Add a `goal_runtime` or equivalently isolated backend module with GoalRun domain, store, service, projection, and API adapter.
- Add an additive local store, recommended `runtime/console/goals.db`, unless inspection proves an existing store is a cleaner isolated owner.
- Implement `/api/v2/goals` create/list/read.
- Implement goal messages and the subset of controls that the compatibility binding can truthfully support.
- Require durable idempotency bound to normalized create payload.
- Persist `binding_kind=mobile_task_compat` and the bound MobileTask id.
- Project current MobileTask state into the stable GoalRun lifecycle without fabricating unsupported pause/resume/takeover.
- Publish GoalRun events or an explicit compatibility event projection with monotonic cursor.
- Add focused domain, store, API, idempotency, projection, and restart tests.
- Tag v2-bound compatibility tasks before execution and suppress or quarantine their successful SkillMemory promotion until an independent original-goal verifier exists.
- Document the exact v2 contract.

## OUT OF SCOPE

- Multi-runtime AI routing.
- Qwen integration.
- New phone execution worker.
- Kernel cutover.
- Automatic device discovery/selection or service repair beyond current compatibility checks.
- Experience schema changes.
- Frontend information-architecture change.
- Removing or renaming any v1 route or database.

## DESIGN

- Original user goal is immutable; later messages are revisioned constraints/inputs.
- GoalRun freezes separate `execution_status` and `control_state` axes exactly as defined in `../01_PRODUCT_SPEC.md`; pause/takeover never masquerade as a business result.
- Ordinary create body contains `goal` and an idempotency value; internal selection fields are absent.
- The GoalRun stores a binding before delegating physical work.
- Compatibility projection cannot upgrade weaker underlying evidence. MobileTask `completed` may be projected as GoalRun `CANDIDATE_COMPLETE` until U3 adds independent final verification; U1 must label that limitation rather than invent certainty.
- U1 may bind only an already configured default target. If none is configured or the configured target is unusable, GoalRun enters `WAITING_CONFIGURATION` with a concrete reason; it does not guess a device. U2 owns automatic discovery, repair, and selection.
- A v2-bound MobileTask may still record raw attempts/evidence, but it cannot activate or expose a successful SkillMemory revision before independent original-goal coverage verification. Existing direct v1 task behavior remains compatible.
- Errors distinguish goal acceptance, binding failure, runtime unavailable, and underlying execution failure.
- API uses an explicit `/api/v2` namespace and cannot collide with current `/api/v1/tasks` payloads.
- Deleting a GoalRun is not part of U1.
- The compatibility binding exposes only controls it can physically fence. An unsupported pause, resume, or takeover returns an explicit capability error; stop behavior is projected only after the underlying worker acknowledges or settles it.

## IMPLEMENTATION FREEDOM

The AI chooses private file layout, SQLite migration mechanism, event projection helpers, pagination shape consistent with repository conventions, and exact test fixtures. It should reuse proven idempotency and serialization utilities where their ownership is clean.

## EARLIEST REAL BREAK

There is currently no durable object or endpoint above MobileTask that accepts a universal goal. Start there; do not touch visual execution first.

## MUST ASK IF

- Implementing the facade would require changing v1 public behavior rather than adapting it.
- A store choice would put GoalRun truth under an application-specific database owner.
- The desired universal lifecycle cannot truthfully represent the current underlying state without a new user-visible semantic decision.

## VERIFY

1. Focused GoalRun domain/store/service/API tests.
2. Existing MobileTask API/runtime focused tests.
3. Full backend regression.
4. Start current source with the normal launcher.
5. Create a v2 goal with no device/model/profile fields.
6. With no configured default target, confirm explicit `WAITING_CONFIGURATION` and no MobileTask side effect.
7. With one configured default target, confirm one and only one compatibility MobileTask is bound.
8. Repeat the same idempotent request and confirm no duplicate.
9. When the compatibility task reports completion, confirm GoalRun is `CANDIDATE_COMPLETE` and no successful SkillMemory is activated for that v2 goal.
10. Read GoalRun after backend restart and confirm stable binding/projection.
11. Confirm all direct v1 routes and history still operate as before.

Real phone completion is recorded if the environment is available, but U1 can be `DONE` after the facade/runtime integration checks because U2 owns the formal real-device product surface gate.

## ROLLBACK

Disable the v2 router registration and leave `goals.db` as an unused additive record. Do not delete MobileTask state or history.

## DONE WHEN

- One v2 create produces one durable GoalRun and one explicit MobileTask compatibility binding.
- The user request contains no internal selection field.
- Missing target configuration waits explicitly; U1 never silently selects a target.
- Projection is honest about current completion limits.
- New v2 compatibility execution cannot pollute active successful SkillMemory before U3.
- Restart and idempotency work.
- Existing v1 regression stays green.
- Contract and `02_CURRENT_STATE.md` are updated.

## NEXT

`U2_ONE_SURFACE_AND_PREFLIGHT.md`.

## COMPLETION EVIDENCE — 2026-08-20

- Artifact: additive `goal_runtime` domain/store/service/API module,
  `runtime/console/goals.db`, mobile-agent schema v3 compatibility tags, and
  `contracts/universal-goals-v2.md` exist in the current working tree.
- Focused automated verification: 35 GoalRun/MobileTask tests passed, including
  missing and unusable default-target waits, create idempotency, restart,
  messages, controls, cursor projection, `CANDIDATE_COMPLETE`, and zero
  SkillMemory promotion for v2 compatibility execution.
- Full backend regression: 724 passed with one pre-existing Starlette/httpx
  deprecation warning in 178.19 seconds on the final source.
- Current-source runtime: the normal `python -m ai_game_console.main` launcher
  served port 44317 from current source. A no-selection v2 settings/battery
  request returned GoalRun `c65ed7bc-1aa2-4ef8-8fd3-680d03bf2a71` in
  `WAITING_CONFIGURATION`; replay returned the same id; binding task id was
  null; `mobile_tasks` count remained zero; restart preserved the GoalRun and
  monotonic event cursor.
- Configured-target compatibility creation and restart binding are proven by
  controlled runtime integration tests. The current machine had no configured
  default Android target, so a current real-phone completion was NOT RUN and
  is not claimed by U1.
- Rollback remains removal/disablement of only the v2 router; additive
  `goals.db` and all v1 MobileTask history remain readable.
