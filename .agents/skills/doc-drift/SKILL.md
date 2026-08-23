---
name: doc-drift
description: Audit AI-GAME AGENTS.md, .agents skills, canonical product documents, work orders, and their referenced instruction files for outdated, conflicting, ambiguous, duplicated, or unsupported constraints. Use for rule drift, context drift, documentation audits, or after governance changes; audit read-only unless the user authorizes fixes.
---

# AI-GAME Rule and Document Drift Audit

[constraint-source: USER_DECISION; ref: repository governance request 2026-08-23]

Find instruction drift without turning uncertain findings into new rules.

## Scope

Start from the files Codex actually loads or follows in this repository:

- `AGENTS.md` and any nested `AGENTS.md` files;
- `.agents/skills/*/SKILL.md` and their linked references;
- `docs/product/00_INDEX.md` through
  `docs/product/09_DECISIONS_AND_OPEN_QUESTIONS.md`;
- the active work order and files directly referenced by those authorities.

Follow the authority order in `AGENTS.md`. Historical documents are evidence,
not current instruction sources. Do not scan unrelated prose merely because it
contains words such as "must" or "gate".

## Findings

Report only evidence-backed findings:

- `outdated`: a path, command, state, version, policy, or factual claim no
  longer matches current source or runtime evidence;
- `conflict`: two applicable authorities prescribe incompatible behavior;
- `duplicate`: the same detailed rule is copied into always-loaded or
  authoritative files and is likely to drift;
- `ambiguous`: wording supports materially different actions or unsafe scope;
- `unsupported_constraint`: a restrictive rule has no traceable
  `USER_DECISION`, `PRODUCT_SPEC`, `ARCH_INVARIANT`, `REPRODUCED_FAILURE`, or
  valid unexpired `TEMPORARY` source.

For each finding, cite the instruction location and the conflicting evidence
with file and line. Low-confidence suspicions are not findings.

Do not retroactively flag every pre-existing settled rule merely because it
lacks a new inline source tag. Trace it through the existing authority chain.
The explicit tag format in `constraint-governance` applies to constraints added
or materially broadened after that governance entry was introduced.

## Resolution

Prioritize by impact, then severity, then clarity of the correction. Recommend
one of:

- keep, with the evidence that supports it;
- correct an outdated factual claim;
- remove a duplicate and retain one canonical location;
- downgrade an unsupported rule to a non-blocking hypothesis or observation;
- remove an expired temporary constraint;
- request a user decision when competing authoritative meanings cannot be
  resolved from current evidence.

An audit is read-only by default. Do not edit files, create branches, commit,
push, or open a PR unless the user explicitly asks for fixes or delivery. When
fixes are authorized, preserve unrelated changes and do not silently choose
between conflicting user/product decisions.

## Output

Return a prioritized list with `type`, `severity`, `instruction`, `evidence`,
and `recommended_resolution`. State the scanned scope and explicitly say when
no supported finding was found. Create a report file only when the user asks
for an artifact.
