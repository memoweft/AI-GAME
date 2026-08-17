# Game-learning HTTP contract v1

This contract defines the loopback control-plane adapter for bounded game learning. It extends, but does not weaken, the local-host and write-header rules in [`control-plane-v1.md`](./control-plane-v1.md).

GameLearning is a retained advanced/experimental Interface, not a first-level
browser workspace. Generic one-sentence Android and game objectives use
MobileTask and its automatically resolved SkillMemory scope; named long-lived
applications use ApplicationRuntime Profiles. The `LearningProfile` and
`PolicyMemory` defined here are separate from both MobileTask `SkillMemory` and
the delayed reply-strategy memory owned by the `soul-reply-v1` Application
Profile.

## Truth boundary

- A returned profile means a versioned scope is available; it does not mean its target application is installed, ready, certified, or safe in every visible state.
- `202 Accepted` means a LearningJob request is durable and eligible for bounded background processing. It does not mean an episode started, an action was transported, an outcome was confirmed, or learning succeeded.
- Transport acceptance is not application-level outcome confirmation.
- A completed LearningEpisode does not imply a positive Outcome.
- V1 has no persisted PolicyCandidate or separate validation phase. On confirmed success with at least one positive physical Transition, deterministic trajectory distillation inserts a new immutable PolicyMemory revision and promotes it atomically.
- `result=learned` requires that inline evidence-gated `policy_state=promoted`; transport acceptance, image change, or a GUI-model `terminate` signal is insufficient. A future PolicyCandidate remains inactive until its separately governed validation gate promotes it.
- V1 performs trajectory distillation only. It does not train model weights, create a LoRA artifact, provide realtime control, or prove general competence in 率土之滨.
- A completed MobileTask, even against the same game and with a similarly named SkillMemory scope, is not evidence that a LearningEpisode ran, an Outcome was confirmed under this contract, or GameLearning PolicyMemory was promoted.

## Transport rules

All routes are under `/api/v1` and accept only the console's loopback/local-host deployment. Every state-changing request uses JSON and includes:

```text
X-AI-Game-Client: console-v1
```

Unknown request fields are rejected. Validation and domain errors use a redacted envelope and must not echo the rejected instruction, credentials, raw model output, screenshot bytes, or exact typed text:

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "Safe operator-facing message."
  }
}
```

Timestamps are UTC ISO-8601 strings. JSON field names use `snake_case`. Raw evidence bytes are not returned by this contract.

## Resources

### List profiles

```text
GET /api/v1/learning/profiles
```

Response `200 OK`:

```json
{
  "items": [
    {
      "id": "stzb-tutorial-v1",
      "name": "率土之滨低频教程与菜单导航",
      "game": "率土之滨",
      "scope_summary": "固定测试环境中的低频教程与只读菜单导航。",
      "safety_summary": "登录、协议、实名、验证码、支付、购买、招募、领取、聊天、联盟、匹配、真人交互和账号设置均不在允许范围内。",
      "budget_summary": "每个 LearningEpisode 最多 25 个 Transition，最长 180 秒。",
      "revision": 1,
      "allowed_actions": ["tap", "keyevent", "swipe"],
      "max_transitions": 25,
      "max_duration_seconds": 180.0,
      "default_target_id": null
    }
  ],
  "count": 1
}
```

The values above are the v1 profile manifest exposed by the current adapter. `keyevent` permits only the Android Back operation through the profile adapter; it is not permission for arbitrary key codes. The task compiler may narrow the physical actions further for a particular objective. Callers must still use returned profile data rather than hard-code budgets for later revisions.

### Start a learning job

```text
POST /api/v1/learning/jobs
```

Request:

```json
{
  "instruction": "打开并查看任务列表。",
  "client_request_id": "client-generated-stable-id",
  "profile_id": "stzb-tutorial-v1",
  "target_id": "persisted-android-target-id"
}
```

Fields:

- `instruction` is required, trimmed, non-blank text with an HTTP maximum of 200 characters. It is an episode objective, not permission to exceed the profile. The active `stzb-tutorial-v1` compiler further requires exactly one supported sentence and fails closed on ambiguous, forbidden, multi-sentence, or unsupported objectives.
- `client_request_id` is required, trimmed, at most 128 characters, and contains only ASCII letters, digits, `.`, `_`, `:`, or `-`.
- `profile_id` is optional or `null`; omission selects `stzb-tutorial-v1` in v1.
- `target_id` is optional or `null`; omission delegates selection to the profile/runtime resolver. The transport limits `profile_id` to 128 characters and `target_id` to 256. The request fails safely when there is no unambiguous ready target.

Response `202 Accepted` returns a Job representation. Durable acceptance happens before worker processing.

Idempotency is content-bound. The same `client_request_id` and the same transport-normalized request return the original Job at whatever phase or Result it has reached; a retry response is not forced back to `phase=accepted`. For this comparison, outer whitespace is trimmed and blank optional identifiers become `null`; internal instruction whitespace and the profile compiler's canonical goal are not substituted into the digest. Reuse with different instruction, profile, or target returns `409 Conflict`. An uncertain client must inspect or retry with the same identifier; it must not generate a new identifier merely because the first response was lost.

### List jobs

```text
GET /api/v1/learning/jobs
```

The response is a bounded newest-first envelope:

```json
{
  "items": [],
  "count": 0
}
```

The optional integer query parameter `limit` defaults to `100` and must be between `1` and `500`, inclusive. `items` are newest-first Job representations, and `count` is the number of items in this response rather than a database-wide total. They do not include raw evidence or the Transition ledger.

### Inspect a job

```text
GET /api/v1/learning/jobs/{job_id}
```

Response `200 OK` returns the current durable Job representation defined below. It includes bounded outcome, reward, and PolicyMemory summary fields, but not Transition records or evidence references. `404 Not Found` means no job with that identifier exists.

Inspection is a snapshot, not a subscription. A non-terminal job may change after the response. Clients compare `updated_at`, `phase`, `result`, `cancel_requested`, and terminal timestamps rather than assuming one response is final.

### Stop a job

```text
POST /api/v1/learning/jobs/{job_id}/stop
```

Response `202 Accepted` returns the latest Job representation. The job may first be `phase=stopping`. Repeated stop requests are idempotent and terminal jobs remain terminal.

Stop is cooperative. It prevents later new physical actions after the cancellation check, but it cannot undo an atomic action already accepted by the transport. The terminal result is:

- `stopped` when cooperative stop closes without unresolved physical-action uncertainty;
- `stopped_uncertain` when the module cannot establish a neutral, resolved physical state.

The caller must never automatically resend an uncertain action or restart the job from its last Transition.

## Job representation

The stable semantic fields of a Job are:

```json
{
  "id": "job-id",
  "client_request_id": "client-generated-stable-id",
  "profile_id": "stzb-tutorial-v1",
  "profile_revision": 1,
  "target_id": "persisted-android-target-id",
  "phase": "accepted",
  "result": "pending",
  "outcome": "unknown",
  "control_state": "neutral",
  "policy_state": "unchanged",
  "transition_count": 0,
  "max_transitions": 25,
  "total_reward": null,
  "verified_successes": null,
  "policy_memory_revision": 0,
  "policy_memory_count": 0,
  "cancel_requested": false,
  "detail": null,
  "error_code": null,
  "created_at": "2026-08-09T12:00:00.000Z",
  "started_at": null,
  "finished_at": null,
  "updated_at": "2026-08-09T12:00:00.000Z"
}
```

This is the complete v1 response shape. In particular, it deliberately omits the submitted instruction, raw evidence, evidence paths, and Transition records. `client_request_id` is returned so a client can bind retries and responses without retaining the sensitive instruction. `detail` is a nullable, bounded operator-facing summary rather than an extension object. A future contract version may add separately bounded inspection resources, but must not remove or conflate the semantic axes below.

Summary-field semantics:

- `profile_revision` is captured at durable acceptance and does not change when the active catalog later changes;
- `transition_count` counts finalized Transition records, while `max_transitions` is the accepted profile revision's finite budget;
- `total_reward` and `verified_successes` are `null` before the first finalized Transition, then report cumulative verifier-derived reward and confirmed task-success count;
- `policy_memory_revision` and `policy_memory_count` identify the profile-scoped PolicyMemory snapshot captured when the job was accepted, or the newly promoted snapshot when `policy_state=promoted`; revision/count `0` means there was no prior PolicyMemory;
- `cancel_requested=true` records a durable cooperative-stop request; it is not by itself a terminal result;
- `detail` is human-facing and may change wording, while `error_code` is the stable machine-readable diagnostic.

### Phase values

- `accepted`
- `preflight`
- `collecting`
- `distilling`
- `validating`
- `stopping`
- `terminal`

`preflight`, `distilling`, and `validating` are reserved processing positions that v1 clients must accept. The first implementation folds target/environment checks into the claimed worker before physical input and may expose that worker as `collecting` without a separately observed `preflight` response. It also performs no separately observable asynchronous work in `distilling` or `validating`. None of these phase names asserts weight training.

### Result values

- `pending`
- `learned`
- `not_learned`
- `failed`
- `stopped`
- `stopped_uncertain`

Only a non-terminal job may use `pending`. `learned` requires an evidence-gated promoted PolicyMemory version. `not_learned` is a valid completion without promotion, not an operational failure.

### Outcome values

- `unknown`
- `confirmed_success`
- `confirmed_failure`
- `unconfirmed`

`confirmed_success` and `confirmed_failure` require admissible verifier evidence. `unconfirmed` is mandatory when evidence exists but cannot decide the frozen postcondition; it must not be coerced to success.

### Policy-state values

- `unchanged`
- `candidate`
- `promoted`
- `rejected`

The first implementation emits only `unchanged` or `promoted`. It does not persist a candidate: when the evidence and positive-Transition gates pass, it inserts an immutable PolicyMemory revision and promotes it in the same transaction. `candidate` and `rejected` are reserved v1 values for a later separately inspectable candidate-validation workflow; any such future candidate is inactive until separately promoted and remains inactive across restart recovery.

### Control-state values

- `neutral`
- `active`
- `neutralizing`
- `uncertain`

`control_state` reports the module's current knowledge about physical-control activity. It is independent of the learning Result: `neutralizing` accompanies cooperative stopping, while `uncertain` requires operator attention and must not trigger automatic replay.

## Transition and evidence semantics

A Transition is an append-only, monotonically ordered ledger fact for one job. V1 persists Transition facts internally and exposes only `transition_count` through HTTP. The local ledger may contain bounded, sanitized fields such as:

- ordinal and stable identifier;
- observation and post-action evidence hashes or relative references;
- action classification without exact sensitive text;
- transport disposition;
- verifier classification and stable reason code;
- UTC and monotonic timing metadata.

The following are forbidden in responses and diagnostic error payloads:

- screenshot bytes;
- API keys or credentials;
- exact typed secrets;
- raw model response bodies;
- arbitrary ADB commands;
- absolute evidence filesystem paths.

Evidence bytes are local runtime artifacts under `runtime/sessions/game-learning/`. `learning.db` stores only bounded metadata, hashes, and relative references.

## Persistence and recovery

Learning state is stored in `settings.data_dir / "learning.db"`, which resolves to `runtime/console/learning.db` in the default production layout. This is independent of `runtime/console/console.db`.

On shutdown or restart, the implementation does not replay any Transition or physical action. Persisted non-terminal jobs are finalized or moved through explicit stopping/recovery semantics. They do not resume at the previous Transition ordinal. V1 has no persisted PolicyCandidate; a future candidate workflow must not promote one during recovery.

## Profile boundary: `stzb-tutorial-v1`

The profile is restricted to a fixed, user-authorized, already logged-in tutorial context on a selected Android target. It is for low-frequency, bounded trajectory and evidence collection only.

Revision 1 binds the foreground package `com.netease.stzb.netease`, at most 25 Transitions, and at most 180 seconds. Its supported objective families are: advance one current tutorial step; open and only view the task, general, army, or map screen; or return to the verified main scene. Physical input is limited to tap, Android Back, and at most two controlled vertical swipes only for the three list-view objectives that permit swipe.

It excludes credentials, OTP, identity verification, payments, public chat, alliance/social actions, attacks or material effects on other players, public competition, account farming, anti-cheat evasion, and any scene or consequence not covered by the frozen verifier. Leaving the recognized scope yields an unconfirmed or failed result and no promotion.

The profile does not claim that AI-GAME can generally play 率土之滨, support arbitrary accounts or seasons, or satisfy realtime gameplay gates.

## Future compatibility

A future `TrainingJob` and LoRA Adapter require a new, separately governed contract. They are not implicit extensions of `POST /learning/jobs`; v1 clients must not interpret a LearningJob as weight training or expect a model artifact in its response. A future weight candidate remains inactive until separate evaluation and explicit adapter activation; v1's inline PolicyMemory promotion never auto-activates model weights.
