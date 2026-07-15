"""Fail a deployment when a Git revision contains high-confidence secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import PurePosixPath


FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"(^|/).*\.db(?:-wal|-shm)?$"),
    re.compile(r"(^|/)backup/"),
    re.compile(r"(^|/)cutover-[^/]+/"),
    re.compile(r"openai_oauth_token\.json$"),
)

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenRouter key": re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    "Discord webhook": re.compile(
        r"https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9._-]+"
    ),
}


def _git(*args: str, text: bool = True):
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=text,
    ).stdout


def scan_revision(revision: str) -> list[tuple[str, str]]:
    # NUL output avoids core.quotePath's C-style quoting for Korean strategy
    # names and also handles whitespace/newlines in legal Git paths.
    raw_paths = _git(
        "ls-tree", "-r", "-z", "--name-only", revision, text=False
    )
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw_paths.split(b"\0")
        if item
    ]
    findings: list[tuple[str, str]] = []
    for path in paths:
        normalized = str(PurePosixPath(path))
        if any(pattern.search(normalized) for pattern in FORBIDDEN_PATH_PATTERNS):
            findings.append((normalized, "forbidden sensitive path"))
            continue
        blob = _git("show", f"{revision}:{path}", text=False)
        if b"\x00" in blob:
            continue
        content = blob.decode("utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append((normalized, label))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a Git revision for committed secret material."
    )
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    findings = scan_revision(args.revision)
    if findings:
        for path, label in findings:
            print(f"{path}: {label}")
        raise SystemExit("Refusing deployment: possible secret material is committed")
    print(f"Secret scan passed for {args.revision}")


if __name__ == "__main__":
    main()
