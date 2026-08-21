# U3 — Local Qwen orchestration and independent goal verification

## STATUS

`DONE`

The user explicitly advanced U3 on 2026-08-21 while U2's unavailable rendered-
browser gate remained recorded rather than waived. U3 product/runtime gates below
are complete; the U2 UI deviation remains outside this work order.

## BUSINESS OUTCOME

The local Qwen binding interprets the full user purpose, freezes observable success criteria, plans and revises Stages, and explains results. Completing the Planner's shortened plan cannot falsely complete the user's goal or promote experience.

## CONFIRMED MODEL DECISION

Decision D11 sets the measured-role rule and D15 records the current tuning
baseline: Qwen3.8 27B serves commander and visual roles through separate
forced-tool contracts. Compare it serially with GUI-Owl on identical fixtures and
real-device goals; use the better measured default without changing generic role
contracts.

## IN SCOPE

- Live discovery of the actual Qwen3.8 27B endpoint and capabilities.
- Generic model capability registry and role binding.
- Qwen structured adapter for GoalSpecification, Stage planning, reflection, and language.
- Start with GUI-Owl as visual operator, benchmark a visual-capable Qwen when available, and persist the evidence-backed winning binding and fallback.
- Frozen success-criteria checklist and coverage mapping.
- Independent Goal Completion Verifier using verified facts.
- `CANDIDATE_COMPLETE` handling in GoalRun.
- Block/deprecate unvalidated legacy SkillMemory from successful promotion.
- Replace U1 quarantine with verifier-gated promotion: only independently covered successful goals can activate goal-level experience.
- Regression test for the known “launch only” daily false completion.

## OUT OF SCOPE

- Experience ledger redesign beyond the promotion gate.
- Kernel worker.
- Broad multi-capability routing.
- Model training.

## DESIGN

- Core contracts reference capabilities, not Qwen/GUI-Owl names.
- Original goal remains immutable.
- Planner cannot remove a success criterion without a revision justified by user input or verified impossibility.
- Final verification is independent from the generated Stage list.
- Model free text cannot directly set terminal state.
- If Qwen is unavailable, behavior is explicit fallback or waiting, never silent binding change.

## VERIFY

- Qwen schema and Chinese-goal conformance tests against the live endpoint.
- When Qwen supports images, identical-input comparison against GUI-Owl for grounded-action validity, wrong/no-effect actions, completion, recovery, latency, and resource use.
- Invalid/partial/timeout output matrices.
- Goal coverage and final-verifier unit/integration tests.
- Freeze the historical case from `../02_CURRENT_STATE.md` into a sanitized fixture: the original daily-reward/task goal, a generated plan containing only launch, verified launch evidence, and an attempted successful SkillMemory promotion.
- Confirm that fixture remains `CANDIDATE_COMPLETE` or becomes partial/failed after independent verification and cannot activate successful experience.
- Settings/battery GoalRun still completes with evidence.
- Full regression, current-source runtime, and role-binding evidence.

## ROLLBACK

Select the compatibility orchestrator binding and keep GoalSpecification/final-verification records. Never restore false-completion promotion.

## DONE WHEN

The live local Qwen commander produces valid structured goal/stage decisions, its actual modalities are recorded, the visual default is selected from comparative evidence when an alternative exists, full-goal coverage blocks shortened-plan completion, and current general-phone acceptance remains green.

## NEXT

`U4_EXPERIENCE_LEDGER.md`.

## COMPLETION EVIDENCE — 2026-08-21

- Goal schema v5 persists source-quoted GoalSpecification criteria before a new
  compatibility binding, plus append-only independent completion assessments.
- Runtime validation requires exact frozen-criterion coverage, valid generated
  Stage indices, and persisted satisfied/non-uncertain ActionAttempt sequences.
  Invalid model references become `uncertain`; model prose cannot terminalize a
  GoalRun.
- Explicit completion retry preserves an earlier `uncertain` revision and appends
  a later `verified` revision.
- The sanitized historical STZB launch-only fixture freezes open-game, reward,
  and daily-task criteria. Launch may pass, but reward and daily-task criteria
  remain false, GoalRun remains candidate, and no successful memory is promoted.
- A quarantined v2 SkillMemory can be promoted only after a persisted verified
  completion assessment. Promotion is idempotent and records GoalRun id plus
  completion revision; partial/uncertain outcomes have no promotion call.
- Fresh GoalRun `2a363ad3-5e70-4b99-b25a-5582f5053fbf` froze four criteria before
  binding MobileTask `f760003c-1282-4e59-8f8b-54984fb5f943`. It completed on MuMu
  in five attempts with zero reflection: Settings, battery page, visible battery
  facts, and Home all had new verified evidence. Independent Qwen completion
  mapped the four criteria to attempts 2-5 and terminalized the GoalRun.
- Verified result: battery 84%, charging with about one hour remaining, battery
  saver off, then Home. The verified completion promoted SkillMemory version 1
  with assessment provenance only after the gate passed.
- The same five persisted real-device before frames were replayed without ADB.
  Qwen produced 5/5 successful-path decisions in 34.311 seconds. GUI-Owl produced
  3/5 in 6.710 seconds; it failed the already-satisfied finish and system-Home
  cases. Qwen is the accepted current visual default; GUI-Owl is a serial explicit
  diagnostic/rollback binding.
- Live Qwen structured specification probes preserved all four settings clauses
  and all three historical STZB clauses. Goal completion initially exposed a
  reasoning-token truncation; `reasoning_effort=minimal` plus template-level
  thinking disable reduced the verified text-fact merge to a bounded tool call.
- Backend full regression: 757 tests passed with one dependency deprecation
  warning. Frontend: 57 tests passed; TypeScript and production build passed.
- Deployment remains local current-source runtime only. No release artifact,
  external deployment, installer, upgrade, or rollback deployment was performed.
