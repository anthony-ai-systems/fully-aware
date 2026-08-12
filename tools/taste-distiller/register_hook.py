#!/usr/bin/env python3
"""Register/unregister the macro-seat distill spool hook in Claude Code
settings — same idempotent embedded-marker mechanism as imprint's
tools/install/manage_hooks.py, separate marker so the two owners never
touch each other's entries.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

MARKER = "macroseat-taste-distiller-hook"
EVENT = "Stop"
HOOK_SCRIPT = Path(__file__).resolve().parent / "spool_hook.py"


def _owned(entry) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(hook, dict) and MARKER in str(hook.get("command", ""))
        for hook in entry.get("hooks", [])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("register", "unregister", "status"))
    parser.add_argument("--settings", type=Path,
                        default=Path.home() / ".claude" / "settings.json")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()

    settings = {}
    if args.settings.exists():
        settings = json.loads(args.settings.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            print(json.dumps({"status": "error", "error": "settings must be a JSON object"}))
            return 2
    hooks = settings.setdefault("hooks", {})
    existing = hooks.get(EVENT, [])
    if not isinstance(existing, list):
        print(json.dumps({"status": "error", "error": f"hooks.{EVENT} must be a list"}))
        return 2

    if args.action != "status":
        kept = [item for item in existing if not _owned(item)]
        if args.action == "register":
            command = (f"{shlex.quote(str(args.python))} "
                       f"{shlex.quote(str(HOOK_SCRIPT))} # {MARKER}")
            kept.append({"matcher": "",
                         "hooks": [{"type": "command", "command": command}]})
        if kept:
            hooks[EVENT] = kept
        else:
            hooks.pop(EVENT, None)
        if not hooks:
            settings.pop("hooks", None)
        if args.settings.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(args.settings,
                         args.settings.with_name(f"{args.settings.name}.macroseat-backup-{stamp}"))
        temp = args.settings.with_suffix(".macroseat-tmp")
        temp.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, args.settings)

    count = sum(1 for item in settings.get("hooks", {}).get(EVENT, []) if _owned(item))
    expected = 1 if args.action == "register" else 0 if args.action == "unregister" else count
    okay = count == expected
    print(json.dumps({"status": "ok" if okay else "error", "registered": count}, sort_keys=True))
    return 0 if okay else 2


if __name__ == "__main__":
    raise SystemExit(main())
