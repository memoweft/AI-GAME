"""Soul reply application for the generic AI-GAME ApplicationRuntime.

The dating-copilot owner remains the sole device/page/send-ledger authority.
This package owns reply policy, proof verification, and delayed-outcome
learning without persisting screenshots, message bodies, or identity fields.
"""

from .adapter import (
    INTENT_NAME,
    PROFILE_ID,
    SoulApplicationPorts,
    SoulExecutionOwner,
    SoulObservationPort,
    SoulPersistenceProjection,
    SoulReplyMemoryGate,
    SoulReplyPolicy,
    SoulReplyVerifier,
    build_soul_application_ports,
)
from .domain import SoulOwnerObservation, SoulTranscriptItem, SoulVisualFacts
from .errors import SoulApplicationError
from .owner import SCHEDULER_CONTROLLER_REF, SoulOwnerClient
from .reply_learning import (
    DelayedOutcomeEvidence,
    ReplyLearning,
    ReplyLearningStore,
    StrategyRecommendation,
    TrialDraft,
)
from .vision import LoopbackSoulVisionClient

__all__ = [
    "DelayedOutcomeEvidence",
    "INTENT_NAME",
    "LoopbackSoulVisionClient",
    "PROFILE_ID",
    "ReplyLearning",
    "ReplyLearningStore",
    "SCHEDULER_CONTROLLER_REF",
    "SoulApplicationError",
    "SoulApplicationPorts",
    "SoulExecutionOwner",
    "SoulObservationPort",
    "SoulOwnerClient",
    "SoulOwnerObservation",
    "SoulPersistenceProjection",
    "SoulReplyMemoryGate",
    "SoulReplyPolicy",
    "SoulReplyVerifier",
    "SoulTranscriptItem",
    "SoulVisualFacts",
    "StrategyRecommendation",
    "TrialDraft",
    "build_soul_application_ports",
]
