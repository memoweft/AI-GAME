"""Build and verify a bounded U9 release-candidate archive.

The archive is an installation artifact, not deployment evidence.  It contains
only the local console source, the already-built browser bundle, sanitized
configuration templates, and the canonical policy documents that describe the
release contract.  Runtime databases, logs, local configuration, credentials,
and model files intentionally stay outside the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


MANIFEST_NAME = "RELEASE-MANIFEST.json"
NOTES_NAME = "RELEASE-NOTES.md"
MANIFEST_SCHEMA_VERSION = 1
_EXCLUDED_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache", "node_modules"})
_POLICY_DOCUMENTS = (
    "docs/product/01_PRODUCT_SPEC.md",
    "docs/product/04_AUTONOMY_AND_LEARNING.md",
    "docs/product/06_IMPLEMENTATION_ROADMAP.md",
    "docs/product/07_ACCEPTANCE_AND_EVIDENCE.md",
    "docs/product/09_DECISIONS_AND_OPEN_QUESTIONS.md",
    "docs/product/work-orders/U9_PRODUCTION_HARDENING.md",
)
_ROOT_LAUNCHERS = ("启动控制台.cmd", "停止控制台.cmd")
_RELEASE_SCRIPTS = ("scripts/console.ps1", "scripts/production-release.ps1")
_SCHEMA_SOURCES = (
    "apps/console/backend/ai_game_console/application_runtime/store.py",
    "apps/console/backend/ai_game_console/experience_runtime/store.py",
    "apps/console/backend/ai_game_console/goal_runtime/store.py",
    "apps/console/backend/ai_game_console/mobile_agent/store.py",
    "apps/console/backend/ai_game_console/runtime_adapters/sqlite/store.py",
)


class ReleaseBuildError(RuntimeError):
    """The source tree cannot safely produce or verify a release archive."""


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    release_id: str
    archive: Path
    manifest: Path
    checksum: Path
    archive_sha256: str
    source_fingerprint: str

    def payload(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "archive": str(self.archive),
            "manifest": str(self.manifest),
            "checksum": str(self.checksum),
            "archive_sha256": self.archive_sha256,
            "source_fingerprint": self.source_fingerprint,
        }


def build_release_candidate(
    project_root: Path,
    output_dir: Path,
    *,
    created_at: datetime | None = None,
) -> ReleaseArtifact:
    """Create one immutable, locally installable release-candidate archive.

    The caller must build the browser bundle before invoking this function.
    Refusing an absent or stale bundle keeps a source-only archive from being
    presented as a runnable console release.
    """

    root = Path(project_root).resolve()
    destination = Path(output_dir).resolve()
    _assert_project_layout(root)
    _assert_frontend_bundle_is_current(root)

    files = _collect_release_files(root)
    source_fingerprint = _source_fingerprint(root, files)
    project = _project_metadata(root)
    source_control = _source_control(root)
    source_state = "clean" if source_control["dirty"] is False else "workspace_snapshot"
    release_id = (
        f"{_slug(str(project['name']))}-{project['version']}-"
        f"{source_state}-{source_fingerprint[:12]}"
    )

    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{release_id}.zip"
    manifest_path = destination / f"{release_id}.manifest.json"
    checksum_path = destination / f"{release_id}.zip.sha256"
    for existing in (archive, manifest_path, checksum_path):
        if existing.exists():
            raise ReleaseBuildError(
                f"release artifact already exists and will not be overwritten: {existing}"
            )

    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    notes = _release_notes(release_id, project, source_state)
    file_entries = [_file_entry(root, path) for path in files]
    file_entries.append(_bytes_entry(NOTES_NAME, notes.encode("utf-8")))
    file_entries.sort(key=lambda item: item["path"])
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_id": release_id,
        "created_at": timestamp,
        "artifact_kind": "local_installation_candidate",
        "deployment_status": "NOT_DEPLOYED",
        "deployment_note": (
            "This archive is a release candidate only. Installation, target approval, "
            "long-run observation, failure injection, upgrade, rollback, and restore "
            "remain separate U9 evidence."
        ),
        "application": project,
        "source_control": source_control,
        "source_fingerprint": source_fingerprint,
        "runtime_contract": {
            "normal_launcher": "scripts/console.ps1",
            "default_runtime_mode": "kernel_active",
            "listener": "127.0.0.1:4310",
            "data_root": "runtime/console",
        },
        "included_policy_documents": list(_POLICY_DOCUMENTS),
        "included_schema_sources": list(_SCHEMA_SOURCES),
        "included_model_binding_templates": [
            item["path"]
            for item in file_entries
            if item["path"].startswith("config/") and item["path"].endswith(".env.example")
        ],
        "files": file_entries,
    }
    manifest_bytes = _canonical_json(manifest)
    temporary_archive = archive.with_suffix(".zip.tmp")
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_checksum = checksum_path.with_suffix(".sha256.tmp")
    try:
        _write_archive(temporary_archive, root, files, notes, manifest_bytes)
        archive_sha256 = _sha256_file(temporary_archive)
        sidecar = {**manifest, "archive": archive.name, "archive_sha256": archive_sha256}
        temporary_manifest.write_bytes(_canonical_json(sidecar))
        temporary_checksum.write_text(f"{archive_sha256}  {archive.name}\n", encoding="ascii")
        os.replace(temporary_archive, archive)
        os.replace(temporary_manifest, manifest_path)
        os.replace(temporary_checksum, checksum_path)
    finally:
        for temporary in (temporary_archive, temporary_manifest, temporary_checksum):
            if temporary.exists():
                temporary.unlink()

    return ReleaseArtifact(
        release_id=release_id,
        archive=archive,
        manifest=manifest_path,
        checksum=checksum_path,
        archive_sha256=archive_sha256,
        source_fingerprint=source_fingerprint,
    )


def verify_release_candidate(archive_path: Path) -> dict[str, Any]:
    """Verify the archive's manifest, entry hashes, and companion checksum."""

    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise ReleaseBuildError(f"release archive does not exist: {archive}")
    checksum_path = archive.with_suffix(".zip.sha256")
    if not checksum_path.is_file():
        raise ReleaseBuildError(f"release checksum does not exist: {checksum_path}")
    expected_checksum = _read_checksum(checksum_path, archive.name)
    actual_checksum = _sha256_file(archive)
    if actual_checksum != expected_checksum:
        raise ReleaseBuildError("release archive checksum does not match its companion file")

    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                raise ReleaseBuildError("release archive has duplicate entry names")
            if MANIFEST_NAME not in names:
                raise ReleaseBuildError("release archive has no manifest")
            manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
            _validate_manifest(manifest)
            files = manifest["files"]
            declared_paths = {str(item["path"]) for item in files}
            actual_paths = set(names) - {MANIFEST_NAME}
            if declared_paths != actual_paths:
                raise ReleaseBuildError("release archive entries do not match its manifest")
            for item in files:
                path = str(item["path"])
                _assert_archive_path(path)
                content = bundle.read(path)
                if len(content) != int(item["size"]):
                    raise ReleaseBuildError(f"release entry size mismatch: {path}")
                if hashlib.sha256(content).hexdigest() != str(item["sha256"]):
                    raise ReleaseBuildError(f"release entry digest mismatch: {path}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise ReleaseBuildError(f"release archive cannot be read: {archive}") from error

    return {
        "verified": True,
        "release_id": manifest["release_id"],
        "archive": str(archive),
        "archive_sha256": actual_checksum,
        "file_count": len(manifest["files"]),
        "deployment_status": manifest["deployment_status"],
    }


def _assert_project_layout(root: Path) -> None:
    required = (
        "apps/console/backend/pyproject.toml",
        "apps/console/backend/ai_game_console/__init__.py",
        "apps/console/frontend/dist/index.html",
        *_RELEASE_SCRIPTS,
        "config/model-runtime.env.example",
        *_SCHEMA_SOURCES,
        *_POLICY_DOCUMENTS,
        *_ROOT_LAUNCHERS,
    )
    missing = [item for item in required if not (root / item).is_file()]
    if missing:
        raise ReleaseBuildError(
            "project cannot produce a release candidate; missing: " + ", ".join(missing)
        )


def _assert_frontend_bundle_is_current(root: Path) -> None:
    dist_index = root / "apps/console/frontend/dist/index.html"
    source_root = root / "apps/console/frontend/src"
    if not source_root.is_dir():
        raise ReleaseBuildError(f"frontend source directory does not exist: {source_root}")
    source_files = [
        *(_iter_files(source_root)),
        *(
            root / "apps/console/frontend" / item
            for item in ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")
            if (root / "apps/console/frontend" / item).is_file()
        ),
    ]
    newest_source = max(path.stat().st_mtime_ns for path in source_files)
    if newest_source > dist_index.stat().st_mtime_ns:
        raise ReleaseBuildError(
            "frontend dist is older than its source; run scripts/console.ps1 build first"
        )


def _collect_release_files(root: Path) -> tuple[Path, ...]:
    selected: list[Path] = []
    selected.extend(_iter_files(root / "apps/console/backend"))
    selected.extend(_iter_files(root / "apps/console/frontend"))
    selected.extend(sorted((root / "config").glob("*.env.example")))
    selected.extend(root / item for item in _POLICY_DOCUMENTS)
    selected.extend(root / item for item in _RELEASE_SCRIPTS)
    selected.extend(root / item for item in _ROOT_LAUNCHERS)
    selected.append(root / "README.md") if (root / "README.md").is_file() else None

    unique: dict[str, Path] = {}
    for candidate in selected:
        if not candidate.is_file():
            raise ReleaseBuildError(f"release input is not a file: {candidate}")
        relative = candidate.relative_to(root).as_posix()
        _assert_archive_path(relative)
        if candidate.is_symlink():
            raise ReleaseBuildError(f"release input may not be a symbolic link: {relative}")
        unique[relative] = candidate
    return tuple(unique[key] for key in sorted(unique))


def _iter_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        raise ReleaseBuildError(f"release input directory does not exist: {directory}")
    for candidate in sorted(directory.rglob("*")):
        relative_parts = candidate.relative_to(directory).parts
        if any(part in _EXCLUDED_DIRECTORY_NAMES for part in relative_parts):
            continue
        if candidate.is_symlink():
            raise ReleaseBuildError(f"release input may not be a symbolic link: {candidate}")
        if candidate.is_file() and candidate.suffix not in {".pyc", ".pyo"}:
            yield candidate


def _project_metadata(root: Path) -> dict[str, str]:
    with (root / "apps/console/backend/pyproject.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ReleaseBuildError("backend pyproject.toml has no project metadata")
    name = str(project.get("name") or "").strip()
    version = str(project.get("version") or "").strip()
    if not name or not version:
        raise ReleaseBuildError("backend pyproject.toml needs name and version for release packaging")
    return {"name": name, "version": version}


def _source_control(root: Path) -> dict[str, Any]:
    revision = _run_git(root, "rev-parse", "HEAD")
    dirty_output = _run_git(root, "status", "--porcelain", "--untracked-files=normal")
    if revision is None or dirty_output is None:
        return {"revision": None, "dirty": None, "state": "unavailable"}
    return {
        "revision": revision.strip(),
        "dirty": bool(dirty_output.strip()),
        "state": "dirty" if dirty_output.strip() else "clean",
    }


def _run_git(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _source_fingerprint(root: Path, files: Iterable[Path]) -> str:
    entries = [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)}
        for path in files
    ]
    # File content and relative archive names make this portable across hosts.
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_entry(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _bytes_entry(path: str, content: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}


def _write_archive(
    destination: Path,
    root: Path,
    files: Iterable[Path],
    notes: str,
    manifest: bytes,
) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(root).as_posix())
        bundle.writestr(NOTES_NAME, notes.encode("utf-8"))
        bundle.writestr(MANIFEST_NAME, manifest)


def _release_notes(release_id: str, project: dict[str, str], source_state: str) -> str:
    return "\n".join(
        (
            f"# AI-GAME release candidate: {release_id}",
            "",
            f"Application: {project['name']} {project['version']}",
            f"Source state: {source_state}",
            "",
            "This archive is a local installation candidate, not deployment proof.",
            "It is intentionally local-only (`127.0.0.1`) and excludes runtime data,",
            "credentials, model weights, screenshots, and logs.",
            "",
            "Before operating it on a new target, complete the U9 approved install plan",
            "and record fresh-install, upgrade, rollback, restore, long-run, and failure",
            "injection evidence. Do not use a source checkout alone as a data rollback.",
            "",
        )
    )


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _read_checksum(path: Path, archive_name: str) -> str:
    line = path.read_text(encoding="ascii").strip()
    digest, separator, filename = line.partition("  ")
    if not separator or filename != archive_name or len(digest) != 64:
        raise ReleaseBuildError(f"release checksum has an invalid format: {path}")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ReleaseBuildError(f"release checksum is not hexadecimal: {path}")
    return digest


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ReleaseBuildError("release manifest is not an object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ReleaseBuildError("release manifest schema is unsupported")
    if not isinstance(manifest.get("release_id"), str) or not manifest["release_id"]:
        raise ReleaseBuildError("release manifest has no release id")
    if manifest.get("deployment_status") != "NOT_DEPLOYED":
        raise ReleaseBuildError("release manifest makes an unsupported deployment claim")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseBuildError("release manifest has no files")
    for item in files:
        if not isinstance(item, dict):
            raise ReleaseBuildError("release manifest contains an invalid file entry")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise ReleaseBuildError("release manifest contains an incomplete file entry")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ReleaseBuildError(f"release manifest has an invalid digest for {path}")


def _assert_archive_path(path: str) -> None:
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        raise ReleaseBuildError(f"release path is unsafe: {path!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    lowered = value.lower()
    return "".join(character if character.isalnum() else "-" for character in lowered).strip("-")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or verify an AI-GAME U9 release candidate.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--project-root", required=True)
    build.add_argument("--output-dir", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "build":
            result: dict[str, Any] = build_release_candidate(
                Path(arguments.project_root), Path(arguments.output_dir)
            ).payload()
        else:
            result = verify_release_candidate(Path(arguments.archive))
    except ReleaseBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
