"""Audit source inventory and optional private deny terms without printing content."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

IGNORED = {'.git', 'node_modules', '__pycache__', '.venv', '.mypy_cache', '.ruff_cache', '.pytest_cache'}
FORBIDDEN_SUFFIXES = {'.sock', '.sqlite3', '.log', '.pem', '.key', '.pyc'}


def audit(root: Path, terms: list[str]) -> tuple[int, list[str]]:
    failures = []
    count = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            failures.append(f'{relative}: symlink outside export inventory')
            continue
        if not path.is_file():
            continue
        count += 1
        if path.suffix in FORBIDDEN_SUFFIXES or path.name in {'.env', 'auth.json', 'identity.json'}:
            failures.append(f'{relative}: runtime or credential filename')
        try:
            text = path.read_text()
        except UnicodeError:
            failures.append(f'{relative}: unexpected binary file')
            continue
        combined = str(relative) + '\n' + text
        if any(term.casefold() in combined.casefold() for term in terms):
            failures.append(f'{relative}: private deny term matched')
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{40}', text):
            failures.append(f'{relative}: private key data')
        if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})\b', text):
            failures.append(f'{relative}: possible live credential')
    return count, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--deny-file', type=Path, help='private newline-separated terms; never copied into export')
    args = parser.parse_args()
    terms = [line.strip() for line in args.deny_file.read_text().splitlines() if line.strip()] if args.deny_file else []
    count, failures = audit(args.root, terms)
    for failure in failures:
        print(failure)
    print(f'Audited {count} source files; {len(failures)} findings')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
