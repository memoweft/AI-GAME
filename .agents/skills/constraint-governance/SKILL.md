---
name: constraint-governance
description: Govern any new or broadened AI-GAME MUST, MUST NOT, blocker, gate, precondition, prohibition, required step, or failure-derived restriction. Use when editing instructions, product documents, work orders, acceptance rules, runtime guards, or handoffs so temporary judgments cannot become permanent product constraints.
---

# Constraint Governance

[constraint-source: USER_DECISION; ref: repository governance request 2026-08-23]

Prevent the allowed behavior set from shrinking through unsupported Agent-made
rules. This skill does not supersede the user's current instruction, existing
canonical product decisions, architecture invariants, or security boundaries.

## Source every new constraint

Before adding or materially broadening restrictive language such as `MUST`,
`MUST NOT`, `BLOCK`, `REQUIRE`, `gate`, `precondition`, `禁止`, `不得`, or `必须`,
identify exactly one source class:

- `USER_DECISION`: the user explicitly chose the behavior;
- `PRODUCT_SPEC`: a current canonical product requirement;
- `ARCH_INVARIANT`: violating it breaks a current confirmed system invariant;
- `REPRODUCED_FAILURE`: a real reproduced failure proves a scoped safeguard is
  needed;
- `TEMPORARY`: a bounded incident or runtime judgment that is not settled
  product policy.

Use a compact adjacent annotation:

```text
[constraint-source: USER_DECISION; ref: D16]
[constraint-source: PRODUCT_SPEC; ref: docs/product/01_PRODUCT_SPEC.md section 4]
[constraint-source: ARCH_INVARIANT; ref: docs/product/03_TARGET_ARCHITECTURE.md section 6]
[constraint-source: REPRODUCED_FAILURE; evidence: test or runtime evidence reference]
```

A `TEMPORARY` constraint requires all lifecycle fields:

```text
[constraint-source: TEMPORARY; scope: bounded affected path or run;
 evidence: exact current failure or observation;
 remove-when: expiry date, successful verification, or other objective condition]
```

Keep the annotation next to the rule so later agents do not have to infer its
authority. Reuse an existing cited rule instead of copying it into another
authority file.

## Lifecycle

1. Check whether the proposed restriction already exists in current authority.
2. Keep a settled rule no broader than its cited source.
3. Put an incident stop or workaround under `TEMPORARY`; keep it in the scoped
   work note, runtime state, or handoff, not as a settled rule in permanent
   product authority.
4. When `remove-when` becomes true, remove the temporary restriction. Do not
   carry it into later work orders or summarize it as a permanent prerequisite.
5. If no valid source exists, classify the proposal as
   `unsupported_constraint`. Do not codify it. Remove a line added in the
   current change, or downgrade it to a clearly non-blocking hypothesis.
6. For a pre-existing possibly unsupported rule, preserve user work during the
   audit and report it through `doc-drift`; edit it only within the user's
   authorized scope.

An Agent cannot promote `TEMPORARY` or its own inference to `USER_DECISION`,
`PRODUCT_SPEC`, or `ARCH_INVARIANT`. Only new user evidence or an authorized
change to the relevant authority can do that.

## Failure behavior

One failure proves only that a path failed under the reproduced conditions.
Default response:

```text
reproduce -> fix the earliest cause -> verify -> retry the same intended path
```

Do not infer from one failure that the capability is globally forbidden, that
all later stages need a new gate, or that every related component requires a
fallback/preflight. A persistent safeguard may use `REPRODUCED_FAILURE` only
when its scope and verification match the evidence. Use
`avoid-overdefensive-programming` when the proposed constraint is implemented
as a guard, retry, fallback, compatibility branch, default, or feature flag.

## Blockers and acceptance

Do not label difficult, incomplete, risky-in-the-abstract, or not-yet-polished
work as `BLOCKED`. Preserve the status semantics and evidence levels already
defined by the product protocol. Missing evidence may keep a specific
acceptance claim incomplete without automatically blocking unrelated roadmap
work; roadmap coupling needs its own valid source.

This governance never authorizes lowering acceptance criteria or deleting a
real invariant. It requires the restriction and its scope to be traceable.

## Review before handoff

Inspect the current diff for added or broadened constraint language. For each
case, confirm its adjacent source annotation, scope, and—when temporary—removal
condition. Remove or downgrade `unsupported_constraint` additions before
handoff, and mention any unresolved pre-existing finding without upgrading it
to a blocker.
