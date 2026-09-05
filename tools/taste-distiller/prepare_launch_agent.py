#!/usr/bin/env python3
"""Render a reviewable LaunchAgent without installing it or starting a worker."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import sys


def executable(value, name):
    found = value or shutil.which(name)
    if not found or not os.path.isabs(found) or not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise ValueError("%s must resolve to an executable absolute file" % name)
    return os.path.abspath(found)


def render(claude_bin=None, python_bin=None, worker=None, logs=None):
    here = Path(__file__).resolve().parent
    claude = executable(claude_bin or os.environ.get("MACROSEAT_CLAUDE_BIN"), "claude")
    python = executable(python_bin or sys.executable, "python3")
    worker = str(Path(worker or here / "taste_distiller.py").resolve(strict=True))
    path_parts = [os.path.dirname(claude), os.path.dirname(python)]
    node = shutil.which("node")
    if node:
        path_parts.append(os.path.dirname(node))
    path_parts += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    with (here / "com.macroseat.taste-distiller.plist").open("rb") as fh:
        value = plistlib.load(fh)
    value["ProgramArguments"] = [python, worker]
    value["EnvironmentVariables"] = {"MACROSEAT_CLAUDE_BIN": claude,
                                     "PATH": ":".join(dict.fromkeys(path_parts))}
    log_root = Path(logs or Path.home() / ".optimus/logs")
    value["StandardOutPath"] = str(log_root / "taste-distiller.log")
    value["StandardErrorPath"] = str(log_root / "taste-distiller.err.log")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin")
    parser.add_argument("--python-bin")
    parser.add_argument("--worker")
    parser.add_argument("--logs")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    value = render(args.claude_bin, args.python_bin, args.worker, args.logs)
    with open(args.out, "wb") as fh:
        plistlib.dump(value, fh)


if __name__ == "__main__":
    main()
