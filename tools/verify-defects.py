#!/usr/bin/env python3
"""verify-defects.py -- run every defect's verify command, record what is still broken.

``registers/defects.json`` (schema ``defect-register/v1``) is the single list of
what is broken across the estate. It carries no status: every item carries a
shell command that exits 0 when the defect is gone, and THIS tool is the morning
loop that runs them all and computes status into two gitignored outputs:

  * ``state/defects-status.json`` (schema ``defect-status/v1``) -- the machine
    record the boot-pack assembler folds in as section 0.
  * ``state/DEFECTS.md`` -- the plain-English list, grouped by who can act.

Contract for one item (register ``rules.verify``): ``bash -c <verify>`` from
``$HOME``, 60 s timeout, PATH prefixed with the Homebrew / local bins so a
launchd run sees the same tools as a shell. Exit 0 -> ``fixed``; any other exit
-> ``open``; timeout -> ``error``. A ``provisional`` item is never run (its
verify is a known placeholder); an item whose ``not_before`` is still in the
future is ``deferred`` and never run; an item carrying an ``accepted`` reason is
a standing decision not to fix it, is never run, is never open, and never reaches
the defect gate -- it keeps its place in the register with the reason and the
ruling date beside it.

Dates are LOCAL dates, not UTC ones. ``today`` is the day on Anthony's own wall
clock, so an evening run cannot age every open defect by an extra day, and the
defect gate's own fallback (``tools/hooks/defect_gate.py``, which reads the local
date) computes the same number this file records.

History carry-forward (why ``days_open`` can be trusted): the previous status
file is read before the run. An item that was open and is open again keeps its
original ``open_since``; an item that was fixed and is open again starts its
clock today; an item never seen before inherits the register's ``since`` date. A
newly fixed item gets today's ``fixed_at``; an already-fixed item keeps the one
it had.

"Fixed since yesterday" counts only what THIS run learned: an item whose
``fixed_at`` falls after the day the previous status file was written. With no
previous status file -- a first run, or one after the file was lost -- the answer
is 0, because "every check that happened to pass the first time it ran" is not a
list of fixes. Two runs on the same local day therefore report the second one's
new fixes only, never the first run's again.

Class D30 (read-only over everything except its own two ``state/`` outputs,
stateless, non-enforcing, report-only). It never edits the register, never
commits, never pushes. The verify commands themselves are the register's
responsibility: they are written to observe, not to change anything.

Exit status: 0 whenever the loop ran (a failing verify is DATA, not a failure);
2 on a hard failure: the register is missing or unparseable, or the outputs
could not be written (a non-gitignored output path, or a filesystem error).

Anything that would name a client -- a repository, a fork, a URL, a list of
pull requests -- must never sit in the register (P25: client names never land
in a networked repo). Those values live in ONE local file, KEY=VALUE per line,
and reach every verify as environment variables:

    ~/.config/fully-aware/defects.env   (override: $DEFECTS_PRIVATE_ENV)

Only ``DEFECT_``-prefixed names are read from that file. The rest are ignored,
so a ``PATH=`` or ``HOME=`` line there cannot redirect what a verify runs.

A verify that needs one of them must fail (stay open) when it is empty.

Stdlib only, Python 3.9+. ``--now`` injects the clock so tests are deterministic.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

REGISTER_SCHEMA = "defect-register/v1"
STATUS_SCHEMA = "defect-status/v1"

# Register rules.verify, verbatim: 60 s, from $HOME, Homebrew-first PATH.
VERIFY_TIMEOUT = 60
HOME = os.path.expanduser("~")
PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:%s/.local/bin:" % HOME

# The private values file (see the module docstring). Missing file: no
# variables, and every verify that depends on one fails -- never passes.
PRIVATE_ENV_PATH = os.environ.get("DEFECTS_PRIVATE_ENV") or os.path.join(
    HOME, ".config", "fully-aware", "defects.env")
# Only ``DEFECT_``-prefixed names are read (docs/DEFECTS.md). The values are
# applied over the environment AFTER the Homebrew-first PATH is set, so a
# stray ``PATH=`` or ``HOME=`` line must not be able to redirect a verify.
_ENV_KEY_RE = re.compile(r"^DEFECT_[A-Z0-9_]+$")

# Output of a verify is never rendered into the boot pack; only this much of its
# tail rides into the status file, for the "Check errored" group.
STDERR_TAIL_CHARS = 300

SEVERITIES = ("P0", "P1", "P2")
_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}

# Owner -> the plain-English heading its open items are grouped under.
OWNER_GROUPS = (
    ("anthony", "Only you can do these"),
    ("session", "A supervised session on this Mac"),
    ("codex", "The nightly lane can take these"),
)
OTHER_OWNERS_HEADING = "Waiting on someone else"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import notify  # noqa: E402  (the push edge; opt-in via FULLY_AWARE_PUSH=1)


class RegisterError(Exception):
    """A hard failure: the register itself is missing or unusable (exit 2)."""


class WriteRefused(Exception):
    """An output path that is not gitignored (state/ discipline)."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def oneline(value):
    """Collapse a value to a single whitespace-normalized line ("" for None)."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def parse_date(value):
    """Parse a leading ISO date (YYYY-MM-DD) to a date, or None. Never raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()[:10]
    try:
        return datetime.date(*(int(p) for p in token.split("-")))
    except (TypeError, ValueError):
        return None


def local_date(now):
    """The LOCAL calendar date of an instant, as an ISO string.

    Every date this tool records -- ``open_since``, ``fixed_at``,
    ``last_verified``, and the ``today`` every age is measured against -- is the
    day on the wall clock Anthony reads. A UTC date rolls at 17:00 local and
    would age every open defect a day early; the defect gate's own fallback uses
    the local date, so the file and the hook would then disagree.
    """
    return now.astimezone().date().isoformat()


def days_between(start, today):
    """Whole days from an ISO start date to an ISO today (never negative)."""
    a = parse_date(start)
    b = parse_date(today)
    if a is None or b is None:
        return 0
    return max(0, (b - a).days)


def is_accepted(item):
    """True when the item carries an ``accepted`` reason (a standing decision).

    An accepted item is a known problem nobody is going to fix, by ruling. Its
    check is never run, it is never open, it never reaches the defect gate, and
    it is never anyone's work for today -- but it stays in the register, in its
    own group, with the reason and the ruling date written next to it. Deleting
    it would be the lie; counting it as open would be the nag.
    """
    value = (item or {}).get("accepted")
    return isinstance(value, str) and bool(value.strip())


def tail(text, limit=STDERR_TAIL_CHARS):
    """The last ``limit`` characters of a command's output, whitespace-collapsed."""
    line = oneline(text)
    if len(line) <= limit:
        return line
    return line[-limit:]


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_register(path):
    """Load registers/defects.json, or raise RegisterError (the only exit-2 path)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RegisterError("cannot load register %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise RegisterError("register %s is not a JSON object" % path)
    if data.get("schema") != REGISTER_SCHEMA:
        raise RegisterError("register %s has unexpected schema %r (want %r)"
                            % (path, data.get("schema"), REGISTER_SCHEMA))
    if not isinstance(data.get("items"), list):
        raise RegisterError("register %s missing items[]" % path)
    return data


def load_previous_doc(path):
    """The previous status document -- ``{}`` on any problem (history is a bonus)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def previous_records(doc):
    """``{id: record}`` from a previous status document."""
    out = {}
    for rec in (doc or {}).get("items") or []:
        if isinstance(rec, dict) and rec.get("id"):
            out[rec["id"]] = rec
    return out


def previous_run_date(doc):
    """The LOCAL date the previous status file was generated, or ``None``.

    This is what "fixed since yesterday" is measured against. ``None`` -- no
    previous file, or one with no usable stamp -- means there is nothing to
    compare with, and the honest count of new fixes is zero.
    """
    stamp = (doc or {}).get("generated_at")
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return parse_date(stamp).isoformat() if parse_date(stamp) else None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return local_date(parsed)


def load_previous(path):
    """Previous status records by id -- ``{}`` on any problem (history is a bonus)."""
    return previous_records(load_previous_doc(path))


# --------------------------------------------------------------------------- #
# running one verify
# --------------------------------------------------------------------------- #
def load_private_env(path=None):
    """Read ``KEY=VALUE`` lines from the private values file. No shell, no
    expansion: surrounding quotes are stripped, everything else is literal.
    Blank lines and ``#`` comments are skipped; a missing file is empty."""
    path = path or PRIVATE_ENV_PATH
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not _ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _shell_runner(command):
    """Run one verify under bash from $HOME.

    Return ``(exit_code, stdout, stderr)``.  Both streams are captured so a
    noisy successful check cannot leak into the morning output, while the
    status record can retain the specifically requested stderr tail.
    """
    env = dict(os.environ)
    env["PATH"] = PATH_PREFIX + env.get("PATH", "")
    env.update(load_private_env())
    proc = subprocess.run(["bash", "-c", command], cwd=HOME, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=VERIFY_TIMEOUT)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def run_verify(command, runner=None, clock=None):
    """Run a verify command. Returns ``(status, exit_code, output, duration_ms)``.

    ``runner`` is injectable; it takes the command and returns
    ``(exit_code, stdout, stderr)`` or raises ``subprocess.TimeoutExpired``.
    A timeout is ``error`` (the check could not decide), never ``open``.
    """
    runner = runner or _shell_runner
    clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    started = clock()

    def elapsed():
        return int((clock() - started).total_seconds() * 1000)

    if not command:
        return "error", None, "no verify command in the register", elapsed()
    try:
        result = runner(command)
        if len(result) == 2:
            # Backward-compatible test seam for the first untested draft.
            code, stderr = result
        else:
            code, _stdout, stderr = result
    except subprocess.TimeoutExpired:
        return "error", None, "verify timed out after %ds" % VERIFY_TIMEOUT, elapsed()
    except OSError as exc:
        return "error", None, "verify could not run: %s" % exc, elapsed()
    status = "fixed" if code == 0 else "open"
    return status, code, stderr, elapsed()


# --------------------------------------------------------------------------- #
# per-item evaluation + history carry-forward
# --------------------------------------------------------------------------- #
def plan_item(item, today):
    """What the loop will do with one item: ``('run', None)`` or a skip status."""
    if is_accepted(item):
        return "skip", "accepted"
    if item.get("provisional") is True:
        return "skip", "provisional"
    nb = parse_date(item.get("not_before"))
    if nb is not None and parse_date(today) is not None and nb > parse_date(today):
        return "skip", "deferred"
    return "run", None


def carry_history(item, previous, status, today):
    """Return ``(open_since, days_open, fixed_at)`` for one evaluated item."""
    since = item.get("since") or today
    prev = previous or {}
    prev_status = prev.get("status")
    prev_open_since = prev.get("open_since")

    if status in ("open", "error"):
        if prev_status in ("open", "error"):
            open_since = prev_open_since or since
        elif prev_status == "fixed":
            open_since = today          # it regressed: the clock restarts today
        else:
            open_since = since          # never seen: the register's own date
        return open_since, days_between(open_since, today), None

    if status == "fixed":
        fixed_at = prev.get("fixed_at") if prev_status == "fixed" else None
        return (prev_open_since or since), None, (fixed_at or today)

    # provisional / deferred: not run, so nothing is learned and nothing moves.
    return (prev_open_since or since), None, prev.get("fixed_at")


def evaluate_item(item, previous, today, runner=None, clock=None):
    """Evaluate one register item into a status record (runs the verify if due)."""
    action, skip_status = plan_item(item, today)
    if action == "skip":
        status, code, output, ms = skip_status, None, "", 0
    else:
        status, code, output, ms = run_verify(item.get("verify", ""),
                                              runner=runner, clock=clock)
    open_since, days_open, fixed_at = carry_history(item, previous, status, today)
    return {
        "id": item.get("id", ""),
        "severity": item.get("severity", ""),
        "owner": item.get("owner", ""),
        "fix_scope": item.get("fix_scope", ""),
        "size": item.get("size", ""),
        "system": oneline(item.get("system")),
        "symptom": oneline(item.get("symptom")),
        "fix_hint": oneline(item.get("fix_hint")),
        "accepted": oneline(item.get("accepted")) or None,
        "status": status,
        "exit": code,
        "last_verified": today if action == "run" else None,
        "open_since": open_since,
        "days_open": days_open,
        "fixed_at": fixed_at,
        "duration_ms": ms,
        "stderr_tail": tail(output) if status in ("open", "error") else "",
    }


# --------------------------------------------------------------------------- #
# counts, ordering, the one summary line
# --------------------------------------------------------------------------- #
def _sort_key(rec):
    """P0 first, then the item open longest, then the id (deterministic)."""
    return (_SEVERITY_RANK.get(rec.get("severity"), 9),
            -(rec.get("days_open") or 0), rec.get("id", ""))


def _yours_sort_key(rec):
    """Anthony's P0s first; after that, age outranks lower severities."""
    return (0 if rec.get("severity") == "P0" else 1,
            -(rec.get("days_open") or 0), rec.get("id", ""))


def new_p0_ids(records, previous):
    """Items that are open P0 now and were not open P0 in the previous status.

    The comparison is against the previous FILE, not against time: the morning
    job writes once a day, so a defect that appears (or escalates to P0) pushes
    once and then lives in the digest. No previous file means a first run or a
    rebuilt one -- everything would look new, and a burst of pushes on a
    rebuild helps nobody, so nothing is reported.
    """
    if not previous:
        return []
    fresh = []
    for rec in records:
        if rec.get("severity") != "P0" or rec.get("status") != "open":
            continue
        prev = previous.get(rec.get("id")) or {}
        if prev.get("severity") == "P0" and prev.get("status") == "open":
            continue
        fresh.append(rec.get("id"))
    return fresh


def is_new_fix(rec, previous_run):
    """True when THIS run is the first to learn the item is fixed.

    ``previous_run`` is the local date the previous status file was written. An
    item counts only when its ``fixed_at`` falls AFTER that day: an item already
    reported fixed is not news, and with no previous file (``previous_run`` is
    None) nothing can honestly be called a new fix.
    """
    if (rec or {}).get("status") != "fixed":
        return False
    fixed_at = parse_date(rec.get("fixed_at"))
    prev = parse_date(previous_run)
    if fixed_at is None or prev is None:
        return False
    return fixed_at > prev


def build_counts(records, today, previous_run=None):
    """The counts block: per-severity open + oldest, plus the standing tallies."""
    counts = {}
    for sev in SEVERITIES:
        opens = [r for r in records
                 if r["status"] == "open" and r["severity"] == sev]
        counts[sev] = {
            "open": len(opens),
            "oldest_days": max([r.get("days_open") or 0 for r in opens] or [0]),
        }
    counts["fixed_since_last"] = len(
        [r for r in records if is_new_fix(r, previous_run)])
    counts["accepted"] = len([r for r in records if r["status"] == "accepted"])
    counts["provisional"] = len([r for r in records if r["status"] == "provisional"])
    counts["deferred"] = len([r for r in records if r["status"] == "deferred"])
    counts["error"] = len([r for r in records if r["status"] == "error"])
    by_owner = {}
    for r in records:
        if r["status"] == "open":
            owner = r.get("owner") or "?"
            by_owner[owner] = by_owner.get(owner, 0) + 1
    counts["open_by_owner"] = dict(sorted(by_owner.items()))
    return counts


def summary_line(counts):
    """The ONE line every session sees first. Built from counts alone.

    Kept identical (by construction, not by import) in assemble-boot-pack.py and
    boot-digest.py: those tools read the status file's counts block, never this
    module -- coupling by data contract only.
    """
    counts = counts or {}
    p0 = counts.get("P0") or {}
    p1 = counts.get("P1") or {}
    p2 = counts.get("P2") or {}
    p0_open = int(p0.get("open") or 0)
    oldest = " (oldest %dd)" % int(p0.get("oldest_days") or 0) if p0_open else ""
    owners = counts.get("open_by_owner") or {}
    return ("DEFECTS -- P0: %d%s · P1: %d · P2: %d · "
            "fixed since yesterday: %d · yours today: %d · "
            "no real check yet: %d · accepted: %d"
            % (p0_open, oldest, int(p1.get("open") or 0), int(p2.get("open") or 0),
               int(counts.get("fixed_since_last") or 0),
               int(owners.get("anthony") or 0),
               int(counts.get("provisional") or 0),
               int(counts.get("accepted") or 0)))


def build_status(register, now, previous=None, runner=None, clock=None,
                 only=None, previous_run=None):
    """Run the loop over the register and return the defect-status/v1 document.

    ``previous_run`` is the local date of the PREVIOUS status file (see
    ``previous_run_date``). It is what the "fixed since yesterday" count is
    measured against; ``None`` means there is no previous run and the count is 0.
    """
    previous = previous or {}
    today = local_date(now)
    records = []
    for item in register.get("items", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if only and item.get("id") != only:
            continue
        records.append(evaluate_item(item, previous.get(item.get("id")), today,
                                     runner=runner, clock=clock))

    counts = build_counts(records, today, previous_run=previous_run)
    yours = [r["id"] for r in sorted(
        [r for r in records if r["status"] == "open" and r["owner"] == "anthony"],
        key=_yours_sort_key)]
    nightly = [r["id"] for r in sorted(
        [r for r in records
         if r["status"] == "open" and r["owner"] == "codex"
         and r["fix_scope"] == "repo-pr"], key=_sort_key)]
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        # The local day this run belongs to, and the local day of the run it is
        # compared with. Both are recorded so a reader never has to re-derive a
        # date from a timestamp in another zone.
        "today": today,
        "previous_run": previous_run,
        "register_updated": register.get("updated", ""),
        "counts": counts,
        "yours_today": yours,
        "nightly_eligible": nightly,
        "items": records,
    }


# --------------------------------------------------------------------------- #
# the plain-English render
# --------------------------------------------------------------------------- #
MD_TITLE = "# FULLY AWARE -- DEFECTS (what is broken, and who can fix it)"


def _bullet(rec, today):
    """One plain-English line for one item. ``days_open`` is only carried for
    open/error items, so the other groups measure from ``open_since`` here (a
    fixed item measures to the day it closed, everything else to today)."""
    days = rec.get("days_open")
    if days is None:
        days = days_between(rec.get("open_since"), rec.get("fixed_at") or today)
    line = "- %s — %dd open — %s" % (rec.get("id", "?"), days,
                                     rec.get("symptom") or "(no symptom)")
    if rec.get("fix_hint"):
        line += " — fix: %s" % rec["fix_hint"]
    if rec.get("status") == "error" and rec.get("stderr_tail"):
        line += " — the check errored: %s" % rec["stderr_tail"]
    if rec.get("status") == "accepted" and rec.get("accepted"):
        line += " — accepted: %s" % rec["accepted"]
    return line


def _groups(status):
    """(heading, [records]) in render order -- who can act, then the exceptions."""
    records = status.get("items") or []
    previous_run = status.get("previous_run")
    out = []
    known = set()
    for owner, heading in OWNER_GROUPS:
        known.add(owner)
        out.append((heading, [r for r in records
                              if r["status"] == "open" and r["owner"] == owner]))
    out.append((OTHER_OWNERS_HEADING,
                [r for r in records
                 if r["status"] == "open" and r["owner"] not in known]))
    out.append(("Accepted, not being fixed",
                [r for r in records if r["status"] == "accepted"]))
    out.append(("No real check yet",
                [r for r in records if r["status"] == "provisional"]))
    out.append(("Deferred", [r for r in records if r["status"] == "deferred"]))
    out.append(("Fixed since yesterday",
                [r for r in records if is_new_fix(r, previous_run)]))
    out.append(("Check errored", [r for r in records if r["status"] == "error"]))
    return [(heading, sorted(recs, key=_sort_key)) for heading, recs in out]


def render_md(status):
    """The plain-English defect list. No commands, no jargon -- fix hints only."""
    lines = [MD_TITLE, "", summary_line(status.get("counts")), ""]
    today = status.get("today") or (status.get("generated_at") or "")[:10]
    for heading, recs in _groups(status):
        lines.append("## %s (%d)" % (heading, len(recs)))
        for rec in recs:
            lines.append(_bullet(rec, today))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_status(status):
    return json.dumps(status, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# gitignore guard (state/ outputs must be gitignored) -- assembler pattern
# --------------------------------------------------------------------------- #
def _inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


def assert_gitignored(out_path):
    """Refuse an in-repo output path unless git ignores it (D30 state discipline)."""
    if not _inside(out_path, _REPO_ROOT):
        return
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        ["git", "-C", _REPO_ROOT, "check-ignore", "--quiet", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise WriteRefused(
            "refusing to write %s: defect-status outputs must be gitignored "
            "(state/ is local-only). Add it to .gitignore or pass an output "
            "path outside the repo." % out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default(*parts):
    return os.path.join(_REPO_ROOT, *parts)


def resolve_repo_path(path):
    """Resolve every relative CLI path from the script's repository root."""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(_REPO_ROOT, path))


def _dry_run_report(register, today, out, only=None):
    """List what the loop WOULD do. Runs nothing, writes nothing."""
    for item in register.get("items", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if only and item.get("id") != only:
            continue
        action, skip = plan_item(item, today)
        if action == "run":
            out.write("would run   %-10s %s / %s\n"
                      % (item["id"], item.get("severity", "?"),
                         item.get("owner", "?")))
        elif skip == "accepted":
            out.write("would skip  %-10s accepted (not being fixed)\n"
                      % item["id"])
        elif skip == "deferred":
            out.write("would skip  %-10s deferred until %s\n"
                      % (item["id"], item.get("not_before")))
        else:
            out.write("would skip  %-10s no real check yet (provisional)\n"
                      % item["id"])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run every defect verify in the register and record status.")
    ap.add_argument("--register", default=_default("registers", "defects.json"),
                    help="the single defect register (defect-register/v1)")
    ap.add_argument("--status-out",
                    default=_default("state", "defects-status.json"),
                    help="machine status the boot pack reads (defect-status/v1)")
    ap.add_argument("--md-out", default=_default("state", "DEFECTS.md"),
                    help="the plain-English defect list")
    ap.add_argument("--only", default=None, metavar="ID",
                    help="run ONE item's verify and print the result; writes "
                         "nothing (a single-item run would otherwise overwrite "
                         "every other item's history)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would run; run nothing, write nothing")
    ap.add_argument("--now", default=None, metavar="ISO",
                    help="injectable clock (ISO timestamp) for deterministic runs")
    args = ap.parse_args(argv)

    args.register = resolve_repo_path(args.register)
    args.status_out = resolve_repo_path(args.status_out)
    args.md_out = resolve_repo_path(args.md_out)

    try:
        register = load_register(args.register)
    except RegisterError as exc:
        sys.stderr.write("verify-defects: REGISTER FAILURE: %s\n" % exc)
        return 2

    if args.now:
        now = datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
    today = local_date(now)
    previous_doc = load_previous_doc(args.status_out)

    if args.dry_run:
        _dry_run_report(register, today, sys.stdout, only=args.only)
        return 0

    if args.only:
        ids = [i.get("id") for i in register.get("items", []) if isinstance(i, dict)]
        if args.only not in ids:
            sys.stderr.write("verify-defects: no item %r in the register\n"
                             % args.only)
            return 0
        status = build_status(register, now,
                              previous=previous_records(previous_doc),
                              previous_run=previous_run_date(previous_doc),
                              only=args.only)
        rec = status["items"][0]
        sys.stdout.write("%s: %s (exit %s, %dms)\n"
                         % (rec["id"], rec["status"], rec["exit"],
                            rec["duration_ms"]))
        if rec["stderr_tail"]:
            sys.stdout.write("  output tail: %s\n" % rec["stderr_tail"])
        sys.stdout.write("  nothing written (single-item run)\n")
        return 0

    status = build_status(register, now,
                          previous=previous_records(previous_doc),
                          previous_run=previous_run_date(previous_doc))

    status_out = os.path.abspath(args.status_out)
    md_out = os.path.abspath(args.md_out)
    try:
        assert_gitignored(status_out)
        assert_gitignored(md_out)
    except WriteRefused as exc:
        sys.stderr.write("verify-defects: WRITE REFUSED: %s\n" % exc)
        return 2
    try:
        for path in (status_out, md_out):
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        with open(status_out, "w", encoding="utf-8") as fh:
            fh.write(render_status(status))
        with open(md_out, "w", encoding="utf-8") as fh:
            fh.write(render_md(status))
    except OSError as exc:
        sys.stderr.write("verify-defects: OUTPUT FAILURE: %s\n" % exc)
        return 2

    sys.stderr.write("verify-defects: %s\n" % summary_line(status["counts"]))
    sys.stderr.write("  status: %s\n  list:   %s\n" % (status_out, md_out))

    # The push edge: a defect newly at open-P0 reaches the phone once, here,
    # after the files are safely written. Opt-in (FULLY_AWARE_PUSH=1, set by
    # morning-pack.sh); a failed push costs nothing but the push.
    fresh = new_p0_ids(status["items"], previous_records(previous_doc))
    if fresh:
        first = next(r for r in status["items"] if r.get("id") == fresh[0])
        notify.push(
            "Defect register: %d new P0%s" % (len(fresh),
                                              "" if len(fresh) == 1 else "s"),
            "%s. First symptom: %s"
            % (", ".join(str(i) for i in fresh),
               oneline(first.get("symptom") or "")[:200]),
            priority="high")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
