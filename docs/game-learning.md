# Bounded game learning

## Current conclusion

The primary one-sentence path for Android and game objectives is now
MobileTask: it owns a durable TaskPlan/Subgoal loop, accepts mid-task Input,
uses Planner/Verifier/Reflection roles, and may resolve an automatic
`auto:*` SkillMemory scope. This document instead describes the retained
advanced/experimental GameLearner Interface. GameLearner accepts a bounded
`LearningJob`, collects one finite `LearningEpisode`, appends a `Transition`
ledger, retains local evidence, classifies the outcome through an
evidence-gated `OutcomeVerifier`, derives a `RewardSignal`, and distills
eligible positive Transitions into versioned `PolicyMemory`.

It does **not** train or fine-tune model weights, implement a realtime combat controller, add continuous state estimation or multi-touch, prove a game objective from an ADB return code, or establish that AI-GAME “already knows how to play 率土之滨.” The `stzb-tutorial-v1` profile is a narrow compatibility and safety scope, not a certification, endorsement, or general game-playing claim.

The project uses the canonical terms in [`CONTEXT.md`](../CONTEXT.md). In particular, a `LearningJob`, its `Outcome`, its terminal `Result`, and its policy state are four different facts.

Three similarly named learning stores remain deliberately separate:

- GameLearner `PolicyMemory` is the bounded Transition/Outcome trajectory
  distillation described in this document;
- MobileTask `SkillMemory` belongs to the generic long-task runtime and is
  promoted only through its own verifier/evidence gate;
- Soul reply-strategy memory belongs to the `soul-reply-v1`
  ApplicationRuntime Profile and changes only from admissible delayed
  interaction evidence, not from the current send alone.

Accordingly, GameLearning Profile `stzb-tutorial-v1` and MobileTask automatic
scope `auto:stzb/tutorial/v1` are different persisted objects, state machines,
and acceptance results despite their similar names. ApplicationRuntime is a
sibling Module for named long-lived applications; Soul reply learning is not a
GameLearner job.

## One-sentence interface

`GameLearner.start()` and `shutdown()` own lifecycle; `list_profiles()`, `learn()`, `list_jobs()`, `inspect()`, and `stop()` are the complete caller interface, while screenshots, bounded episode execution, Transition persistence, verification, reward derivation, distillation, and PolicyMemory promotion remain inside the module.

The interface is deliberately small:

- `start()` initializes recovery and any bounded background worker;
- `shutdown()` cooperatively stops owned work without replaying physical actions;
- `list_profiles()` returns the frozen learning scopes available to callers;
- `learn(...)` durably accepts one idempotent job request;
- `list_jobs(limit=100)` returns newest-first durable job summaries with an allowed limit of 1–500;
- `inspect(job_id)` returns the current durable job snapshot;
- `stop(job_id)` requests cooperative termination and returns the latest durable state.

HTTP schemas are adapters for this interface, not the learning domain itself. The browser, tests, and future typed callers should not orchestrate screenshots, verifier calls, rewards, distillation, or PolicyMemory promotion directly.

## First-version flow

```text
learn(request)
    |
    v
LearningJob: accepted
    |
    v
profile and target preflight
    |
    v
one bounded LearningEpisode
    |
    +--> append-only Transition ledger
    |       observation evidence reference/hash
    |       proposed atomic action classification
    |       transport disposition
    |       post-action evidence reference/hash
    |
    v
OutcomeVerifier
    |
    +--> unknown / confirmed_success / confirmed_failure / unconfirmed
    |
    v
RewardSignal
    |
    +--> not confirmed_success --> continue within budget or terminal without promotion
    |
    `--> confirmed_success
             |
             v
        deterministic trajectory distillation
             |
             +--> no eligible positive physical Transition --> unchanged / not_learned
             |
             `--> eligible positive physical Transition
                      |
                      v
                 insert immutable PolicyMemory revision
                 and promote it atomically --> promoted / learned
    |
    v
LearningJob: terminal
```

The `preflight`, `distilling`, and `validating` phase names are reserved by the v1 contract so later implementations can expose those positions without changing vocabulary. The first build performs target/environment checks inside the claimed worker before physical input and may therefore move externally from `accepted` directly to `collecting`. It also performs its evidence check, trajectory distillation, and eligible PolicyMemory promotion inline, so it may move directly from `collecting` to `terminal` and does not expose a separately durable candidate-validation phase. These reserved names are not a claim that model training or a sophisticated evaluation pipeline already runs.

### Bounded means finite

Every LearningProfile must define finite action and duration budgets. Reaching a budget terminates collection without inventing a positive outcome. An episode cannot silently fall back to the unbounded production chat loop.

The first version remains low-frequency and sequential:

```text
fresh screenshot
→ one local GUI proposal
→ cancellation and profile checks
→ at most one restricted ADB action
→ fresh post-action screenshot
→ append Transition facts
→ repeat within the frozen budget
```

This architecture is suitable for evidence-backed tutorial navigation and trajectory collection. It is not the 20 Hz state-estimation and multi-pointer control architecture required for realtime gameplay.

### Android environment Adapter

Production composition injects one `StzbAndroidEnvironmentFactory` behind the `GameLearner` environment seam. It reuses the configured Android executor and the loopback local GUI-model client, but the LearningProfile compiler narrows the accepted instruction and action vocabulary before any device input. A session exposes only fresh observation, one proposal, one permitted atomic execution, its profile verifier, and close; the HTTP layer cannot call those operations directly.

When the required executor, target, or local model configuration is unavailable, profile discovery may still describe `stzb-tutorial-v1`, but opening an episode fails closed. A listed profile is therefore a contract/scope fact, not evidence that the Android environment is currently ready. The module does not silently fall back to the unbounded production chat loop or to arbitrary ADB commands.

## Four independent state axes

The API and database must keep the following axes separate.

### Phase

`phase` reports where processing currently is:

- `accepted`: the idempotent request is durable but work has not begun;
- `preflight`: profile, target, evidence paths, and runtime prerequisites are being checked;
- `collecting`: the bounded LearningEpisode is observing and may transport permitted atomic actions;
- `distilling`: reserved for trajectory-to-policy reduction;
- `validating`: reserved for candidate evaluation before promotion;
- `stopping`: a cooperative stop has been requested and no new work should be admitted;
- `terminal`: no further processing is scheduled for this job.

### Result

`result` reports the terminal learning disposition:

- `pending`: the job is not terminal;
- `learned`: admissible evidence and at least one positive physical Transition produced an atomically promoted PolicyMemory version;
- `not_learned`: the job completed without an operational failure but did not promote new policy knowledge;
- `failed`: the job could not complete because of a stable operational or contract failure;
- `stopped`: cooperative stop completed with no unresolved physical-action uncertainty reported by the module;
- `stopped_uncertain`: processing stopped, but an already transported or in-flight physical action could not be resolved confidently.

`not_learned` is a valid, informative result. It must not be rewritten as `learned` merely to make a run appear successful.

### Outcome

`outcome` reports what the evidence supports about the episode objective:

- `unknown`: verification has not run or no admissible postcondition is available yet;
- `confirmed_success`: the profile's verifier observed its frozen success postcondition;
- `confirmed_failure`: the verifier observed a frozen failure postcondition;
- `unconfirmed`: collection ended, but the available evidence cannot support success or failure.

Operational `failed` and evidence `confirmed_failure` are not synonyms. A healthy job may obtain `confirmed_failure`; a crashed verifier may produce `failed` with an `unknown` or `unconfirmed` outcome.

### Policy state

`policy_state` reports what happened to reusable PolicyMemory:

- `unchanged`: no new PolicyMemory version became active;
- `candidate`: reserved for a future immutable candidate that exists but is not active;
- `promoted`: a new PolicyMemory version became active through the v1 inline evidence gate, or a future validated candidate became active;
- `rejected`: reserved for a future candidate that was evaluated and not promoted.

The first build does not create or validate a persisted PolicyCandidate. After a confirmed-success episode, deterministic trajectory distillation selects only positive, confirmed physical Transitions; if at least one exists, inserting a new immutable PolicyMemory revision and making it active are one evidence-gated transaction. This automatic inline promotion is the v1 self-reinforcement behavior, and only `promoted` can support `result=learned`.

`candidate` and `rejected` are reserved contract values for a future implementation that makes an inactive candidate separately inspectable. Such a future candidate must never become active merely because it exists, an ADB command returned zero, the image changed, or the GUI model said `terminate`; it requires its separately governed validation gate.

## Evidence and truth boundaries

### Transport accepted is not outcome confirmed

`transport accepted` means only that the device adapter accepted an atomic request. It does not establish that the intended control was hit, the UI changed in the expected way, a tutorial objective completed, or policy knowledge is correct.

The OutcomeVerifier may use only evidence permitted by the selected profile. A changed screenshot alone is not automatically a success postcondition. Missing, stale, corrupt, ambiguous, or out-of-scope evidence yields `unknown` or `unconfirmed`, never an inferred success.

Reward is downstream of verification. A positive `RewardSignal` requires an evidence-gated positive classification; transport acceptance, proposal count, episode duration, or model confidence cannot substitute for it. Reward remains a local learning input, not a gameplay score or model loss.

### Transition ledger

The Transition ledger is append-only for a job. Each record preserves ordering and enough sanitized provenance to distinguish observation, proposal, transport, post-action observation, and verification. It must not contain API keys, credentials, exact typed secrets, raw model responses, or an unredacted instruction copied into diagnostic text.

The ledger is evidence metadata, not a frame store. Large bytes live in the evidence directory; SQLite stores relative references, hashes, dimensions, classifications, and other bounded metadata.

## Persistence and local evidence

Game learning owns storage separately from the control-plane database:

```text
runtime/console/console.db             existing control-plane/chat state
runtime/console/learning.db            game-learning jobs, transitions, outcomes,
                                       rewards and PolicyMemory metadata
runtime/sessions/game-learning/        local episode evidence and derived artifacts
```

The production paths resolve as:

- learning database: `settings.data_dir / "learning.db"`;
- evidence root: `settings.project_root / "runtime" / "sessions" / "game-learning"`.

The separate database keeps learning schema evolution and write volume from changing the established `console.db` contract. It does not turn learning into a separate network service: the same loopback control-plane process owns the module and its lifecycle.

Evidence remains local. The game-learning module does not add a route that returns raw screenshots and does not upload them to a cloud planner. The v1 HTTP Job shape exposes bounded summary fields but no Transition records, evidence references, or absolute paths. Files under the evidence root are runtime artifacts, not source-controlled fixtures or publication evidence.

## Idempotency, stop, shutdown, and restart

The rules in this section apply to GameLearner jobs. ApplicationRuntime and
MobileTask have their own persistence and no-replay contracts.

`learn()` is content-bound by `client_request_id`. Retrying the same request identifier with the same normalized request returns the original job; reusing it for different content is a conflict. Durable acceptance precedes background work.

`stop()` is cooperative. It prevents later new actions after the stop check, but it cannot undo an atomic input already accepted by ADB. The job may first report `phase=stopping`; it becomes `stopped` only when the module can close without unresolved physical uncertainty. Otherwise it must end as `stopped_uncertain`.

On process shutdown or restart:

- no Transition, proposed action, or uncertain physical operation is replayed;
- non-terminal persisted jobs are recovered into an explicit terminal or stopping disposition rather than resumed from the last ordinal;
- v1 has no persisted PolicyCandidate; if a future version adds one, recovery must not promote it merely because it was found on disk;
- evidence already written remains inspectable, subject to the local retention policy.

These rules intentionally sacrifice automatic continuation in favor of physical-action certainty and auditability.

## `stzb-tutorial-v1` profile boundary

`stzb-tutorial-v1` is the first named LearningProfile. “STZB” identifies the profile's compatibility target; it does not mean AI-GAME is affiliated with, certified for, or generally capable in 率土之滨.

The safe scope is narrow:

- a user-owned, explicitly selected Android target;
- a fixed, already logged-in tutorial or other pre-authorized low-consequence starting state;
- the exact `com.netease.stzb.netease` foreground package, a recognized low-risk scene, and verifier confidence at or above the frozen profile threshold before proposing a physical action;
- exactly one supported sentence of 1–200 characters for continuing one tutorial step, opening and viewing the task, general, army, or map screen, or returning to the verified main scene;
- low-frequency observation, tap, Android Back, and profile-controlled swipe where that objective permits it, followed by post-action evidence collection and bounded stop behavior;
- at most 25 Transitions and 180 seconds for profile revision 1;
- no extrapolation from one tutorial trajectory to arbitrary maps, accounts, seasons, devices, or public competitive play.

Out of scope includes login or account recovery, credentials and OTP, identity verification, purchases or premium currency, public chat, alliance/social actions, attacking or materially affecting other players, rankings or public competition, account farming, evasion of anti-cheat or platform controls, and any action whose consequence is not covered by the frozen verifier.

The profile name is not itself a visual safety classifier. Current preflight checks the bound target/executor, foreground package, assessed scene, unsafe/failure markers, and verifier confidence; it does not prove device ownership, account authorization, or broad compatibility across app versions, seasons, orientations, resolutions, and UI scales. The operator must control those environmental facts. If the runtime cannot establish the expected starting state or a later observation leaves the recognized tutorial scope, it must not award success or promote memory.

## HTTP adapter

The loopback API uses these v1 routes:

- `GET /api/v1/learning/profiles`;
- `POST /api/v1/learning/jobs`;
- `GET /api/v1/learning/jobs`;
- `GET /api/v1/learning/jobs/{job_id}`;
- `POST /api/v1/learning/jobs/{job_id}/stop`.

The full request, response, status, idempotency, and error contract is in [`contracts/game-learning-v1.md`](../contracts/game-learning-v1.md). State-changing requests retain the console write header and loopback-only host policy.

## Future `TrainingJob` and LoRA Adapter

Weight training is intentionally outside v1. A future `TrainingJob` would consume a frozen, reviewed dataset derived from eligible evidence and labels. It would be separately authorized, separately scheduled, resource-bounded, versioned, and unable to execute device actions. Its completion would be training-artifact evidence, not gameplay acceptance.

A future LoRA Adapter would load an explicitly selected, versioned weight artifact behind a model seam. A produced weight candidate would remain inactive until separate artifact evaluation and explicit adapter activation; the v1 inline PolicyMemory promotion rule does not apply to model weights. It would not overwrite the base model or become active merely because a LearningJob or TrainingJob ended. Dataset approval, training, artifact evaluation, model-adapter activation, live runtime loading, and device acceptance would remain separate state transitions.

## Evidence levels and current runtime

Keep these claims separate in every handoff:

1. documentation or code exists;
2. automated tests pass against isolated fixtures;
3. the running console has loaded the new code and storage schema;
4. a real device LearningEpisode ran;
5. its OutcomeVerifier confirmed the frozen objective;
6. an eligible trajectory was evidence-gated and its new PolicyMemory version was promoted (or, in a future explicit-candidate workflow, a PolicyCandidate was separately validated and promoted);
7. broader gameplay readiness gates passed.

A supplied live Android runtime record establishes that MobileTask
`daac81a7-1af9-47e3-9566-66e73509a0fd` completed after 23 ActionAttempts under
automatic SkillMemory scope `auto:stzb/tutorial/v1`. Its local evidence includes
`runtime/sessions/mobile-tasks/evidence/ec5f551bfb124b0d8739a16fcba76fac.png`.
This is one task-specific MobileTask result. It is **not** evidence that this
contract's `LearningEpisode` ran, that its `OutcomeVerifier` confirmed an
Outcome, that GameLearning `PolicyMemory` was promoted, or that AI-GAME has
general 率土之滨 or realtime-game capability. The primary browser navigation is
one-sentence start, Soul, Device, and Settings; GameLearning remains an
advanced/compatibility API rather than a first-level Learning workspace.
