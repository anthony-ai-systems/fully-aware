#!/usr/bin/env python3
"""Macro-seat session-close spool hook (spec §2.2 stage 1). Budget ≤200ms.

Appends ONE NDJSON line — {session_id, transcript_path, project_dir, ts} — to
the macro-seat distill queue. No model call, no transcript read, no imprint
import. FAIL-OPEN: nothing here may ever block a session from stopping, so
every failure path is a silent exit 0. A single small O_APPEND write is atomic
on local filesystems, so no locking is needed against the worker.

Stop fires at the end of EVERY assistant turn, not once per session — the
worker's ledger + quiet-period gate collapse these per-turn breadcrumbs into
one distillation per session.
"""

import datetime
import json
import os
import sys


def main():
    try:
        event = json.load(sys.stdin)
        session_id = event.get("session_id")
        transcript = event.get("transcript_path", "")
        if not session_id:
            return 0
        cfg_path = (os.environ.get("IMPRINT_CONFIG")
                    or os.path.expanduser("~/.config/imprint/config.json"))
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        root = os.path.join(cfg["data_root"], cfg["operator_slug"], "macroseat")
        os.makedirs(root, mode=0o700, exist_ok=True)
        line = json.dumps({
            "session_id": session_id,
            "transcript_path": transcript,
            "project_dir": event.get("cwd", ""),
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, separators=(",", ":"))
        # 0600 create — this tree sits inside imprint's data root, whose
        # health scan degrades on any group/world-accessible path.
        fd = os.open(os.path.join(root, "distill-queue.ndjson"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — fail-open by contract
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
