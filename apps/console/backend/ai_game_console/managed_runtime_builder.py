"""Build and audit the Windows x64 AI-GAME managed runtime distribution."""

from __future__ import annotations

import argparse
import email
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .managed_runtime import MANAGED_PROTOCOL_VERSION


MANAGED_MANIFEST_NAME = "managed-runtime-manifest.json"
MANAGED_MANIFEST_SCHEMA_VERSION = 1
MANAGED_RUNTIME_NAME = "ai-game-managed-runtime"
_FORBIDDEN_PARTS = {"tests", "test", "runtime", "logs", "__pycache__"}
# A drive letter must not be the final character of an URL scheme (for example
# the ``s:/`` fragment in ``https://pypi.org``).
_ABSOLUTE_MACHINE_PATH = re.compile(rb"(?i)(?:(?<![a-z0-9+.-])[a-z]:[\\/]|file:///+[a-z]:/)")
_MANIFEST_KEYS = {
    "schema_version", "artifact_kind", "source", "managed_protocol_version",
    "execution_api_version", "platform", "architecture", "entry",
    "third_party_notices", "files",
}
_NOTICE_KEYS = {
    "name", "version", "license_expression", "license_metadata", "source",
    "legal_review", "evidence",
}
_NOTICE_EVIDENCE_KEYS = {"frozen_top_level_modules", "locked_runtime_dependency"}
_LICENSE_METADATA_KEYS = {"license", "license_expression"}
# Consumer verification cannot rely on the build environment's installed
# distribution metadata. Keep the import ownership for the locked managed
# runtime closure explicit and fail closed when that closure changes.
_MANAGED_DISTRIBUTION_TOP_LEVEL_MODULES = {
    "annotated-doc": ("annotated_doc",),
    "annotated-types": ("annotated_types",),
    "anyio": ("anyio",),
    "click": ("click",),
    "colorama": ("colorama",),
    "fastapi": ("fastapi",),
    "h11": ("h11",),
    "idna": ("idna",),
    "pydantic": ("pydantic",),
    "pydantic-core": ("pydantic_core",),
    "starlette": ("starlette",),
    "typing-extensions": ("typing_extensions",),
    "typing-inspection": ("typing_inspection",),
    "uvicorn": ("uvicorn",),
}


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
        "--exclude-module", "pytest", "--exclude-module", "_pytest",
        "--exclude-module", "pluggy", "--exclude-module", "iniconfig",
        "--exclude-module", "pygments",
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
    entry = root / manifest["entry"]
    notices = root / manifest["third_party_notices"]
    if not entry.is_file() or manifest["entry"] not in declared:
        raise ManagedRuntimeBuildError("managed runtime entry is not in the closure")
    if not notices.is_file() or manifest["third_party_notices"] not in declared:
        raise ManagedRuntimeBuildError("managed runtime notices are not in the closure")
    _assert_no_forbidden_content(root, manifest, source_root=source_root)
    _validate_notices(root, manifest, source_root=source_root)
    return {"verified": True, "file_count": len(declared), "entry": manifest["entry"]}


def _write_manifest(runtime_root: Path, source_root: Path) -> Path:
    entry = f"{MANAGED_RUNTIME_NAME}.exe"
    if not (runtime_root / entry).is_file():
        raise ManagedRuntimeBuildError("managed runtime executable is missing")
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
        "files": [],
    }
    notices = runtime_root / "THIRD_PARTY_NOTICES.json"
    notices.write_text(
        json.dumps(_third_party_notices(runtime_root, source_root), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    # Notices are generated before the closure is declared.
    manifest["files"] = sorted(
        (
            _file_entry(runtime_root, path)
            for path in _iter_files(runtime_root)
            if path.name != MANAGED_MANIFEST_NAME
        ),
        key=lambda item: item["path"],
    )
    destination = runtime_root / MANAGED_MANIFEST_NAME
    destination.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ManagedRuntimeBuildError("managed runtime manifest schema is unsupported")
    if manifest.get("schema_version") != MANAGED_MANIFEST_SCHEMA_VERSION:
        raise ManagedRuntimeBuildError("managed runtime manifest schema is unsupported")
    required = {
        "artifact_kind": "windows_x64_self_contained_managed_runtime",
        "managed_protocol_version": MANAGED_PROTOCOL_VERSION,
        "execution_api_version": "2.0", "platform": "windows", "architecture": "x64",
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise ManagedRuntimeBuildError("managed runtime manifest identity is invalid")
    source = manifest.get("source")
    entry = manifest.get("entry")
    notices = manifest.get("third_party_notices")
    files = manifest.get("files")
    if (
        not isinstance(source, dict) or set(source) != {"name", "version", "revision"}
        or not all(isinstance(source.get(key), str) and source[key] for key in source)
        or not re.fullmatch(r"[0-9a-f]{40}|unavailable", source["revision"])
        or entry != f"{MANAGED_RUNTIME_NAME}.exe" or not _safe_relative(entry)
        or not isinstance(notices, str) or not _safe_relative(notices)
        or not isinstance(files, list) or not files
    ):
        raise ManagedRuntimeBuildError("managed runtime manifest is incomplete")
    seen: set[str] = set()
    previous = ""
    for item in files:
        if not isinstance(item, dict) or not _safe_relative(item.get("path")):
            raise ManagedRuntimeBuildError("managed runtime manifest contains an unsafe path")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ManagedRuntimeBuildError("managed runtime manifest contains an invalid hash")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            raise ManagedRuntimeBuildError("managed runtime manifest contains an invalid size")
        if item["path"] in seen or item["path"] <= previous:
            raise ManagedRuntimeBuildError("managed runtime manifest paths are not canonical")
        seen.add(item["path"])
        previous = item["path"]


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
        # Inspect deterministic source/metadata resources. Native Microsoft
        # DLLs legitimately contain drive-like diagnostic strings that are not
        # build-machine provenance and cannot be treated as paths.
        text_like = Path(path).suffix.lower() in {".json", ".txt", ".html", ".md", ".cfg", ".ini"}
        if text_like and _ABSOLUTE_MACHINE_PATH.search(contents):
            raise ManagedRuntimeBuildError(f"managed runtime contains a machine absolute path: {path}")
        if any(marker in contents for marker in source_root_bytes):
            raise ManagedRuntimeBuildError("managed runtime contains a machine absolute source path")


def _scrub_machine_build_metadata(root: Path) -> None:
    """Remove pip/uv editable-install traces before the closure is declared."""

    for path in root.rglob("*"):
        if path.is_file() and (
            path.name in {"direct_url.json", "uv_build.json", "uv_cache.json"}
            or path.suffix == ".map"
        ):
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
    return (
        isinstance(path, str) and bool(path) and path == path.strip()
        and not Path(path).is_absolute() and ".." not in Path(path).parts
        and "\\" not in path and path != "." and not path.startswith("./")
    )


def _distribution_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _project_metadata(root: Path) -> dict[str, str]:
    pyproject = root / "apps" / "console" / "backend" / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    version = next((line.split("=", 1)[1].strip().strip('"') for line in text.splitlines() if line.startswith("version =")), "unknown")
    revision = _git(root, "rev-parse", "HEAD") or "unavailable"
    return {"name": "ai-game-console-backend", "version": version, "revision": revision}


def _git(root: Path, *args: str) -> str | None:
    completed = subprocess.run(("git", "-C", str(root), *args), capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _third_party_notices(runtime_root: Path, source_root: Path) -> list[dict[str, Any]]:
    """Describe the locked runtime closure, with PYZ module evidence.

    PyInstaller commonly stores pure Python distributions only in ``PYZ.pyz``;
    relying on copied ``.dist-info`` therefore omits FastAPI/Uvicorn and their
    transitive runtime dependencies.  The lock gives a deterministic runtime
    dependency graph and the final archive proves at least one top-level module
    from every declared distribution is actually frozen.
    """

    locked = _locked_runtime_packages(source_root)
    if not locked:
        raise ManagedRuntimeBuildError("runtime dependency lock is empty")
    frozen = _frozen_modules(runtime_root / f"{MANAGED_RUNTIME_NAME}.exe")
    installed = {
        _distribution_key(item.metadata["Name"]): item
        for item in importlib.metadata.distributions()
        if item.metadata.get("Name")
    }
    notices: list[dict[str, Any]] = []
    for package in locked:
        name = package["name"]
        key = _distribution_key(name)
        top_levels = _MANAGED_DISTRIBUTION_TOP_LEVEL_MODULES.get(key)
        if top_levels is None:
            raise ManagedRuntimeBuildError("runtime dependency import ownership is unavailable")
        if any(
            not any(item == module or item.startswith(module + ".") for item in frozen)
            for module in top_levels
        ):
            raise ManagedRuntimeBuildError("runtime dependency is absent from the frozen archive")
        distribution = installed.get(key)
        fields = distribution.metadata if distribution is not None else None
        license_expression = (
            fields.get("License-Expression") or fields.get("License")
            if fields is not None else None
        )
        source = package.get("source")
        if not isinstance(source, dict) or not source:
            raise ManagedRuntimeBuildError("runtime dependency source is unavailable")
        notices.append({
            "name": name,
            "version": package["version"],
            "license_expression": license_expression,
            "license_metadata": ({"license": fields.get("License"), "license_expression": fields.get("License-Expression")} if fields is not None else {}),
            "source": source,
            "legal_review": "metadata-only" if license_expression else "required",
            "evidence": {
                "frozen_top_level_modules": list(top_levels),
                "locked_runtime_dependency": True,
            },
        })
    return sorted(notices, key=lambda item: item["name"].casefold())


def _locked_runtime_packages(source_root: Path) -> list[dict[str, Any]]:
    lock_path = source_root / "apps" / "console" / "backend" / "uv.lock"
    if not lock_path.is_file():
        return []
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ManagedRuntimeBuildError("runtime dependency lock is unavailable") from error
    records = {str(item.get("name", "")).casefold(): item for item in lock.get("package", []) if isinstance(item, dict)}
    direct = {"fastapi", "uvicorn"}
    pending = list(direct)
    closure: set[str] = set()
    while pending:
        name = pending.pop().casefold()
        if name in closure:
            continue
        record = records.get(name)
        if record is None:
            raise ManagedRuntimeBuildError("runtime dependency lock is incomplete")
        closure.add(name)
        for dependency in record.get("dependencies", []):
            dep_name = dependency.get("name") if isinstance(dependency, dict) else dependency
            if isinstance(dep_name, str):
                pending.append(dep_name)
    result: list[dict[str, Any]] = []
    for name in closure:
        record = records[name]
        version = record.get("version")
        if not isinstance(version, str) or not version:
            raise ManagedRuntimeBuildError("runtime dependency version is unavailable")
        result.append({"name": str(record["name"]), "version": version, "source": record.get("source")})
    return result


def _frozen_modules(entry: Path) -> set[str]:
    completed = subprocess.run(
        (sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer", "-r", "-b", str(entry)),
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise ManagedRuntimeBuildError("PyInstaller archive inventory is unavailable")
    return {line.strip() for line in completed.stdout.splitlines() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", line.strip())}


def _validate_notices(root: Path, manifest: dict[str, Any], *, source_root: Path | None) -> None:
    try:
        notices = json.loads((root / manifest["third_party_notices"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManagedRuntimeBuildError("managed runtime notices are invalid") from error
    if not isinstance(notices, list):
        raise ManagedRuntimeBuildError("managed runtime notices schema is unsupported")
    if not notices:
        raise ManagedRuntimeBuildError("managed runtime notices contain no distributions")
    names: set[str] = set()
    frozen = _frozen_modules(root / manifest["entry"])
    for item in notices:
        if not isinstance(item, dict) or set(item) != _NOTICE_KEYS:
            raise ManagedRuntimeBuildError("managed runtime notices schema is unsupported")
        name, version, source, evidence = item.get("name"), item.get("version"), item.get("source"), item.get("evidence")
        key = _distribution_key(name) if _nonblank_string(name) else ""
        if (
            not key or key in names or key not in _MANAGED_DISTRIBUTION_TOP_LEVEL_MODULES
            or not _nonblank_string(version) or not isinstance(source, dict) or not source
            or any(not _nonblank_string(field) or not _nonblank_string(value) for field, value in source.items())
            or not isinstance(evidence, dict) or set(evidence) != _NOTICE_EVIDENCE_KEYS
            or evidence.get("locked_runtime_dependency") is not True
        ):
            raise ManagedRuntimeBuildError("managed runtime notices schema is unsupported")
        modules = evidence.get("frozen_top_level_modules")
        if not isinstance(modules, list) or any(not _nonblank_string(module) for module in modules):
            raise ManagedRuntimeBuildError("managed runtime notice evidence is invalid")
        if tuple(modules) != _MANAGED_DISTRIBUTION_TOP_LEVEL_MODULES[key]:
            raise ManagedRuntimeBuildError("managed runtime notice module attribution is invalid")
        if any(
            not any(item == module or item.startswith(module + ".") for item in frozen)
            for module in modules
        ):
            raise ManagedRuntimeBuildError("managed runtime notice evidence does not match frozen archive")
        license_expression = item.get("license_expression")
        license_metadata = item.get("license_metadata")
        legal_review = item.get("legal_review")
        if license_expression is not None and not _nonblank_string(license_expression):
            raise ManagedRuntimeBuildError("managed runtime notice license provenance is invalid")
        if (
            not isinstance(license_metadata, dict)
            or any(key not in _LICENSE_METADATA_KEYS for key in license_metadata)
            or any(value is not None and not _nonblank_string(value) for value in license_metadata.values())
            or legal_review is not None and not _nonblank_string(legal_review)
        ):
            raise ManagedRuntimeBuildError("managed runtime notice license provenance is invalid")
        has_license_metadata = any(_nonblank_string(value) for value in license_metadata.values())
        if not has_license_metadata and not _nonblank_string(legal_review):
            raise ManagedRuntimeBuildError("managed runtime notice license provenance is unavailable")
        names.add(key)
    if names != set(_MANAGED_DISTRIBUTION_TOP_LEVEL_MODULES):
        raise ManagedRuntimeBuildError("managed runtime notices do not match the dependency closure")


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
