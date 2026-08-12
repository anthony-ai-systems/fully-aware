#!/usr/bin/env python3
"""One-shot backfill seeder (spec §2.4). Seeds the distill queue with every
surviving transcript under ~/.claude/projects/ that the ledger hasn't seen.

Backfill is shallow by fact, not choice: the surviving window is ~3 weeks
(the pre-M0 30-day prune ate the rest), so this is a recency baseline, not
behavioral archaeology. Live capture at session close is the real asset.

This script only WRITES QUEUE LINES — no model calls, no ingest. The worker
drains the queue at its normal paced cadence (MACROSEAT_MAX_SESSIONS per
15-min tick, default 6). At default pacing 6K+ transcripts take weeks, so a
deliberate backfill run looks like:

    python3 backfill_seed.py --yes
    MACROSEAT_MAX_SESSIONS=200 python3 taste_distiller.py   # repeat / loop

Model-token spend happens in the worker, and most backfill transcripts are
skipped_trivial (no Haiku call) by the worker's floors. Requires --yes so
seeding thousands of sessions is never an accident.
"""

import argparse
import datetime
import glob
import json
import os
import sys

from taste_distiller import (load_imprint_config, load_ledger, ledger_path,
                             macroseat_root, queue_path, read_queue)

PROJECTS = os.path.expanduser("~/.claude/projects")


def decode_project_dir(dirname):
    """Best-effort decode of Claude Code's path-mangled project dir name
    (-Users-anthonyflores-code-foo -> /Users/anthonyflores/code/foo). Hyphens
    that were part of the original path are ambiguous; prefer a decode that
    exists on disk, else fall back to the raw mangled name."""
    if not dirname.startswith("-"):
        return dirname
    parts = dirname[1:].split("-")
    # Greedy: try joining with "/" then repairing non-existent tails with "-".
    candidate = "/" + "/".join(parts)
    if os.path.isdir(candidate):
        return candidate
    # Walk prefixes: keep "/" joins while the prefix exists, then "-" the rest.
    path = ""
    i = 0
    while i < len(parts):
        nxt = path + "/" + parts[i]
        if os.path.isdir(nxt) or i == 0:
            path = nxt
            i += 1
            continue
        # Try extending the last component with "-part" while that helps.
        merged = path + "-" + parts[i]
        if os.path.isdir(merged) or not os.path.isdir(path):
            path = merged
            i += 1
            continue
        path = nxt
        i += 1
    return path if os.path.isdir(path) else dirname


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true",
                    help="actually append to the queue (dry-run without)")
    ap.add_argument("--min-bytes", type=int, default=20000,
                    help="skip transcripts smaller than this (default 20000; "
                         "tiny transcripts carry no distillable taste)")
    args = ap.parse_args(argv)

    data_root, operator = load_imprint_config()
    root = macroseat_root(data_root, operator)
    os.makedirs(root, exist_ok=True)
    ledger = load_ledger(ledger_path(root))
    queued, _ = read_queue(queue_path(root))
    already = set(ledger) | {e["session_id"] for e in queued}

    transcripts = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    fresh, small = [], 0
    for path in transcripts:
        session_id = os.path.splitext(os.path.basename(path))[0]
        if session_id in already:
            continue
        try:
            if os.path.getsize(path) < args.min_bytes:
                small += 1
                continue
        except OSError:
            continue
        fresh.append((session_id, path,
                      decode_project_dir(os.path.basename(os.path.dirname(path)))))

    print("backfill: %d transcript(s) on disk, %d already queued/ledgered, "
          "%d under --min-bytes, %d fresh"
          % (len(transcripts), len(transcripts) - len(fresh) - small, small,
             len(fresh)))
    if not args.yes:
        print("dry-run — pass --yes to seed the queue")
        return 0

    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(queue_path(root), "a", encoding="utf-8") as fh:
        for session_id, path, project_dir in fresh:
            fh.write(json.dumps({
                "session_id": session_id,
                "transcript_path": path,
                "project_dir": project_dir,
                "ts": ts,
                "backfill": True,
            }, separators=(",", ":")) + "\n")
    print("seeded %d queue line(s); pace the worker with MACROSEAT_MAX_SESSIONS"
          % len(fresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
