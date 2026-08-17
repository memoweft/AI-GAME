from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .domain import ArtifactRef, Observation


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ArtifactStore(Protocol):
    def put(
        self,
        *,
        job_id: str,
        label: str,
        sequence: int,
        observation: Observation,
    ) -> ArtifactRef | None: ...


class LocalArtifactStore:
    """Content-addressed local evidence; SQLite stores only the returned reference."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def put(
        self,
        *,
        job_id: str,
        label: str,
        sequence: int,
        observation: Observation,
    ) -> ArtifactRef | None:
        payload = observation.payload
        if payload is None:
            return None
        if not _SAFE_SEGMENT.fullmatch(job_id) or not _SAFE_SEGMENT.fullmatch(label):
            raise ValueError("artifact job id or label is unsafe")
        if sequence < 0:
            raise ValueError("artifact sequence must be non-negative")

        digest = hashlib.sha256(payload).hexdigest()
        suffix = _suffix_for(observation.mime_type)
        relative = Path(job_id) / f"{sequence:06d}-{label}-{digest}{suffix}"
        destination = (self.root / relative).resolve()
        if self.root not in destination.parents:
            raise ValueError("artifact path escaped its configured root")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise RuntimeError("artifact hash collision or corruption")
        else:
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()

        return ArtifactRef(
            sha256=digest,
            relative_path=relative.as_posix(),
            size_bytes=len(payload),
            mime_type=observation.mime_type,
        )


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    def put(
        self,
        *,
        job_id: str,
        label: str,
        sequence: int,
        observation: Observation,
    ) -> ArtifactRef | None:
        if observation.payload is None:
            return None
        digest = hashlib.sha256(observation.payload).hexdigest()
        relative = f"{job_id}/{sequence:06d}-{label}-{digest}{_suffix_for(observation.mime_type)}"
        self.payloads[relative] = observation.payload
        return ArtifactRef(
            sha256=digest,
            relative_path=relative,
            size_bytes=len(observation.payload),
            mime_type=observation.mime_type,
        )


def _suffix_for(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "application/json": ".json",
    }.get(mime_type, ".bin")
