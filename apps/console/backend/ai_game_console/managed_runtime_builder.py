"""Build and audit the Windows x64 AI-GAME managed runtime distribution."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .managed_runtime import MANAGED_PROTOCOL_VERSION


MANAGED_MANIFEST_NAME = "managed-runtime-manifest.json"
MANAGED_MANIFEST_SCHEMA_VERSION = 1
MANAGED_RUNTIME_NAME = "ai-game-managed-runtime"
_FORBIDDEN_PARTS = {"tests", "test", "runtime", "logs", "__pycache__"}


class ManagedRuntimeBuildError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ManagedRuntimeArtifact:
    root: Path
    entry: Path
    manifest: Path


def build_managed_runtime(project_root: Path, output_dir: Path) -> ManagedRuntimeArtifact:
    """Freeze a one-directory Windows runtime; never mutate source artifacts."""

    root = Path(project_root).resolve()
    destination = Path(output_dir).resolve() / MANAGED_RUNTIME_NAME
    backend = root / "apps" / "console" / "backend"
    frontend = root / "apps" / "console" / "frontend" / "dist"
    entry_script = backend / "ai_game_console" / "managed_entry.py"
    if not backend.is_dir() or not frontend.is_dir() or not entry_script.is_file():
        raise ManagedRuntimeBuildError("managed runtime inputs are incomplete")
    if destination.exists():
        raise ManagedRuntimeBuildError("managed runtime destination already exists")
    try:
        import PyInstaller  # noqa: F401
    except ImportError as error:
        raise ManagedRuntimeBuildError("PyInstaller is required to build the managed runtime") from error
    work = Path(output_dir).resolve() / ".managed-runtime-build"
    spec = Path(output_dir).resolve() / ".managed-runtime-spec"
    command = (
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", MANAGED_RUNTIME_NAME, "--distpath", str(Path(output_dir).resolve()),
        "--workpath", str(work), "--specpath", str(spec), "--paths", str(backend),
        "--add-data", f"{frontend}{os.pathsep}frontend", "--collect-all", "ai_game_console",
        str(entry_script),
    )
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not destination.is_dir():
        raise ManagedRuntimeBuildError("PyInstaller did not produce the managed runtime")
    try:
        _scrub_machine_build_metadata(destination)
        manifest = _write_manifest(destination, root)
        verified = verify_managed_runtime(destination, source_root=root)
        if not verified["verified"]:
            raise ManagedRuntimeBuildError("managed runtime verification failed")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(spec, ignore_errors=True)
    return ManagedRuntimeArtifact(destination, destination / f"{MANAGED_RUNTIME_NAME}.exe", manifest)


def verify_managed_runtime(runtime_root: Path, *, source_root: Path | None = None) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    manifest_path = root / MANAGED_MANIFEST_NAME
    if not root.is_dir() or not manifest_path.is_file():
        raise ManagedRuntimeBuildError("managed runtime manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManagedRuntimeBuildError("managed runtime manifest is invalid") from error
    _validate_manifest(manifest)
    declared = {item["path"]: item for item in manifest["files"]}
    actual = {
        path.relative_to(root).as_posix()
        for path in _iter_files(root)
        if path.name != MANAGED_MANIFEST_NAME
    }
    if set(declared) != actual:
        raise ManagedRuntimeBuildError("managed runtime files do not match manifest")
    for relative, entry in declared.items():
        path = root / relative
        if path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            raise ManagedRuntimeBuildError("managed runtime file hash does not match manifest")
    _assert_no_forbidden_content(root, manifest, source_root=source_root)
    return {"verified": True, "file_count": len(declared), "entry": manifest["entry"]}


def _write_manifest(runtime_root: Path, source_root: Path) -> Path:
    entry = f"{MANAGED_RUNTIME_NAME}.exe"
    if not (runtime_root / entry).is_file():
        raise ManagedRuntimeBuildError("managed runtime executable is missing")
    files = [_file_entry(runtime_root, path) for path in _iter_files(runtime_root)]
    source = _project_metadata(source_root)
    manifest = {
        "schema_version": MANAGED_MANIFEST_SCHEMA_VERSION,
        "artifact_kind": "windows_x64_self_contained_managed_runtime",
        "source": source,
        "managed_protocol_version": MANAGED_PROTOCOL_VERSION,
        "execution_api_version": "2.0",
        "platform": "windows",
        "architecture": "x64",
        "entry": entry,
        "third_party_notices": "THIRD_PARTY_NOTICES.json",
        "files": files,
    }
    notices = runtime_root / "THIRD_PARTY_NOTICES.json"
    notices.write_text(json.dumps(_third_party_notices(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    # Notices are generated before the closure is declared.
    manifest["files"] = [_file_entry(runtime_root, path) for path in _iter_files(runtime_root)]
    destination = runtime_root / MANAGED_MANIFEST_NAME
    destination.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANAGED_MANIFEST_SCHEMA_VERSION:
        raise ManagedRuntimeBuildError("managed runtime manifest schema is unsupported")
    required = {
        "artifact_kind": "windows_x64_self_contained_managed_runtime",
        "managed_protocol_version": MANAGED_PROTOCOL_VERSION,
        "execution_api_version": "2.0", "platform": "windows", "architecture": "x64",
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise ManagedRuntimeBuildError("managed runtime manifest identity is invalid")
    entry = manifest.get("entry")
    files = manifest.get("files")
    if not isinstance(entry, str) or not _safe_relative(entry) or not isinstance(files, list) or not files:
        raise ManagedRuntimeBuildError("managed runtime manifest is incomplete")
    for item in files:
        if not isinstance(item, dict) or not _safe_relative(item.get("path")):
            raise ManagedRuntimeBuildError("managed runtime manifest contains an unsafe path")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ManagedRuntimeBuildError("managed runtime manifest contains an invalid hash")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            raise ManagedRuntimeBuildError("managed runtime manifest contains an invalid size")


def _assert_no_forbidden_content(
    root: Path, manifest: dict[str, Any], *, source_root: Path | None,
) -> None:
    source_root_bytes: tuple[bytes, ...] = ()
    if source_root is not None:
        normalized = Path(source_root).resolve().as_posix()
        source_root_bytes = tuple({
            str(Path(source_root).resolve()).encode("utf-8"), normalized.encode("utf-8"),
            f"file:///{normalized}".encode("utf-8"),
            urllib.parse.quote(normalized, safe="/:").encode("utf-8"),
        })
    for item in manifest["files"]:
        path = item["path"]
        parts = set(Path(path).parts)
        if parts & _FORBIDDEN_PARTS or path.endswith((".sqlite", ".db", ".log", ".pyc")):
            raise ManagedRuntimeBuildError("managed runtime contains forbidden runtime or test content")
        contents = (root / path).read_bytes()
        if any(marker in contents for marker in source_root_bytes):
            raise ManagedRuntimeBuildError("managed runtime contains a machine absolute source path")


def _scrub_machine_build_metadata(root: Path) -> None:
    """Remove pip/uv editable-install traces before the closure is declared."""

    for path in root.rglob("*"):
        if path.is_file() and path.name in {"direct_url.json", "uv_build.json", "uv_cache.json"}:
            path.unlink()


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def _file_entry(root: Path, path: Path) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path), "size": path.stat().st_size}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: object) -> bool:
    return isinstance(path, str) and bool(path) and not Path(path).is_absolute() and ".." not in Path(path).parts and "\\" not in path


def _project_metadata(root: Path) -> dict[str, str]:
    pyproject = root / "apps" / "console" / "backend" / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    version = next((line.split("=", 1)[1].strip().strip('"') for line in text.splitlines() if line.startswith("version =")), "unknown")
    revision = _git(root, "rev-parse", "HEAD") or "unavailable"
    return {"name": "ai-game-console-backend", "version": version, "revision": revision}


def _git(root: Path, *args: str) -> str | None:
    completed = subprocess.run(("git", "-C", str(root), *args), capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _third_party_notices() -> list[dict[str, str]]:
    notices: list[dict[str, str]] = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower() if item.metadata["Name"] else ""):
        name = distribution.metadata["Name"]
        if name:
            notices.append({"name": name, "version": distribution.version, "license": distribution.metadata.get("License", "UNKNOWN")})
    return notices


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify the AI-GAME managed runtime.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--project-root", required=True)
    build.add_argument("--output-dir", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--runtime-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            artifact = build_managed_runtime(Path(args.project_root), Path(args.output_dir))
            result: dict[str, Any] = {"root": str(artifact.root), "entry": str(artifact.entry), "manifest": str(artifact.manifest)}
        else:
            result = verify_managed_runtime(Path(args.runtime_root))
    except ManagedRuntimeBuildError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


__all__ = ["MANAGED_MANIFEST_NAME", "ManagedRuntimeArtifact", "ManagedRuntimeBuildError", "build_managed_runtime", "verify_managed_runtime"]


if __name__ == "__main__":
    raise SystemExit(main())
