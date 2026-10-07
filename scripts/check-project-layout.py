#!/usr/bin/env python3
"""Read-only checks for the first-party documentation layout.

This checks a small declared set of project entry points. It does not read
runtime data, scan secrets, install packages, run git, or verify application
behavior. Use --root only for a repository you intend to inspect.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAIN = (
    'README.md', 'AGENTS.md', '.gitignore',
    'docs/START_HERE.md', 'docs/PROJECT_DIRECTION.md',
    'docs/CURRENT_STATE.md', 'docs/WORKLOG.md', 'docs/DECISIONS.md',
    'docs/DEVELOPMENT_LOOP.md', 'docs/OWNER_TESTS.md',
    'docs/FRONTEND_PLAN.md', 'docs/DSH_UPGRADE_POLICY.md',
    'docs/REPOSITORY_LAYOUT.md', 'docs/SETUP.md',
    'docs/CODEX_START_PROMPT.txt', 'docs/WORKSPACE_MAP.example.json',
    'docs/DOCUMENT_MIGRATION.json',
    'docs/tasks/S00-A.md', 'docs/DEVELOPMENT_ENVIRONMENT.md',
    'docs/evidence/S00-A/VALIDATION.md',
)
COMPONENT = ('README.md', 'AGENTS.md', '.gitignore', 'docs/COMPONENT.md')
PRIVATE_RULES = ('/.local/', '/.private/', '/.artifacts/', '/workspace.local.json')

def inspect(root: Path) -> dict[str, object]:
    root = root.resolve()
    errors: list[str] = []
    kind = 'weftmate' if (root / 'src').is_dir() or (root / 'docs/PROJECT_DIRECTION.md').is_file() else 'weftmod'
    required = MAIN if kind == 'weftmate' else COMPONENT
    if not root.is_dir():
        return {'ok': False, 'kind': kind, 'errors': ['repository root is missing']}
    checked = 0
    for rel in required:
        p = root / rel
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root):
            errors.append(f'missing or unsafe required file: {rel}')
            continue
        checked += 1
        try:
            text = p.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            errors.append(f'cannot read {rel}: {type(exc).__name__}')
            continue
        if rel in ('README.md', 'AGENTS.md'):
            if re.search(r'\.\./\.\./(?:AGENTS|PROJECT_DIRECTION|CURRENT_STATE)', text):
                errors.append(f'entry requires a parent document: {rel}')
            if re.search(r'(?i)D:[/\\](?:AIProjects|PersonalAssistant|MemoWeft)', text):
                errors.append(f'entry refers to a fixed machine path: {rel}')
        if not rel.endswith('.md'):
            continue
        prose = re.sub(r'```.*?```', '', text, flags=re.S)
        for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', prose):
            target = target.strip().split(' "', 1)[0].strip('<>')
            try:
                parsed = urlsplit(target)
            except ValueError:
                errors.append(f'invalid link in {rel}')
                continue
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            dst = (p.parent / unquote(parsed.path)).resolve()
            if not dst.is_relative_to(root):
                errors.append(f'current document link leaves repository: {rel} -> {target}')
            elif not dst.exists() or dst.is_symlink():
                errors.append(f'broken local link: {rel} -> {target}')
    # Check every current first-party document, not only the required entries.
    scanned = set(required)
    for p in sorted((root / 'docs').rglob('*.md')):
        rel = p.relative_to(root).as_posix()
        if rel in scanned:
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            errors.append(f'unsafe document: {rel}')
            continue
        prose = re.sub(r'```.*?```', '', p.read_text(encoding='utf-8'), flags=re.S)
        for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', prose):
            parsed = urlsplit(target.strip().split(' "', 1)[0].strip('<>'))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            dst = (p.parent / unquote(parsed.path)).resolve()
            if not dst.is_relative_to(root) or not dst.exists() or dst.is_symlink():
                errors.append(f'broken or external local link: {rel} -> {target}')
        checked += 1
    ignore = root / '.gitignore' 
    if ignore.is_file() and not ignore.is_symlink():
        lines = set(ignore.read_text(encoding='utf-8').splitlines())
        for rule in PRIVATE_RULES:
            if rule not in lines:
                errors.append(f'missing explicit local-only ignore rule: {rule}')
    return {
        'ok': not errors, 'kind': kind,
        'required_entry_files_checked': checked,
        'errors': errors,
        'scope': 'declared documentation entries, local links, and explicit ignore rules only',
        'not_verified': ['remote repository', 'secrets/history', 'application startup', 'model/device behavior', 'owner acceptance'],
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = inspect(args.root)
    except (OSError, UnicodeError, ValueError) as exc:
        result = {'ok': False, 'errors': [f'layout check failed: {type(exc).__name__}: {exc}']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1

if __name__ == '__main__':
    raise SystemExit(main())
