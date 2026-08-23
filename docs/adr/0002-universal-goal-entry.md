# ADR 0002: One universal goal entry with capability routing

## Status

Accepted by product direction on 2026-08-20. The decision remains accepted;
implementation-status text below is a decision-time snapshot. Current progress
is maintained in `../product/02_CURRENT_STATE.md` and
`../product/06_IMPLEMENTATION_ROADMAP.md`.

## Context

At decision time, AI-GAME exposed separate MobileTask,
ApplicationRuntime/Soul, GameLearning, Chat, Gateway, device, and settings
concepts. The existing MobileTask loop could operate Android from natural
language, while other domains provided useful long-lived, learning, or contract
primitives. A normal user should not need to identify which internal module
applies before stating a purpose.

The desired product is a universal local phone operator: the user gives an outcome; the platform discovers and configures capabilities, plans, acts, verifies, recovers, learns, and continues.

## Decision

Introduce one durable user-visible `GoalRun` and universal goal API.

The GoalRun persists the original goal, revisioned interpretation, success criteria, execution binding, progress, verified facts, waiting/external state, and result. A Qwen-class local orchestrator selects capabilities through a registry and environment manager. Internal modules remain adapters with their real semantics; they are not top-level products.

Migration is incremental:

1. v2 GoalRun initially binds supported phone goals to the current MobileTask engine.
2. Automatic preflight and one product composer are added.
3. Independent original-goal verification and canonical experience learning are added.
4. The full proven loop is migrated to RuntimeKernel canary and then cut over.
5. Long-lived ApplicationRuntime/Soul capabilities are routed through the same ingress later.

Profiles remain internal bundles of capability, application knowledge, verifier, and ownership. They cannot narrow the user's original goal silently or become mandatory user choices.

## Consequences

- The old four/five-entry product information architecture is superseded.
- Existing v1 contracts remain compatibility contracts until migrated.
- A facade alone is not a universal autonomous platform; evidence must follow the U1-U9 gates.
- MobileTask, GameLearning, ApplicationRuntime, and specialized owners do not need an unsafe all-at-once data-model merge.
- One physical target still has one authoritative owner.
- A classification decision cannot itself send a physical action.
- Goal completion and learned success require independent evidence against the original purpose.

## Supersedes

This ADR supersedes only the user-facing multi-entry decision in ADR 0001 and older design documents. It does not supersede ADR 0001's external Soul owner boundary or the principle that AI-GAME remains application-general.

## Authoritative details

See [`../product/00_INDEX.md`](../product/00_INDEX.md) and the work orders under [`../product/work-orders/`](../product/work-orders/README.md).
