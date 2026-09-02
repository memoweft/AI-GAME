"""Emulator-only profile and general Android UI runtime primitives.

This package owns emulator configuration and identity checks. It does not open
an ADB connection merely by being imported.
"""

from .domain import (
    EmulatorFingerprint,
    EmulatorProbe,
    EmulatorProfile,
    EmulatorProfileState,
)
from .production import (
    ProductionEmulatorComposition,
    ProductionFrameBindings,
    compose_production_emulator_runtime,
)
from .service import EmulatorProfileService, EmulatorProfileServiceError
from .store import EmulatorProfileStore

__all__ = [
    "EmulatorFingerprint",
    "EmulatorProbe",
    "EmulatorProfile",
    "EmulatorProfileState",
    "EmulatorProfileService",
    "EmulatorProfileServiceError",
    "EmulatorProfileStore",
    "ProductionEmulatorComposition",
    "ProductionFrameBindings",
    "compose_production_emulator_runtime",
]
