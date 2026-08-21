# Universal Goal API v2

Status: U1 compatibility contract (2026-08-20)

U2 additive preflight revision: 2026-08-20

U3 additive completion-gate revision: 2026-08-21

## Boundary

`/api/v2/goals` is the single ordinary-language ingress for a durable
`GoalRun`. U1 supports finite Android goals through the explicit
`mobile_task_compat` binding only. It does not classify across runtimes, select
devices, or independently verify original-goal completion.

Every POST requires the existing local-console header:

```http
X-AI-Game-Client: console-v1
```

Unknown request fields are rejected. In particular, the ordinary create body
cannot contain a device, serial, runtime, profile, model, skill, or owner id.

## Create and read

```http
POST /api/v2/goals
Content-Type: application/json

{
  "goal": "打开设置，查看当前可见的电池信息，告诉我，然后返回桌面。",
  "idempotency_key": "settings-battery-20260820-1"
}
```

The response is `202 Accepted`. `idempotency_key` is durable and is bound to
the normalized create payload. Repeating the same key and payload returns the
same GoalRun and cannot create a second MobileTask. Reusing the key for a
different normalized payload returns `409 goal_idempotency_conflict`.

```http
GET /api/v2/goals?limit=100
GET /api/v2/goals/{goal_id}
```

The projection contains:

- immutable `original_goal`;
- revisioned `goal_specification`; U3 freezes complete criteria and exact
  original-goal source quotes before a new compatibility task is bound;
- separate `execution_status` and `control_state`;
- explicit compatibility binding kind, state, target, and MobileTask id;
- waiting/error/result fields;
- timestamps, independent completion assessment/history, verified facts, and
  remaining criteria.
- `environment_state` with the current preflight state, redacted capability
  facts, selected target, and any plain-language target options.

U1 state projection is deliberately one-way and cannot strengthen evidence:

| MobileTask | GoalRun execution | GoalRun control |
|---|---|---|
| `queued` | `ACCEPTED` | `AUTOMATED` |
| `planning` | `PLANNING` | `AUTOMATED` |
| `running` | `RUNNING` | `AUTOMATED` |
| `stopping` | `RUNNING` | `STOP_REQUESTED` |
| `completed` | `CANDIDATE_COMPLETE` | `AUTOMATED` |
| `failed` | `FAILED` | `AUTOMATED` |
| `stopped` | `CANCELLED` | `AUTOMATED` |
| `uncertain` | `UNCERTAIN` | `AUTOMATED` |

`CANDIDATE_COMPLETE` is non-terminal. It means only that the compatibility
executor finished its own generated plan. U3 must independently map frozen
original success criteria to verified facts before the GoalRun can become
`COMPLETED`. The runtime accepts `verified` only when every frozen criterion is
present exactly once, every satisfied criterion maps to an existing generated
Stage, and every evidence reference names a persisted satisfied/non-uncertain
ActionAttempt. Model prose or a fabricated sequence cannot set terminal state.

`completion_assessment` is the latest verdict and `completion_history` is an
append-only retry ledger. A failed or malformed assessment remains
`partial`/`uncertain`; an explicit retry appends a new revision instead of
overwriting it:

```http
POST /api/v2/goals/{goal_id}/completion/retry
```

Only a persisted `verified` assessment may promote the quarantined v2
SkillMemory. The memory records the GoalRun id and completion-assessment
revision. Partial, uncertain, invalid-reference, and shortened-plan outcomes
remain unlearned.

If no default serial is configured, the compatibility runtime is unavailable,
or the currently configured executor probe is not ready, create returns the
durable GoalRun in `WAITING_CONFIGURATION`. It creates no MobileTask in this
case and U1 never guesses another device.

Under U2, current-source production composition runs a fresh read-only model
and ADB assessment before binding. A sole ready idle Android target is selected
automatically. An explicitly configured ready deployment serial wins over
other discovered candidates. Busy, stale, non-Android, offline, and
unauthorized targets cannot be selected. Multiple materially valid targets
produce `WAITING_EXTERNAL/TARGET_SELECTION_REQUIRED` instead of a guess.

Preflight can be repeated on the same GoalRun after the environment changes:

```http
POST /api/v2/goals/{goal_id}/preflight/retry
```

When a multiple-target gate is active, the user may answer that gate without
changing the original goal:

```http
POST /api/v2/goals/{goal_id}/preflight/selection

{
  "target_id": "adb:127.0.0.1:16384"
}
```

Selection is revalidated against a fresh preflight. A stale option returns
`409 goal_state_conflict`; an already bound GoalRun cannot silently switch
devices.

## Messages

```http
POST /api/v2/goals/{goal_id}/messages

{
  "content": "完成后一定返回桌面。",
  "idempotency_key": "goal-message-1"
}
```

A message is a revisioned constraint/input associated with the same GoalRun.
The original goal is never overwritten. The facade derives a stable MobileTask
input request id so a retry cannot duplicate the compatibility input.

## Controls

```http
POST /api/v2/goals/{goal_id}/controls

{
  "action": "stop",
  "idempotency_key": "goal-stop-1"
}
```

U1 truthfully supports `stop` only. `pause`, `resume`, and `takeover` return
`409 goal_control_unsupported`; they never return fake success. A stop request
is projected as `STOP_REQUESTED` only after MobileTask acknowledges `stopping`.
It becomes `CANCELLED` only when MobileTask settles `stopped`.

## Events

```http
GET /api/v2/goals/{goal_id}/events?after=0&limit=100
```

Events use a durable, monotonically increasing integer `cursor`. The response
contains `items`, `count`, and `next_cursor`. Goal-native events and redacted
MobileTask compatibility events share this cursor. Compatibility events expose
the source sequence and event type, not raw action arguments or private model
content.

## Stable errors

| HTTP | Code | Meaning |
|---:|---|---|
| 403 | `console_client_required` | Missing local write-client header |
| 404 | `goal_not_found` | GoalRun does not exist |
| 409 | `goal_idempotency_conflict` | Same key, different operation payload |
| 409 | `goal_control_unsupported` | Binding cannot implement the control |
| 409 | `goal_state_conflict` | GoalRun state cannot accept the operation |
| 422 | `invalid_goal_request` | Invalid or extra request input; content is redacted |

## Persistence and rollback

GoalRun truth is stored additively in `runtime/console/goals.db`. The
compatibility task remains in `mobile-tasks.db`. Disabling the v2 router leaves
both histories intact and does not change any `/api/v1` contract.
