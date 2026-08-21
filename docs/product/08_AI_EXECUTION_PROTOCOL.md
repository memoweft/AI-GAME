# AI implementation execution protocol

## 1. Purpose

This protocol lets an implementation AI advance AI-GAME without repeatedly rediscovering product intent, treating historical passes as current proof, or rewriting the whole platform at once.

The user has delegated technical implementation choices inside the current work order. The AI should act autonomously on discoverable details and ask plain-language questions only when an answer materially changes product behavior, external authority, irreversible state, or acceptance.

## 2. Start sequence

Before implementation:

1. read root `AGENTS.md` and this product package;
2. read the active work order completely;
3. run `git status --short --branch`;
4. preserve unrelated and user-owned changes;
5. inspect current code and current runtime rather than trusting an old report;
6. identify the earliest real break in the work-order business loop;
7. record current evidence level and rollback path;
8. ask only if an unresolved item in `09_DECISIONS_AND_OPEN_QUESTIONS.md` is required now.

## 3. Implementation behavior

Within one work order the AI may:

- choose private classes, functions, schema migration details, test fixtures, and file layout consistent with target architecture;
- make the complete bounded vertical change rather than stopping after each file;
- run focused tests, repair the first real failure, and continue;
- use read-only inspection and real current-source runtime checks;
- update documentation and current state to match verified behavior;
- use parallel read-only analysis or non-overlapping implementation agents when it materially helps.

The AI must not:

- silently change the user's goal or success criteria;
- make an old module the permanent product boundary merely because it is easier;
- remove a compatibility path before replacement runtime and rollback evidence;
- mix old and new workers on the same physical target;
- let model names leak into generic domain contracts;
- turn learned experience into fixed coordinate macros;
- promote incomplete or uncertain experience;
- claim a real-device result from mocks, tests, ADB acceptance, or a report from another commit;
- deploy, publish, push, install broad machine dependencies, or alter external accounts without the necessary authority.

## 4. Earliest-break loop

For each work order:

```text
reproduce the intended vertical
-> find the earliest point where current reality diverges
-> make the smallest architecture-consistent vertical repair
-> run focused verification
-> continue the same vertical
-> run regression and specified real acceptance
-> update evidence and status
```

Do not spend a turn polishing downstream UI when the task never leaves `CREATED`. Do not redesign memory retrieval when Goal completion can still be falsely declared.

## 5. Decision boundary

Ask the user in plain language when the next action requires one of these:

- selecting between materially different product behaviors;
- deciding what an external or open-ended outcome means;
- presenting the one concrete plan-level approval required by decision D12 before a missing-component install or system-security mutation, and asking again only if the plan materially expands;
- deleting or transforming user data without a tested reversible migration;
- operating a real account/target outside the previously authorized environment;
- choosing a model role that cannot be discovered from capabilities;
- accepting a deviation from the work-order success criteria.

Do not ask for:

- names of private helpers;
- which test file to add;
- which SQLite migration number to use;
- whether to inspect a safe local file;
- whether to run focused tests;
- whether to fix a regression introduced by the current change;
- details that can be determined from code, runtime probes, or existing configuration.

Question style:

```text
I found X. If I choose A, the product will behave like ...; if I choose B, it will behave like .... Which result do you want?
```

Avoid architecture jargon when a concrete consequence can be stated.

## 6. Work-order status

Allowed states:

```text
NOT_STARTED
IN_PROGRESS
PARTIAL
BLOCKED
FAILED
DONE
```

`DONE` requires every specified mandatory verification. If real-device evidence is unavailable, the work order is `PARTIAL` unless the work order explicitly defines it as a later separate gate.

Do not set `BLOCKED` because work is difficult. Use it only when implementation cannot proceed without a missing authority, external state, or required user decision.

## 7. Advancing work orders

If the user authorizes execution of the roadmap, the AI may continue from one completed work order to the next without requesting ceremonial approval when:

- the current order is `DONE`;
- its rollback path remains valid;
- the next order has no unresolved required decision;
- no external deployment or destructive operation is implied;
- the repository is in a known state and evidence is recorded.

Otherwise it reports the exact completed boundary and asks one concrete question.

Creating or editing these documents alone is not authorization to implement product code. A request such as “按最新路线开始执行”, “执行 U1”, or equivalent activates implementation.

## 8. Git and changes

- Do not reset or discard pre-existing work.
- Do not include `session.jsonl`, runtime databases, model files, credentials, screenshots, or temporary evidence unless a work order explicitly requires a sanitized artifact.
- Prefer additive, reversible migrations until cutover.
- Focus commits by verified work order when commits are authorized.
- Never push automatically.
- Record the exact commit/working tree used for runtime evidence.

## 9. Runtime and evidence hygiene

- Use the normal launcher for composition claims.
- Do not test against a stale already-running process after source changes.
- Identify devices and processes before mutating or stopping them.
- Keep real accounts and runtime data out of destructive tests; use controlled copies for restore/failure injection.
- Store evidence references and integrity metadata, not secrets or unnecessary personal content.
- Recheck time-sensitive facts such as mode, endpoint health, device serial, and model binding.

## 10. Work-order template

```text
# Ux — title

STATUS
NOT_STARTED

BUSINESS OUTCOME
What becomes possible for the user

CURRENT FACTS
Verified code/runtime starting point

IN SCOPE
Allowed changes

OUT OF SCOPE
Explicit exclusions

DESIGN
Frozen public behavior, ownership, data, and migration rules

IMPLEMENTATION FREEDOM
Choices the AI should make without asking

EARLIEST REAL BREAK
Where the current end-to-end flow stops

MUST ASK IF
Work-order-specific product/authority decisions

VERIFY
Focused tests, regression, current-source runtime, real target, metrics

ROLLBACK
How to disable or restore safely

DONE WHEN
Exact exit conditions

NEXT
Following work order after a clean exit
```

## 11. Handoff report

Use the format in `07_ACCEPTANCE_AND_EVIDENCE.md`. Lead with the observable outcome, then evidence, boundaries, rollback, and remaining risk. Do not report tool activity as product progress.

Every stage/work-order handoff must finish with exactly these four user-facing
sections, in this order:

```text
总路线
U0-U9 status table and current active work order

完成了什么
Observable capability plus separate artifact, automated, current-source
runtime, real-device, learning, deployment, and rollback facts

还有什么没完成
Every missing acceptance level, open gate, deviation, blocker, and NOT RUN or
NOT AVAILABLE item

下一阶段是什么
Next work order, business outcome, prerequisites, and whether advancement is
currently allowed
```

This four-part closeout is mandatory even when a stage is only `PARTIAL`,
`BLOCKED`, or `FAILED`. It supplements rather than weakens the evidence-layer
format in `07_ACCEPTANCE_AND_EVIDENCE.md`.
