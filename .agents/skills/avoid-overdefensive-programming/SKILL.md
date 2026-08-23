---
name: avoid-overdefensive-programming
description: Prevent evidence-free guards, fallbacks, retries, compatibility branches, defaults, feature flags, and future-risk abstractions while implementing or reviewing AI-GAME. Use when a change handles failures or adds defensive behavior; do not use it to remove an existing product or architecture safeguard merely because it is defensive.
---

# Avoid Over-Defensive Programming

[constraint-source: USER_DECISION; ref: repository governance request 2026-08-23]

Keep AI-GAME changes evidence-based and failure-visible. Fix the earliest
confirmed cause instead of making an unhealthy path appear successful.

## Workflow

1. Inspect the affected call path, current behavior, tests, and relevant
   product contract.
2. Identify the evidence for the change: a reproduced failure, a documented
   contract, an existing architecture invariant, a security boundary, or a
   focused test for the valid case.
3. Repair the earliest incorrect state. Do not patch downstream code with a
   default merely to hide the upstream defect.
4. For every new guard, `try/catch`, null/default branch, retry, fallback,
   compatibility branch, preflight, feature flag, or degradation path, state
   which evidence requires it and keep its scope no broader than that evidence.
5. Verify that invalid states remain observable and that success, completion,
   ownership, or authoritative data is not fabricated.

If the evidence is only a hypothetical future risk, do not add the defensive
behavior. Record a non-blocking observation if it is useful.

## Valid defenses

This skill does not weaken safeguards already required by the user, the
canonical product documents, architecture invariants, security boundaries, or
reproduced failures. Examples include input and schema validation, ownership,
user stop, revision/current-observation fences, fresh verification, no
uncertain replay, bounded recovery, timeouts, transactions, and an explicitly
designed rollback or compatibility path.

Do not remove or narrow an existing safeguard just because this skill is
active. If an existing safeguard seems obsolete or contradictory, audit it
with `doc-drift` and preserve it until its authority and evidence are resolved.

## Unsupported patterns

Without the evidence above, do not add:

- silent exception swallowing or failure-to-success conversion;
- empty/default results that change required-state semantics;
- retries for deterministic validation, authorization, or invariant failures;
- a second source of truth, implicit dual write, or legacy-store fallback;
- compatibility for unknown clients or hypothetical old behavior;
- configuration switches, abstractions, or preflights for imagined future use;
- a broad refactor while repairing one bounded failure.

When a new defensive behavior also creates a rule, gate, blocker, or
precondition, apply `constraint-governance` and label its source there. A
single failure does not establish a permanent prohibition.

## Completion note

Report only material defensive behavior added by the change, with its evidence
and focused verification. If none was added, say so briefly. Do not impose a
new project-wide report template.
