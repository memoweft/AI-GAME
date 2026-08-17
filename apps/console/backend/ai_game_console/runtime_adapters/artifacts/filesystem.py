from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

from ...runtime_kernel.observation import ArtifactRef


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_EXTENSIONS = {
    "image/png": ".png",
    "application/xml": ".xml",
    "text/xml": ".xml",
}


class FilesystemArtifactStore:
    """Atomic local artifact storage rooted outside the Runtime database."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write(
        self, *, artifact_id: str, content_type: str, content: bytes
    ) -> ArtifactRef:
        if not isinstance(content, bytes) or not content:
            raise ValueError("artifact content must be non-empty bytes")
        parts = artifact_id.split("/")
        if not parts or any(not _SAFE_COMPONENT.fullmatch(part) for part in parts):
            raise ValueError("artifact_id contains an unsafe component")
        extension = _EXTENSIONS.get(content_type, ".bin")
        reference = "/".join((*parts[:-1], parts[-1] + extension))
        destination = self.root.joinpath(*reference.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Linking a fully flushed temp file is an atomic, no-overwrite
            # finalize on the same filesystem. A duplicate immutable artifact
            # therefore fails without replacing committed history.
            os.link(temporary, destination)
            temporary.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()
        return ArtifactRef(
            reference=reference,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def delete(self, artifact: ArtifactRef) -> None:
        path = self.resolve(artifact)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        parent = path.parent
        if parent != self.root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                pass

    def resolve(self, artifact: ArtifactRef) -> Path:
        root = self.root.resolve()
        candidate = self.root.joinpath(*artifact.reference.split("/")).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError("artifact reference escapes the configured root")
        return candidate

    def read(self, artifact: ArtifactRef) -> bytes:
        content = self.resolve(artifact).read_bytes()
        if len(content) != artifact.size_bytes:
            raise RuntimeError("artifact size does not match its reference")
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise RuntimeError("artifact checksum does not match its reference")
        return content
