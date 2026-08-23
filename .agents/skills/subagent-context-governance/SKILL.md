---
name: subagent-context-governance
description: Decide when AI-GAME work should use Codex subagents, construct bounded delegation briefs, and repair or disclose main-agent context gaps. Use for parallel exploration, isolated implementation, independent verification, large logs or document sets, context compaction, uncertain inherited context, or any proposed delegation; do not trigger merely because a task is difficult.
---

# Subagent and Main-Agent Context Governance

[constraint-source: USER_DECISION; ref: repository governance request 2026-08-23]

Use subagents to improve bounded execution or keep noisy intermediate work out
of the main thread. The main Agent remains responsible for the user's goal,
authority, decisions, integration, evidence boundaries, and final answer.

## Main-agent context defects

A main-agent context defect exists when a fact needed for the current scoped
decision or claim is not reliably supported in the main Agent's current
context. Common cases are:

- current `AGENTS.md`, product authority, active work order, target files,
  dirty-tree overlap, or live runtime state has not been inspected;
- an inherited summary, older report, memory, or subagent result may be stale
  and the current source is available but unread;
- a conclusion depends on omitted raw evidence, conflicting references, or an
  unavailable external/runtime observation;
- large logs, stack traces, search output, or document sets are burying the
  requirements and decisions in context pollution;
- a material product choice or authority decision is genuinely absent.

Difficulty, uncertainty before inspection, or the availability of subagents is
not by itself a context defect.

Use these diagnostic classes when useful; they are not new work-order states:

- `discoverable_context_gap`: current repository, runtime, or authoritative
  files can answer it;
- `delegable_context_gap`: a bounded independent investigation can return the
  missing evidence without making the product decision;
- `decision_context_gap`: only the user or another named authority can choose
  between materially different outcomes;
- `unavailable_evidence`: the required browser, device, service, account, or
  external observation cannot currently be obtained.

## Repair order

1. State the exact decision or claim the missing context affects.
2. Read the current authority and smallest directly relevant source or live
   evidence. Do not use a subagent to avoid the main Agent's own authority read.
3. Separate verified fact, inference, and unknown. Never fill a context gap with
   a plausible story.
4. Delegate only if the remaining gap is bounded and independent, or moving its
   noisy investigation off the main thread materially protects context quality.
5. Ask the user only for a `decision_context_gap` that cannot be resolved from
   the current authority. For `unavailable_evidence`, qualify the claim instead
   of inventing proof.

After context compaction, continue from the preserved task state. Re-read only
the authority pointers and drift-prone evidence needed for the next action; do
not restart completed work merely because the original transcript is shorter.
A context gap does not become `BLOCKED` unless it independently meets the
existing blocker definition in the execution protocol.

## Invoke a subagent when

Use the smallest number of agents that materially helps, and only for concrete,
bounded work. Good triggers include:

- two or more independent codebase questions can be answered in parallel;
- read-heavy exploration, test output, log analysis, or document extraction
  would pollute the main thread but can return a concise evidence summary;
- implementation can be partitioned into non-overlapping file ownership with
  independently testable outputs;
- an independent review or forward test would materially reduce framing bias
  for a complex or risky change;
- a long-running check can proceed independently while the main Agent performs
  useful non-dependent work.

Prefer read-only subagents for discovery, review, triage, summarization, and
evidence extraction. Parallel writes require explicit non-overlapping ownership
and a shared-worktree warning.

## Do not invoke a subagent when

- one targeted read, search, or command can answer the question quickly;
- work is sequential and the next task depends on the current result;
- the main Agent has not yet established current authority, scope, and dirty
  worktree boundaries;
- multiple agents would edit the same files or operate the same physical target;
- delegation would bypass permission, approval, user decision, or evidence
  requirements;
- the intent is to manufacture consensus, offload final accountability, or hand
  an unbounded version of the entire task to another Agent;
- the only issue is that the task is difficult.

Subagent unavailability is normally a reason to continue serially, not a new
project blocker.

## Delegation brief

Make each brief self-sufficient for its bounded task; do not assume inherited
history contains the needed truth. Include:

1. the concrete subtask and expected output;
2. current source-of-truth files or raw artifacts to inspect;
3. verified facts, known unknowns, and the evidence level required;
4. read-only scope or exact owned files/responsibility;
5. the shared dirty-worktree warning: other work exists, do not revert or
   overwrite it, and accommodate concurrent changes;
6. mutation and external-action authority, including what is not authorized;
7. success criteria and a proportionate stopping condition;
8. the requested return format, including file/line references, commands, test
   output, or an explicit `NOT AVAILABLE` boundary.

Pass only the context needed for the subtask. For an independent evaluation,
provide the raw artifact and acceptance target without preloading the main
Agent's suspected answer. Full conversation inheritance is appropriate only
when the subtask genuinely depends on it.

## Main-agent integration

The main Agent must:

- retain the original user objective and current authority chain;
- inspect each returned result and resolve disagreements against current source
  or runtime evidence;
- treat a subagent statement as evidence to verify, not as product authority;
- integrate only non-overlapping edits and run proportionate combined checks;
- steer or stop a subagent that drifts beyond scope;
- disclose any remaining context or evidence gap without converting it into a
  success claim or a permanent constraint.

A subagent cannot create a user decision, product principle, permanent gate,
or acceptance waiver. Apply `constraint-governance` to restrictions proposed by
either the main Agent or a subagent.

## Handoff

Report delegation only to the degree needed to make the result auditable:
subtask ownership, material evidence returned, integration checks performed,
and unresolved context gaps. Do not replace the product outcome with an Agent
activity log.
