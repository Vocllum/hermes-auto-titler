#!/usr/bin/env python3
"""Fail if the live plugin-catalog entry lags behind the local plugin.yaml/tag.

Why: the plugin repo's plugin.yaml + tag are authoritative, the upstream catalog
is a hand-copied duplicate with no mechanical link. Three releases (0.2.1 ->
0.2.2 -> 0.2.3) went out unsynced, and nothing surfaced it because a missing
entry only means "absent from the directory" -- install never errors.

Usage:
    python3 scripts/check_catalog_sync.py            # compare local vs live
    python3 scripts/check_catalog_sync.py --json     # machine-readable

Exit 0 = in sync. Exit 1 = catalog lags (or entry missing/unparseable).
Exit 2 = could not reach the live catalog (do NOT treat as in-sync).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

LIVE_URL = "https://hermes-agent.nousresearch.com/docs/api/plugin-catalog.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
SELF_NAME = "hermes-auto-titler"


def local_version() -> str:
    with open("plugin.yaml", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^version:\s*(\S+)\s*$", line)
            if m:
                return m.group(1).strip("\"'")
    raise SystemExit("could not read version from plugin.yaml")


def local_commit_for(tag: str) -> str:
    # peel annotated tags: rev-parse of the tag object returns the tag sha
    return subprocess.run(
        ["git", "rev-parse", f"{tag}^{{}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def fetch_live() -> dict:
    req = urllib.request.Request(LIVE_URL, headers={
        "User-Agent": "hermes-auto-titler-catalog-sync-check",
        "Cache-Control": "no-cache",
    })
    # bypass any TTL/CDN reuse: the caller wants the published truth, not a cache
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live-url", default=LIVE_URL)
    args = ap.parse_args()

    reported: dict = {"entry": SELF_NAME}
    problems: list[str] = []
    local = local_version()
    reported["local_version"] = local

    try:
        want_sha = local_commit_for(f"v{local}")
        reported["expected_sha"] = want_sha
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: no local tag matching local version v{local} "
              f"(create+push it before bumping the catalog): {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ERROR: git not available; run inside the repo", file=sys.stderr)
        return 2

    url = args.live_url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "hermes-auto-titler-catalog-sync-check",
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"WARN: live catalog unreachable ({url}): {exc}", file=sys.stderr)
        print("WARN: NOT verifying catalog sync; treat as unknown, not as in-sync",
              file=sys.stderr)
        return 2

    entries = data.get("entries") or []
    reported["live_generated_at"] = data.get("generated_at")
    reported["live_entry_count"] = len(entries)

    ours = [e for e in entries if str(e.get("name")) == SELF_NAME]
    if len(ours) != 1:
        problems.append(f"catalog has {len(ours)} entries named {SELF_NAME} (want 1)")
        reported["problems"] = problems
        emit(reported, args.json)
        return 1

    e = ours[0]
    live_sha = str(e.get("sha") or "").lower()
    live_version = e.get("version")
    reported["live_sha"] = live_sha
    reported["live_version"] = live_version

    if not SHA_RE.match(live_sha):
        # mirror of entry_from_mapping(): a short/invalid sha silently drops the
        # entry in the client, and the client never reports it
        problems.append(f"live sha {live_sha!r} is not a 40-hex commit pin; "
                        "the entry will be silently dropped by the host")
    elif live_sha != want_sha:
        problems.append(f"catalog lags: live sha {live_sha[:12]} != "
                        f"plugin.yaml v{local} commit {want_sha[:12]}")

    live_version_value = "" if live_version is None else str(live_version)
    if live_version_value and local and live_version_value != local:
        problems.append(f"catalog version {live_version_value!r} != "
                        f"plugin.yaml {local!r}")
    if live_version_value and not VERSION_RE.match(live_version_value):
        problems.append(f"live version {live_version_value!r} fails the catalog "
                        "regex; it will be dropped to a cosmetic empty label")

    reported["problems"] = problems
    emit(reported, args.json)
    return 1 if problems else 0


def emit(reported: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(reported, indent=2))
        return
    print(f"entry           : {reported['entry']}")
    print(f"local version   : {reported.get('local_version')}")
    print(f"expected sha    : {reported.get('expected_sha')}")
    print(f"live generated  : {reported.get('live_generated_at')}")
    print(f"live entries    : {reported.get('live_entry_count')}")
    print(f"live sha        : {reported.get('live_sha')}")
    print(f"live version    : {reported.get('live_version')!r}")
    probs = reported.get("problems") or []
    print(f"problems        : {len(probs)}")
    for p in probs:
        print(f"  - {p}")


if __name__ == "__main__":
    raise SystemExit(main())
