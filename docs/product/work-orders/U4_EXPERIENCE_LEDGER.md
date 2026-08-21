# U4 — Unified experience ledger and scene-conditioned memory

## STATUS

`DONE` — 2026-08-21. The additive ledger, MobileTask and selected GameLearning
adapters, semantic/perceptual scene retrieval, candidate/policy lifecycle,
scope and UI-version isolation, usage/trial attribution, controlled benchmarks,
and a real attributable settings/battery warm run passed.

## BUSINESS OUTCOME

The system can remember not only what succeeded, but what was verified wrong, which wrong page resulted, how it recovered, and when that experience applies on a later comparable screen.

## IN SCOPE

- Additive ExperienceEpisode, SceneState, ActionTransition, OutcomeSignal, Candidate, and PolicyRevision storage.
- Immediate outcome and recovery capture from the compatibility MobileTask loop.
- Adapter/import path for selected GameLearning provenance.
- Scene/objective/scope-conditioned retrieval.
- Positive, negative, recovery, uncertain, delayed-capable signal vocabulary.
- Candidate validation, support/failure confidence, promotion, reject, deprecate, rollback.
- Retrieval and actual-use attribution.
- Cold/warm deterministic fixtures and metrics.
- Consistency checks for legacy SkillMemory references.

## OUT OF SCOPE

- Deleting or merging old databases.
- Model weight training.
- STZB production claim.
- Kernel execution migration.

## DESIGN

Follow `../04_AUTONOMY_AND_LEARNING.md`. Coordinates are evidence only. Uncertain is neither success nor permanent failure. Original Goal verification gates successful goal-level memory. Scope isolates user/account/application/goal/UI compatibility.

## VERIFY

- Schema/migration/restart tests.
- Provenance and scope invariant tests.
- Same-scene wrong-action suppression fixture.
- Wrong-page recovery fixture.
- Stale UI compatibility/degradation fixture.
- Candidate rollback and legacy import tests.
- Cold/warm benchmark meeting initial controlled thresholds.
- Full regression; current settings/battery GoalRun uses and records experience without regression.

## ROLLBACK

Disable experience retrieval and promotion while preserving the append-only ledger. Compatibility execution continues without deleting evidence.

## DONE WHEN

Controlled warm runs demonstrate attributable improvement and every promoted rule has complete provenance, goal coverage, and rollback.

## COMPLETION EVIDENCE

- `runtime/console/experience.db` is an additive schema-v2 database. It does
  not merge or delete MobileTask, SkillMemory, or GameLearning history.
- MobileTask records Episode, Scene, Transition, Outcome and Candidate facts;
  independent verified Goal completion is required before promotion. Uncertain
  transitions never create candidates.
- Active retrieval is isolated by user/account/application/goal/UI/device
  scope, objective similarity, perceptual scene compatibility and confidence.
  Coordinates remain audit evidence; Qwen supplies a semantic target description
  and must re-ground it against the current screenshot.
- Candidate promote/reject/deprecate, immutable PolicyRevision heads, rollback,
  support/failure trials, legacy SkillMemory validation and selected finalized
  GameLearning import all have focused tests.
- The three-scene controlled suite covers ordinary, modal and shifted-anchor
  states. Warm actions fell from 16 to 9 (43.75%), false completion was zero,
  promoted provenance was 100%, known-wrong repeat was 0%, known-wrong recovery
  was 100%, interventions were zero and scope leakage was zero.
- Real cold GoalRun `5c3a161d-3608-4aee-aa61-13e2c1e0e418` completed in ten
  actions. Attributable warm GoalRun `db578fbb-c842-40ea-a998-545c3dda5fe2`
  completed in five actions, a 50% reduction without a completion-rate drop.
  It persisted five retrievals and eight supportive usage/trial attributions,
  including avoidance of the known same-scene Home failures.
- The warm completion history preserves revision 1 `partial`; after clarifying
  that the GoalRun final response is the delivery carrier for a report criterion,
  explicit retry appended verified revision 2 using attempts 2-5. It reported
  battery 98%, charging with about one hour remaining, battery saver off, and
  verified Home.
- A separate warm attempt `7a856427-0f4c-482a-a29d-e446114a4d55` remains an
  honest failed runtime record after a model-service outage. No action replay or
  completion promotion occurred.
- Final regression on the completed code is recorded in current state. Qwen and
  the console were restored through their normal owned launchers.

Rollback is configuration-level: construct `ExperienceService(enabled=False)`
or omit the MobileTask experience adapter. Execution continues through the
compatibility runtime while the append-only ledger remains readable.

## NEXT

`U5_STZB_DAILY_LEARNING.md`.
