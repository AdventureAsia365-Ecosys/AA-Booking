#!/usr/bin/env python3
"""Fail when a tracked file carries a literal AWS resource ID (AA-902). Stdlib only.

This repo is public: AWS account IDs, ARNs with an account, VPC/SG/subnet IDs, Cognito user-pool IDs and API
Gateway hostnames must not be committed (DM-6.10, D-25). Code reads them at run time (STS, env, Terraform
output, an Actions secret); docs name the resource or write a placeholder such as ``<acc2>`` or
``arn:aws:iam::<account>:role/<name>``.

Patterns follow AA-Ecosys ``tools/docs/check_docs_text.py`` (resource-ids), with two differences for a public
repo: every tracked text file is scanned, not only docs, and a 12-digit number after ``-`` counts too (a bucket
name such as ``<stem>-<account id>`` publishes the account ID; the last group of a UUID is not an account ID).

Allowed: the AWS documentation placeholder 123456789012, a 12-digit run of one repeated digit (test fakes such as
111111111111) and a user-pool ID containing TEST or EXAMPLE. Add ``docs-lint: ignore`` to a line to skip it (say
why next to it: a reviewer has to agree the value is public by design).

Usage (from the repo root):  python3 .github/scripts/check_resource_ids.py [--root DIR] [files...]
Exit 1 when anything is found; each finding is printed as a GitHub Actions ::error annotation.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

IGNORE_MARKER = "docs-lint: ignore"
ALLOWED_ACCOUNT_IDS = {"123456789012"}
_HEX_ID = r"[0-9a-f]{8}(?:[0-9a-f]{9})?"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ARN with account ID", re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):")),
    (
        "VPC resource ID",
        re.compile(rf"(?<![\w-])(?:sg|subnet|vpc|vpce|rtb|igw|nat|eni|acl|eipalloc|tgw)-{_HEX_ID}(?![\w-])"),
    ),
    ("Cognito user-pool ID", re.compile(r"(?<![\w-])[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{9}(?![\w-])")),
    ("API Gateway host", re.compile(r"(?<![\w-])[a-z0-9]{10}\.execute-api\.")),
    # Not inside a longer word or number, and not the last group of a UUID (…-0000-000000000001); '-', '/', ':',
    # '.', '_' and quotes around it still match, so a bucket name ending in an account ID is caught.
    ("12-digit account ID", re.compile(r"(?<![0-9A-Za-z])(?<![0-9A-Fa-f]{4}-)(\d{12})(?![0-9A-Za-z])")),
]


def allowed(label: str, match: re.Match[str]) -> bool:
    """Obvious fakes only: 123456789012, one repeated digit, a user-pool ID that says TEST or EXAMPLE."""
    if label == "Cognito user-pool ID":
        return any(word in match.group(0).upper() for word in ("TEST", "EXAMPLE"))
    if match.lastindex is None:  # pattern without an account group
        return False
    account = match.group(1)
    return account in ALLOWED_ACCOUNT_IDS or len(set(account)) == 1


def scan_text(path: str, text: str) -> list[tuple[str, int, str, str]]:
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARKER in line:
            continue
        for label, pattern in PATTERNS:
            hit = next((m for m in pattern.finditer(line) if not allowed(label, m)), None)
            if hit:
                findings.append((path, n, label, hit.group(0)))
                break
    return findings


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    return [f for f in out.decode("utf-8").split("\0") if f]


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):  # deleted in the work tree, or a submodule
        return None
    if b"\0" in data[:8192]:  # binary
        return None
    return data.decode("utf-8", errors="replace")


def mask(value: str) -> str:
    """Never echo a full ID back into a public CI log."""
    return re.sub(r"\d{12}", lambda m: m.group(0)[:2] + "*" * 8 + m.group(0)[-2:], value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="repo-relative files (default: every tracked file)")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = args.files or tracked_files(root)

    findings = []
    for rel in files:
        text = read_text(root / rel)
        if text is not None:
            findings.extend(scan_text(rel, text))

    for path, line, label, value in findings:
        print(
            f"::error file={path},line={line},title=Literal AWS resource ID::{label} '{mask(value)}' in a public "
            "repo; read it at run time (STS, env, secret) or write a placeholder such as <acc2>."
        )
    print(f"resource-ids: {len(files)} files checked, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
