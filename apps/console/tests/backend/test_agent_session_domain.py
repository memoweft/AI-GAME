from __future__ import annotations

import pytest

from ai_game_console.agent_runtime.domain import (
    GoalBindingStatus,
    GoalNodeStatus,
    SessionStatus,
    can_transition_session,
)


def test_r1_domain_keeps_one_active_binding_fence_and_terminal_lifecycle():
    assert GoalBindingStatus.REQUESTED.active is True
    assert GoalBindingStatus.BOUND.active is True
    assert GoalBindingStatus.RELEASED.active is False
    assert GoalNodeStatus.CANCELLED.terminal is True
    assert can_transition_session(SessionStatus.ACTIVE, SessionStatus.STOPPING)
    assert not can_transition_session(SessionStatus.STOPPED, SessionStatus.ACTIVE)


def test_r1_domain_rejects_unlisted_session_transition():
    assert not can_transition_session(SessionStatus.PLANNING, SessionStatus.COMPLETED)
    assert can_transition_session(SessionStatus.ACTIVE, SessionStatus.COMPLETED)
