#!/usr/bin/env python3
"""One-shot backfill seeder (spec §2.4): append surviving Claude Code
transcripts to the distill spool and let the armed worker drain them at its
normal 15-minute pace.

Writes SPOOL ENTRIES ONLY — no model calls, no ingest. The worker's ledger
makes overlap with live capture harmless. Backfill is shallow by fact
(~3-week transcript retention window at build time), a recency baseline,
not behavioral archaeology.

project_dir comes from the transcript's own "cwd" field (the escaped
directory name under ~/.claude/projects is ambiguous for paths containing
hyphens).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import spool_path


def transcript_cwd(path: Path, probe_lines: int = 20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in range(probe_lines):
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                    return obj["cwd"]
    except OSError:
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-dir", type=Path,
                        default=Path.home() / ".claude" / "projects")
    parser.add_argument("--min-bytes", type=int, default=20_000,
                        help="skip transcripts smaller than this (no taste in tiny sessions)")
    parser.add_argument("--since-days", type=float, default=None,
                        help="only transcripts modified in the last N days (default: all surviving)")
    parser.add_argument("--limit", type=int, default=None, help="cap seeded sessions")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = time.time()
    seeded, skipped = 0, 0
    entries = []
    for path in sorted(args.projects_dir.glob("*/*.jsonl")):
        stat = path.stat()
        if stat.st_size < args.min_bytes:
            skipped += 1
            continue
        if args.since_days is not None and now - stat.st_mtime > args.since_days * 86400:
            skipped += 1
            continue
        entries.append({
            "session_id": path.stem,
            "transcript_path": str(path),
            "project_dir": transcript_cwd(path),
            "ts": stat.st_mtime,
            "backfill": True,
        })
        seeded += 1
        if args.limit and seeded >= args.limit:
            break

    if args.dry_run:
        print(f"backfill (dry run): would seed {seeded}, skipped {skipped}")
        return 0
    target = spool_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"backfill: seeded {seeded} session(s) into {target} (skipped {skipped}); "
          "the worker drains them ledger-deduped at its normal cadence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
