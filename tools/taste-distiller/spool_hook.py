#!/usr/bin/env python3
"""Macro-seat distiller spool hook — Claude Code Stop event (spec §2.2 stage 1).

Appends ONE NDJSON line — {session_id, transcript_path, project_dir, ts} —
to <operator_root>/spool/distill-queue.ndjson and exits 0. That is the whole
job: no model call, no transcript read, no imprint import. Budget is <200ms
of the host's 10s hook window; a spool failure must never block a session
from stopping, so every failure path is silent exit 0 (fail-open).

Stop fires at the end of every assistant turn, not once per session — the
worker's ledger + quiet-period gate turn these per-turn breadcrumbs into
one distillation per session.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import spool_path

        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        session_id = event.get("session_id")
        transcript_path = event.get("transcript_path")
        if not session_id or not transcript_path:
            return 0
        line = json.dumps({
            "session_id": str(session_id),
            "transcript_path": str(transcript_path),
            "project_dir": str(event.get("cwd") or ""),
            "ts": time.time(),
        }, ensure_ascii=False, separators=(",", ":"))
        target = spool_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND single write: concurrent Stop hooks interleave whole lines.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
