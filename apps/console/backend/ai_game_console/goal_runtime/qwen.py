from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ..goal_families import (
    STZB_DAILY_GOAL_FAMILY,
    is_stzb_discovery_only_goal,
    normalize_goal_family,
)
from ..mobile_agent.domain import Observation
from ..mobile_task_adapter import OpenAICompatibleToolRoleModel
from .domain import (
    CriterionAssessment,
    GoalCompletionAssessment,
    GoalSpecificationDraft,
    SuccessCriterion,
)
from .stzb_daily import (
    DailyChecklistItem,
    DailyChecklistSnapshot,
    checklist_item_id,
    normalized_intent,
)


class StructuredGoalModel:
    """Qwen goal-level roles over the same provider-neutral forced-tool transport."""

    def __init__(self, role_model: OpenAICompatibleToolRoleModel) -> None:
        self._role_model = role_model

    def specify(self, original_goal: str) -> GoalSpecificationDraft:
        if normalize_goal_family(original_goal) == STZB_DAILY_GOAL_FAMILY:
            # U5 owns a stricter contract than a generic free-form extraction:
            # discover the complete current-day checklist, prove each visible
            # outcome, then independently reread that same frozen checklist.
            # Freezing this known family deterministically prevents a malformed
            # tool response from weakening or blocking the owner contract.
            intent = normalized_intent(
                    original_goal,
                    {
                        "classification": "finite_phone_goal",
                        "outcome": original_goal,
                    },
                )
            if is_stzb_discovery_only_goal(original_goal):
                intent["execution_policy"] = "discovery_only"
                return GoalSpecificationDraft(
                    intent,
                    (
                        SuccessCriterion(
                            "complete_daily_checklist_discovered",
                            "发现并冻结当天完整的每日任务目标集及全部可见条目。",
                            "新鲜设备画面必须证明每日/今日身份、全部可见入口与列表边界覆盖。",
                            original_goal,
                        ),
                        SuccessCriterion(
                            "daily_items_preserved_without_execution",
                            "原样记录每个条目的当前状态，且不执行、领取或完成任何清单条目。",
                            "动作证据只能用于导航和读取；不得出现面向清单条目的执行或领取动作。",
                            original_goal,
                        ),
                    ),
                )
            return GoalSpecificationDraft(
                intent,
                (
                    SuccessCriterion(
                        "complete_daily_checklist_discovered",
                        "发现并冻结当天完整的每日任务清单及全部条目。",
                        "新鲜设备画面必须显示每日/今日身份、完整列表覆盖和可区分的全部条目。",
                        original_goal,
                    ),
                    SuccessCriterion(
                        "all_feasible_daily_items_completed",
                        "完成清单中当前可完成的每一项；受外部条件阻塞的项目必须明确列出。",
                        "每个冻结条目均需有可见完成证据，或有明确且诚实的阻塞证据。",
                        original_goal,
                    ),
                    SuccessCriterion(
                        "daily_checklist_independently_reread",
                        "从不同的新鲜画面重新读取同一清单并确认最终状态。",
                        "最终画面须与冻结清单日期及条目集合一致，且无未完成或不确定项目。",
                        original_goal,
                    ),
                ),
            )
        decoded = self._role_model.call_tool(
            system=(
                "你是独立目标规格层。必须调用 record_goal_specification。原始目标不可修改。"
                "把每个并列、顺序和结果要求分别变成可验证成功标准；不得因为执行困难而省略。"
                "每个标准的 source_quote 必须逐字摘自原始目标，并使用稳定英文小写 id。"
                "只要求一次有界手机结果时，无论目标是哪一个应用，都分类为 finite_phone_goal。"
                "若持续目标只要求 AI-GAME 本机持久等待、用户后续消息、提醒或候选里程碑，"
                "且不需要操作手机应用、外部服务或现实环境，分类为 long_lived_local_goal。"
                "若目标需要在手机应用或外部服务中长期观察、等待事件，并在多个有界周期中"
                "继续操作，分类为 long_lived_application_goal。分类只描述目标生命周期和"
                "环境，不得选择或臆造 owner、profile 或账号门禁。"
            ),
            prompt=f"原始目标：{original_goal}",
            observations=(),
            tool_name="record_goal_specification",
            description="Freeze the complete owner goal before device execution",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "classification": {
                        "type": "string",
                        "enum": [
                            "finite_phone_goal",
                            "long_lived_local_goal",
                            "long_lived_application_goal",
                            "language_only_goal",
                        ],
                    },
                    "outcome": {"type": "string"},
                    "success_criteria": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "evidence_requirement": {"type": "string"},
                                "source_quote": {"type": "string"},
                            },
                            "required": [
                                "id", "description", "evidence_requirement", "source_quote"
                            ],
                        },
                    },
                },
                "required": ["classification", "outcome", "success_criteria"],
            },
            max_tokens=768,
            # This extraction task is schema-constrained and does not benefit
            # from consuming the whole response budget in hidden reasoning.
            # The final verifier uses the same stable non-thinking baseline.
            reasoning_effort="low",
            reasoning_budget=0,
        )
        classification = decoded.get("classification")
        outcome = decoded.get("outcome")
        raw_criteria = decoded.get("success_criteria")
        if (
            not isinstance(classification, str)
            or classification.strip() not in {
                "finite_phone_goal",
                "long_lived_local_goal",
                "long_lived_application_goal",
                "language_only_goal",
            }
            or not isinstance(outcome, str)
            or not outcome.strip()
            or not isinstance(raw_criteria, list)
        ):
            raise ValueError("invalid goal specification response")
        criteria = []
        for item in raw_criteria:
            if not isinstance(item, dict):
                raise ValueError("invalid goal criterion")
            identifier = str(item.get("id") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", identifier):
                raise ValueError("invalid goal criterion id")
            criteria.append(SuccessCriterion(
                identifier,
                str(item.get("description") or "").strip(),
                str(item.get("evidence_requirement") or "").strip(),
                str(item.get("source_quote") or "").strip(),
            ))
        return GoalSpecificationDraft(
            normalized_intent(
                original_goal,
                {"classification": classification.strip(), "outcome": outcome.strip()},
            ),
            tuple(criteria),
        )

    def answer_language(self, original_goal: str) -> str:
        decoded = self._role_model.call_tool(
            # [constraint-source: PRODUCT_SPEC; ref: U8 OUT OF SCOPE and D10]
            system=(
                "你是本地语言结果能力。必须调用 record_language_result。"
                "只完成用户要求的纯语言任务；不得声称访问了设备、账号、实时网络或外部世界。"
            ),
            prompt=f"原始目标：{original_goal}",
            observations=(),
            tool_name="record_language_result",
            description="Return the requested local language-only result",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {"result": {"type": "string", "minLength": 1}},
                "required": ["result"],
            },
            max_tokens=2_048,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        result = decoded.get("result")
        if not isinstance(result, str) or not result.strip():
            raise ValueError("invalid language result response")
        return result.strip()

    def assess_mobile_application_readiness(
        self,
        original_goal: str,
        observation: Observation,
        expected_application_id: str,
    ) -> dict[str, Any]:
        """Assess login/interaction readiness from the persisted pre-action frame.

        The package identity comes from Android device state; this visual role
        only decides whether the current frame shows an account-scoped,
        interaction-ready application surface.  It cannot select an account,
        owner, or physical action.
        """

        decoded = self._role_model.call_tool(
            # [constraint-source: USER_DECISION; ref: D22 U8 readiness before action]
            system=(
                "你是移动应用只读 readiness 验证层。必须调用 "
                "record_application_readiness，禁止输出坐标、点击、文本输入或任何设备动作。"
                "只根据这一张当前截图判断：目标应用是否已进入可交互业务界面，以及是否"
                "显示登录后的账号会话状态，并核对画面是否与原始目标点名或描述的应用一致。"
                "登录页、注册页、验证码页、权限阻塞、断线遮罩、空白/黑屏、应用不匹配或"
                "无法读清时都不能判 ready。不得猜测具体账号身份。"
            ),
            prompt=(
                f"原始长期目标：{original_goal}\n"
                f"Android 当前前台应用：{expected_application_id}\n"
                "给出当前画面的 readiness、登录后会话判断和简短可复核证据。"
            ),
            observations=(observation,),
            tool_name="record_application_readiness",
            description="Record a read-only pre-action application readiness verdict",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ready": {"type": "boolean"},
                    "authenticated": {"type": "boolean"},
                    "application_matches_goal": {"type": "boolean"},
                    "blocking_state": {
                        "type": "string",
                        "enum": [
                            "none",
                            "login_required",
                            "registration_required",
                            "permission_blocked",
                            "connection_blocked",
                            "unreadable",
                            "other",
                        ],
                    },
                    "evidence": {"type": "string", "minLength": 1},
                },
                "required": [
                    "ready",
                    "authenticated",
                    "application_matches_goal",
                    "blocking_state",
                    "evidence",
                ],
            },
            max_tokens=512,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        ready = decoded.get("ready")
        authenticated = decoded.get("authenticated")
        application_matches_goal = decoded.get("application_matches_goal")
        blocking_state = decoded.get("blocking_state")
        evidence = decoded.get("evidence")
        if (
            not isinstance(ready, bool)
            or not isinstance(authenticated, bool)
            or not isinstance(application_matches_goal, bool)
            or blocking_state
            not in {
                "none",
                "login_required",
                "registration_required",
                "permission_blocked",
                "connection_blocked",
                "unreadable",
                "other",
            }
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            raise ValueError("invalid application readiness response")
        if ready and (
            not authenticated
            or not application_matches_goal
            or blocking_state != "none"
        ):
            raise ValueError("application readiness response is contradictory")
        return {
            "ready": ready,
            "authenticated": authenticated,
            "application_matches_goal": application_matches_goal,
            "blocking_state": blocking_state,
            "evidence": evidence.strip()[:1_000],
        }

    def inspect_daily_checklist(
        self, original_goal: str, observations: tuple[Observation, ...]
    ) -> tuple[DailyChecklistSnapshot, ...]:
        """Extract evidence-bounded daily-list or paginated-surface snapshots."""

        if not observations:
            return ()
        evidence = [
            {"image_index": index, "evidence_id": item.evidence_id}
            for index, item in enumerate(observations)
        ]
        decoded = self._role_model.call_tool(
            system=(
                "你是率土之滨每日清单观察器。必须调用 record_daily_checklist。"
                "逐张判断输入画面；记录确实显示每日/日常条目的完整清单或分页表面。"
                "coverage_complete 只有在标题、全部条目和列表末端或总完成标记都可见时才为 true。"
                "横向活动轮播单帧必须为 false，并用 coverage_start/coverage_end 标明滚动条或"
                "边界箭头是否证明已到该表面的起点/终点；只记录卡片上明确写有每日/每天/今日"
                "周期证据的条目。新版任务-事务只有同时可见巡察、每日刷新或今日周期证据时才"
                "使用 migrated_affairs，事务标签本身不够。多帧合并由确定性存储层完成。"
                "不要从旧画面、计划、按钮点击或常识补全条目。每项状态只能依据对应画面。"
            ),
            prompt=json.dumps(
                {
                    "original_goal": original_goal,
                    "images": evidence,
                    "instruction": (
                        "返回可识别的清单画面。image_index 必须引用输入；date_label 使用画面"
                        "可见日期/今日标识，无法区分日期则 uncertain 且 coverage_complete=false。"
                        "对于带明确每日/每天文案但无日历日期的活动轮播，date_label 统一写"
                        "current-daily-cycle，surface_id 写 activity_carousel。"
                    ),
                },
                ensure_ascii=False,
            ),
            observations=observations,
            tool_name="record_daily_checklist",
            description="Record complete visible STZB daily checklist observations",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "snapshots": {
                        "type": "array",
                        "maxItems": len(observations),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "image_index": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": len(observations) - 1,
                                },
                                "date_label": {"type": "string"},
                                "coverage_complete": {"type": "boolean"},
                                "surface_id": {
                                    "type": "string",
                                    "enum": [
                                        "single_daily_list",
                                        "activity_carousel",
                                        "migrated_affairs",
                                        "other",
                                    ],
                                },
                                "coverage_start": {"type": "boolean"},
                                "coverage_end": {"type": "boolean"},
                                "items": {
                                    "type": "array",
                                    "maxItems": 64,
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string"},
                                            "status": {
                                                "type": "string",
                                                "enum": [
                                                    "completed",
                                                    "incomplete",
                                                    "blocked",
                                                    "uncertain",
                                                ],
                                            },
                                            "evidence": {"type": "string"},
                                        },
                                        "required": ["title", "status", "evidence"],
                                    },
                                },
                            },
                            "required": [
                                "image_index",
                                "date_label",
                                "coverage_complete",
                                "surface_id",
                                "coverage_start",
                                "coverage_end",
                                "items",
                            ],
                        },
                    }
                },
                "required": ["snapshots"],
            },
            max_tokens=3_072,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        raw_snapshots = decoded.get("snapshots")
        if not isinstance(raw_snapshots, list):
            raise ValueError("invalid daily checklist response")
        now = datetime.now(UTC)
        snapshots: list[DailyChecklistSnapshot] = []
        seen_evidence: set[str] = set()
        for order, raw in enumerate(raw_snapshots):
            if not isinstance(raw, dict):
                raise ValueError("invalid daily checklist snapshot")
            index = raw.get("image_index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("invalid daily checklist image index")
            if index < 0 or index >= len(observations):
                raise ValueError("daily checklist references an unknown image")
            evidence_id = observations[index].evidence_id
            if evidence_id in seen_evidence:
                raise ValueError("daily checklist repeats one evidence frame")
            seen_evidence.add(evidence_id)
            raw_items = raw.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("invalid daily checklist items")
            items = tuple(
                DailyChecklistItem(
                    checklist_item_id(str(item.get("title") or "")),
                    str(item.get("title") or "").strip(),
                    str(item.get("status") or "uncertain"),  # type: ignore[arg-type]
                    str(item.get("evidence") or "").strip(),
                )
                for item in raw_items
                if isinstance(item, dict)
            )
            if len(items) != len(raw_items):
                raise ValueError("invalid daily checklist item")
            snapshots.append(
                DailyChecklistSnapshot(
                    evidence_id=evidence_id,
                    date_label=str(raw.get("date_label") or "").strip(),
                    coverage_complete=bool(raw.get("coverage_complete")),
                    items=items,
                    observed_at=(now + timedelta(microseconds=order)).isoformat(),
                    surface_id=str(raw.get("surface_id") or "other").strip(),
                    coverage_start=bool(raw.get("coverage_start")),
                    coverage_end=bool(raw.get("coverage_end")),
                )
            )
        return tuple(snapshots)

    def verify(
        self, original_goal: str, specification: dict[str, Any], state: Any
    ) -> GoalCompletionAssessment:
        attempts = tuple(_value(state, "attempts", ()))
        eligible = []
        for attempt in attempts:
            verification = _value(attempt, "verification")
            after = _value(attempt, "after")
            if (
                verification is not None
                and bool(_value(verification, "satisfied", False))
                and not bool(_value(verification, "uncertain", False))
                and after is not None
            ):
                eligible.append({
                    "sequence": int(_value(attempt, "sequence", 0)),
                    "subgoal_index": int(_value(attempt, "subgoal_index", -1)),
                    "verification_evidence": str(_value(verification, "evidence", "")),
                    "after_evidence_id": str(_value(after, "evidence_id", "")),
                })
        if not eligible:
            raise ValueError("goal completion has no eligible verified after evidence")
        plan = _value(state, "plan")
        plan_payload = [
            {
                "index": int(_value(item, "index", -1)),
                "description": str(_value(item, "description", "")),
            }
            for item in _value(plan, "subgoals", ())
        ] if plan is not None else []
        prompt = json.dumps({
            "original_goal": original_goal,
            "frozen_specification": specification,
            "generated_plan": plan_payload,
            "eligible_verified_attempts": eligible,
            "instruction": (
                "逐项判断冻结标准。satisfied 只能引用上面存在的 subgoal index 和 attempt "
                "sequence。每条记录都来自已持久化的新鲜动作后画面验证；最终层不得重新发明画面事实。"
                "计划完成本身不是目标完成。结果摘要只能使用已验证事实。对于告知、报告或"
                "回复用户的标准，本次 GoalRun 最终响应就是交付载体：只要它能完全由某条"
                "eligible_verified_attempt 的事实生成，就引用该事实的阶段和尝试并判定；"
                "不要要求在最终响应之前已经存在另一条用户回复。"
            ),
        }, ensure_ascii=False)
        decoded = self._role_model.call_tool(
            system=(
                "你是独立 Goal Completion Verifier，不是规划器。必须调用 "
                "record_goal_completion。不得相信传输成功或任务 completed 状态；只能使用冻结标准、"
                "已验证尝试事实。任何标准缺证据时 verdict 不能是 verified。"
            ),
            prompt=prompt,
            observations=(),
            tool_name="record_goal_completion",
            description="Assess every frozen criterion against admissible attempt evidence",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "verdict": {"type": "string", "enum": ["verified", "partial", "uncertain"]},
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "criterion_id": {"type": "string"},
                                "satisfied": {"type": "boolean"},
                                "subgoal_indices": {"type": "array", "items": {"type": "integer"}},
                                "attempt_sequences": {"type": "array", "items": {"type": "integer"}},
                                "evidence": {"type": "string"},
                            },
                            "required": [
                                "criterion_id", "satisfied", "subgoal_indices",
                                "attempt_sequences", "evidence"
                            ],
                        },
                    },
                    "verified_facts": {"type": "array", "items": {"type": "string"}},
                    "result_summary": {"type": "string"},
                },
                "required": ["verdict", "criteria", "verified_facts", "result_summary"],
            },
            max_tokens=2_048,
            reasoning_effort="low",
            reasoning_budget=0,
        )
        criteria = tuple(
            CriterionAssessment(
                criterion_id=str(item.get("criterion_id") or ""),
                satisfied=bool(item.get("satisfied")),
                subgoal_indices=tuple(int(value) for value in item.get("subgoal_indices", ())),
                attempt_sequences=tuple(int(value) for value in item.get("attempt_sequences", ())),
                evidence=str(item.get("evidence") or ""),
            )
            for item in decoded.get("criteria", ())
            if isinstance(item, dict)
        )
        facts = decoded.get("verified_facts")
        return GoalCompletionAssessment(
            verdict=str(decoded.get("verdict") or "uncertain"),
            criteria=criteria,
            verified_facts=tuple(str(item).strip() for item in facts)
            if isinstance(facts, list) else (),
            result_summary=str(decoded.get("result_summary") or "").strip(),
        )


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)
