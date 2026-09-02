"""Pure, versioned contracts for the Android UI agent.

This package deliberately owns neither a top-level Task lifecycle nor a worker
thread.  Composition supplies canonical control, observations and dispatch.
"""

from .domain import (
    AndroidUiAction,
    AndroidUiActionIntent,
    CanonicalSnapshot,
    Criterion,
    GoalVerificationRecord,
    GroundedUiNode,
    ObservationArtifactGroundingPort,
    ObservationEnvelope,
    ObservationGroundingQuery,
    OrderedWaypoint,
    OwnerBinding,
    TrustedObservationGroundingManifest,
)
from .runner import AndroidUiAgentV1Handler, AndroidUiHandlerRegistry, DispatchReceipt
from .semantic_verification import EvidenceBoundSemanticVerifier
from .store import (
    CheckpointBaselineAttestationPort,
    CheckpointBaselineQuery,
    SQLiteAndroidUiStepStore,
    TrustedCheckpointBaselineAttestation,
)

__all__ = [
    "AndroidUiAction",
    "AndroidUiActionIntent",
    "AndroidUiAgentV1Handler",
    "AndroidUiHandlerRegistry",
    "CanonicalSnapshot",
    "CheckpointBaselineAttestationPort",
    "CheckpointBaselineQuery",
    "DispatchReceipt",
    "Criterion",
    "EvidenceBoundSemanticVerifier",
    "GoalVerificationRecord",
    "GroundedUiNode",
    "ObservationArtifactGroundingPort",
    "ObservationEnvelope",
    "ObservationGroundingQuery",
    "OwnerBinding",
    "OrderedWaypoint",
    "SQLiteAndroidUiStepStore",
    "TrustedCheckpointBaselineAttestation",
    "TrustedObservationGroundingManifest",
]
