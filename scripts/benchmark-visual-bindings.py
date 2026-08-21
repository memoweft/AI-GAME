from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "console" / "backend"))

from ai_game_console.config import Settings  # noqa: E402
from ai_game_console.mobile_agent import DecisionContext, MobileTaskArchive  # noqa: E402
from ai_game_console.mobile_task_adapter import (  # noqa: E402
    LocalMobileEvidenceStore,
    OpenAICompatibleMobileRoleModel,
    OpenAICompatibleToolRoleModel,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay persisted real frames through one visual binding without ADB."
    )
    parser.add_argument("--binding", choices=("qwen", "gui-owl"), required=True)
    parser.add_argument("--task-id", required=True)
    arguments = parser.parse_args()

    settings = Settings.from_env()
    evidence = LocalMobileEvidenceStore(
        settings.project_root / "runtime" / "sessions" / "mobile-tasks" / "evidence"
    )
    if arguments.binding == "qwen":
        model = OpenAICompatibleToolRoleModel(
            endpoint=_required(settings.mobile_role_endpoint, "mobile role endpoint"),
            model=_required(settings.mobile_role_model, "mobile role model"),
            api_key=settings.mobile_role_api_key,
            timeout_seconds=120,
            evidence=evidence,
        )
    else:
        model = OpenAICompatibleMobileRoleModel(
            endpoint=_required(settings.local_chat_endpoint, "GUI-Owl endpoint"),
            model=_required(settings.local_chat_model, "GUI-Owl model"),
            api_key=settings.local_chat_api_key,
            timeout_seconds=120,
            evidence=evidence,
        )

    state = MobileTaskArchive(settings.data_dir / "mobile-tasks.db").inspect(
        arguments.task_id
    )
    if state.plan is None:
        raise RuntimeError("task has no persisted plan")
    subgoals = {item.index: item for item in state.plan.subgoals}
    results = []
    for offset, attempt in enumerate(state.attempts):
        subgoal = subgoals[attempt.subgoal_index]
        started = time.perf_counter()
        decision = model.decide(DecisionContext(
            task_id=state.task_id,
            goal=state.goal,
            target_id=state.target_id,
            plan_revision=state.plan.revision,
            subgoal=subgoal,
            input_revision=state.input_revision,
            owner_inputs=state.inputs,
            observation=attempt.before,
            strategy=state.strategy,
            consecutive_no_progress=0,
            recent_attempts=state.attempts[:offset],
            skill_memory=None,
        ))
        elapsed = round(time.perf_counter() - started, 3)
        results.append({
            "sequence": attempt.sequence,
            "subgoal_index": attempt.subgoal_index,
            "subgoal": subgoal.description,
            "latency_seconds": elapsed,
            "decision_kind": decision.kind,
            "action": decision.intent.name if decision.intent else None,
            "arguments": dict(decision.intent.arguments) if decision.intent else {},
        })
    print(json.dumps({
        "binding": arguments.binding,
        "task_id": state.task_id,
        "frame_count": len(results),
        "total_latency_seconds": round(sum(item["latency_seconds"] for item in results), 3),
        "results": results,
    }, ensure_ascii=False, indent=2))


def _required(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"missing {label}")
    return value


if __name__ == "__main__":
    main()
