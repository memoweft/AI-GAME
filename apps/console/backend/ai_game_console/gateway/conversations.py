"""ConversationService — unique conversation <-> active Task association
(frozen contract §8).

Rules (from the contract, not a second state machine):

1. If the conversation has exactly one non-terminal Task, a conversation
   message is user input for that Task — never a new goal.
2. If it has zero non-terminal Tasks, the message is a new goal and a new
   Task must be created.
3. If it has more than one non-terminal Task, the association is
   ambiguous: ``CONVERSATION_CONFLICT``.
4. Never guess by "most recent message" — ambiguity is always an error.

Active-task state is read from the Runtime Kernel (the single source of
truth); the Gateway keeps no duplicate association table.
"""

from __future__ import annotations

from ..runtime_kernel import RuntimeKernel, Task
from .errors import ConversationConflict


class ConversationService:
    def __init__(self, kernel: RuntimeKernel) -> None:
        self._kernel = kernel

    def active_tasks(self, conversation_id: str) -> tuple[Task, ...]:
        return tuple(
            task
            for task in self._kernel.list_tasks_by_conversation(conversation_id)
            if not task.terminal
        )

    def resolve(self, conversation_id: str) -> Task | None:
        """Return the conversation's single active Task, or ``None``.

        Raises ``ConversationConflict`` when more than one Task is active.
        """
        active = self.active_tasks(conversation_id)
        if len(active) > 1:
            raise ConversationConflict(
                f"Conversation {conversation_id} is associated with "
                f"{len(active)} active Tasks"
            )
        return active[0] if active else None
