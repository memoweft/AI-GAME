# Experience Ledger v1 additive contract

Status: implemented by U4 on 2026-08-21.

## Boundary

The canonical experience ledger is additive. It does not delete, merge, or
reinterpret the owner truth in `mobile-tasks.db`, `learning.db`, or legacy
SkillMemory. Compatibility sources are imported only through explicit adapters
with provenance and Goal-coverage checks.

## Durable lineage

```text
GoalRun + frozen GoalSpecification
-> ExperienceEpisode
-> SceneState before
-> ActionTransition
-> SceneState after
-> OutcomeSignal
-> ExperienceCandidate
-> verified Goal coverage
-> immutable PolicyRevision
-> Retrieval
-> actual Usage
-> CandidateTrial
```

Required invariants:

- a physical transition references its source task attempt exactly once;
- uncertainty may be recorded but never creates positive or permanent negative
  policy;
- coordinates and typed text are audit-only and never become a replay macro;
- promotion requires a real transition, admissible signal/evidence, frozen
  criteria, verified episode terminal outcome and exact isolation scope;
- retrieval requires compatible user/account/application/goal/UI/device scope,
  a comparable objective and a semantic or perceptually comparable current
  scene;
- every retrieval and actual/avoided rule use is separately attributable;
- a failed trial changes support/failure confidence without deleting history;
- reject and deprecate remove a candidate from active retrieval;
- rollback creates a new policy head referencing a prior candidate set and does
  not delete episodes or older revisions;
- legacy SkillMemory without verified Goal completion remains `untrusted`;
- selected GameLearning facts require finalized before/action/transport/after/
  outcome provenance and an explicit GoalRun/specification mapping.

## Rollback

Experience retrieval/promotion can be disabled while preserving the append-only
database. MobileTask compatibility execution then continues without policy
packets; no old database is rewritten.
