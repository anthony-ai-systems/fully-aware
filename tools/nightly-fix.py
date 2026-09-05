#!/usr/bin/env python3
"""nightly-fix.py -- the nightly executor lane of the defect register.

Every night this takes the oldest small defect the register says the nightly
lane is allowed to fix, fixes it in a FRESH clone with the Codex CLI, proves the
fix with the item's pr_check, and opens a pull request. Merge is always
Anthony's -- this script never merges, never force-pushes, and never touches an
existing checkout.

What is actually isolated, stated exactly (a 2026-08-28 review found the older
"the sandbox is absolute" claim was true of this script's own commands and
false of the Codex process it launches):

  * a fresh clone under /Users/anthonyflores/code/.nightly-fix/, never a repo
    anyone works in;
  * one branch, named fix/<id>-<date>, never a default branch;
  * one pull request, opened and left for Anthony;
  * Codex's SHELL sandbox is workspace-write. That is the only thing the -s
    flag covers: it constrains model-generated shell commands, nothing else;
  * every MCP server named in ~/.codex/config.toml is switched off by name on
    the command line, the whole plugin system is switched off, and the app,
    browser, computer-use, code-mode, memory and chronicle features are
    switched off, for this run only. The global config file is never edited;
  * that is VERIFIED before the model is launched: the lane runs "codex ...
    mcp list" with the same overrides and refuses to start unless every row
    reads "disabled" (outcome codex-isolation-failed);
  * the item's pr_check runs under "codex sandbox", whose seatbelt denies the
    network and denies writes outside the clone.

What is NOT isolated, and should not be claimed to be:

  * READS. A workspace-write sandbox restricts writes, not reads, so Codex can
    still read files elsewhere on this Mac. The staging rail below is the
    answer to that: nothing untracked that looks like a credential, nothing
    over 1 MB and nothing outside the clone is ever committed;
  * the Codex login itself, which stays the machine keychain login. An isolated
    CODEX_HOME was tried and rejected: the keychain credential is bound to the
    real home, so an isolated home breaks authentication.

Modes:

  --dry-run   (default) pick the item, write the prompt that WOULD be sent,
              print the selection, touch nothing else.
  --trial     clone, run Codex, run the pr_check, commit locally. No push, no
              PR. The clone is kept for inspection.
  --live      everything, ending in a pull request, then the clone is deleted.

Inputs:

  state/defects-status.json   written each morning by the register status job
  registers/defects.json      consulted only to fill fields the status file omits

Outputs (all under state/nightly-fix/, all gitignored):

  <date>-<id>.prompt.md     the prompt sent to Codex
  <date>-<id>.codex.log     everything Codex printed
  <date>-<id>.md            a plain-English log of the run
  attempts.json             one line per attempt, for the 72 h backoff
  LOCK                      pid + timestamp while a run is live

Exit codes. launchd keeps one number per job and the morning digest reads it,
so the number has to mean something:

  0   the lane did its job or deliberately stood down -- a pull request opened,
      nothing eligible, a backoff, a stale morning status, a pull request
      already pending, a refused pr_check, Codex changed nothing, a leftover
      clone, or a trial that stopped at the local commit.
  2   a lock or configuration failure, or a safety rail refusing to continue.
  3+  the lane tried and could not finish. One code per outcome, listed in
      OUTCOME_EXIT_CODES below.

Python 3.9, standard library only. Run it with /usr/bin/python3.
"""

import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

UTC = datetime.timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import notify  # noqa: E402  (the push edge; opt-in via FULLY_AWARE_PUSH=1)

DEFAULT_STATUS_FILE = os.path.join(REPO_ROOT, "state", "defects-status.json")
DEFAULT_REGISTER_FILE = os.path.join(REPO_ROOT, "registers", "defects.json")
DEFAULT_STATE_DIR = os.path.join(REPO_ROOT, "state", "nightly-fix")
DEFAULT_CLONE_PARENT = "/Users/anthonyflores/code/.nightly-fix"

CODEX_TIMEOUT_S = 40 * 60
PR_CHECK_TIMEOUT_S = 20 * 60
GIT_TIMEOUT_S = 15 * 60
MCP_LIST_TIMEOUT_S = 5 * 60
LOCK_MAX_AGE_S = 6 * 3600
ATTEMPT_BACKOFF_S = 72 * 3600
CLONE_KEEP_DAYS = 7

# The morning status file is the lane's whole picture of what is broken. The
# 02:30 run always uses the previous morning's snapshot by design; more than a
# day and a half old means the morning job stopped and the lane should not act.
STATUS_MAX_AGE_S = 36 * 3600

# Every outcome that means the lane tried and could not finish gets its own
# number, so `launchctl list` and the morning digest can say WHICH way it
# failed. Anything not listed here exits 0: it either worked or it was a
# deliberate stand-down.
OUTCOME_EXIT_CODES = {
    "clone-failed": 3,
    "branch-failed": 4,
    "codex-isolation-failed": 5,
    "codex-failed": 6,
    "git-status-failed": 7,
    "wrong-branch": 8,
    "unsafe-diff": 9,
    "pr-check-failed": 10,
    "commit-failed": 11,
    "push-failed": 12,
    "pr-create-failed": 13,
}

# --- the push edge ---------------------------------------------------------
#
# Outcomes that need a human reach the phone; deliberate stand-downs stay in
# the digest. The wrapper (nightly-fix.sh) opts the live lane in with
# FULLY_AWARE_PUSH=1; tests never set it, so exercising every outcome against
# fixtures stays off the network.


def outcome_push(item_id, outcome, note="", pr_url=None):
    """``(title, message, priority)`` when a human must act, else ``None``.

    Two tiers only: a pull request waiting on a merge is a default-priority
    push; every tried-and-could-not-finish outcome is high. Stand-downs
    (no-changes, backoff, pr-pending, refused check, ...) return ``None`` --
    they are the digest's business, and a channel that pings on routine
    outcomes gets muted.
    """
    if outcome == "pr-opened":
        return ("Nightly fixer: PR opened for %s" % item_id,
                "Waiting on your merge. %s" % (pr_url or note or ""),
                "default")
    if outcome in OUTCOME_EXIT_CODES:
        return ("Nightly fixer could not finish %s" % item_id,
                "%s: %s -- %s (run log in state/nightly-fix/)"
                % (item_id, outcome, note or "no detail"),
                "high")
    return None

PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")

# --- Codex isolation -------------------------------------------------------
#
# The lane cannot use an isolated CODEX_HOME: the login credential is held in
# the machine keychain and bound to the real home, so an isolated home logs the
# lane out. So the global config is left alone and every tool it enables is
# switched off ON THE COMMAND LINE, by name, for this run only.

CODEX_CONFIG_FILE = os.path.expanduser("~/.codex/config.toml")

# Feature flags switched off for the run. "plugins" is the load-bearing one:
# the app connectors (mail, chat, docs, the GitHub connector, the computer
# history reader) are supplied by the plugin system, not by [mcp_servers], and
# per-plugin overrides do not reach them. Turning the feature off removes them
# from the session entirely -- verified against `codex ... mcp list`.
CODEX_DISABLED_FEATURES = (
    "plugins",
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "code_mode_host",
    "memories",
    "chronicle",
)

# Built-in Codex permission profile used to run the item's pr_check. It denies
# the network and denies writes outside the working directory. Named profiles
# have a leading colon; a profile is REQUIRED by `codex sandbox`.
CODEX_SANDBOX_PROFILE = ":workspace"

# The seatbelt denies writes under $HOME, and uv keeps its cache and its tool
# directory there, so a check that runs pytest through uvx cannot start. These
# point uv at /tmp, which the profile does allow, and the lane warms that
# scratch cache with its own fixed command before entering the sandbox -- the
# seatbelt has no network, so nothing can be downloaded from inside it.
CHECK_SCRATCH_DIR = "/tmp/nightly-fix-check-scratch"
CHECK_ENV = (
    "UV_CACHE_DIR=" + os.path.join(CHECK_SCRATCH_DIR, "uv-cache"),
    "UV_TOOL_DIR=" + os.path.join(CHECK_SCRATCH_DIR, "uv-tools"),
    "UV_PYTHON_INSTALL_DIR=" + os.path.join(CHECK_SCRATCH_DIR, "uv-python"),
)
CHECK_WARM_ARGV = ("uvx", "--with", "pytest", "python", "-c", "pass")

# --- the staging rail ------------------------------------------------------

# Codex can READ anything on this Mac even under a workspace-write sandbox, so
# the rail is on what leaves: an untracked file whose name looks like a secret
# is never staged, and neither is anything large or anything outside the clone.
CREDENTIAL_PATTERNS = (
    r"\.env$",
    r"defects\.env",
    r"\.pem$",
    r"id_rsa",
    r"token",
    r"secret",
    r"credential",
    r"auth\.json",
)
CREDENTIAL_RE = re.compile("|".join(CREDENTIAL_PATTERNS), re.IGNORECASE)
MAX_STAGED_BYTES = 1024 * 1024

DONE_MARKER = "NIGHTLY-FIX DONE"

FORBIDDEN_BRANCHES = {"main", "master", "head", "trunk", "default"}
CLONE_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*-\d{8}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SafetyError(Exception):
    """A rail refused the operation. Always fatal: exit 2."""


class ConfigError(Exception):
    """Bad or missing configuration. Always fatal: exit 2."""


class MissingStatus(Exception):
    """The morning job has not produced a status file yet. Not an error."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print("[%s] nightly-fix: %s" % (stamp, msg), flush=True)


def parse_now(text):
    """--now is local time when naive, converted to local when it carries a zone."""
    if not text:
        return datetime.datetime.now().astimezone()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ConfigError("invalid --now value %r: %s" % (text, exc))
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def parse_stamp(text):
    """Parse an ISO date or datetime from the register/status/attempt files."""
    if not text:
        return None
    text = str(text).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.datetime.combine(
                datetime.date.fromisoformat(text[:10]), datetime.time()
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=None).astimezone()
    return parsed


def date_stamp(now):
    return now.strftime("%Y%m%d")


def _prefixed_path(inherited):
    """PATH_PREFIX ahead of the inherited PATH, with every empty element dropped.

    An empty element ("a::b", a trailing ":") means "the current directory" to
    the shell, and a child of this lane must never pick binaries up from cwd.
    """
    parts = [p for p in (PATH_PREFIX + ":" + (inherited or "")).split(":") if p]
    return ":".join(parts)


def child_env():
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        env.pop(key, None)
    env["PATH"] = _prefixed_path(env.get("PATH", ""))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["IMPRINT_CAPTURE_ORIGIN"] = "automation"
    return env


def codex_binary():
    override = os.environ.get("NIGHTLY_FIX_CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex", path=_prefixed_path(os.environ.get("PATH", "")))
    return found or os.path.expanduser("~/.local/bin/codex")


# --------------------------------------------------------------------------
# safety rails -- coded, not merely documented
# --------------------------------------------------------------------------

def assert_safe_branch(name):
    """Never a default branch, never a flag, never a path trick."""
    if not name or not isinstance(name, str):
        raise SafetyError("empty branch name")
    lowered = name.strip().lower()
    if lowered in FORBIDDEN_BRANCHES:
        raise SafetyError("refusing to work on branch %r -- default branches are Anthony's" % name)
    if lowered.startswith("refs/heads/"):
        tail = lowered[len("refs/heads/"):]
        if tail in FORBIDDEN_BRANCHES:
            raise SafetyError("refusing to work on branch %r" % name)
    if name.startswith("-") or ".." in name or name.endswith(".lock") or " " in name:
        raise SafetyError("refusing malformed branch name %r" % name)
    return name


def assert_safe_item_id(item_id):
    """An id becomes part of filenames and a branch, so it must be one segment."""
    if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
        raise SafetyError("refusing unsafe defect id %r" % (item_id,))
    return item_id


def assert_safe_argv(argv):
    """No forced pushes, no history rewrites, no merging. Ever."""
    if not argv:
        raise SafetyError("empty command")
    parts = [str(a) for a in argv]
    exe = os.path.basename(parts[0])
    joined = " ".join(parts)
    if exe == "git":
        # case-sensitive on purpose: git's -F (message file) is not -f (force)
        for arg in parts[1:]:
            if arg in ("--force", "-f", "--force-with-lease", "--force-if-includes"):
                raise SafetyError("refusing a forced git operation: %s" % joined)
            if arg.startswith("--force"):
                raise SafetyError("refusing a forced git operation: %s" % joined)
        # skip "-C <dir>" and "-c key=value" values when looking for the verb
        verb = None
        i = 1
        while i < len(parts):
            arg = parts[i]
            if arg in ("-C", "-c"):
                i += 2
                continue
            if arg.startswith("-"):
                i += 1
                continue
            verb = arg
            break
        if verb in ("push",) and any(a.startswith("+") for a in parts[i + 1:]):
            raise SafetyError("refusing a forced refspec: %s" % joined)
        if verb in ("merge", "rebase", "reset", "filter-branch", "clean", "pull", "stash", "checkout"):
            if verb == "checkout" and "-b" in parts:
                pass  # creating our own branch inside our own fresh clone is fine
            else:
                raise SafetyError("refusing git %s -- this lane never rewrites or syncs a checkout" % verb)
    if exe == "gh":
        if any(parts[i:i + 2] == ["pr", "merge"] for i in range(1, len(parts) - 1)):
            raise SafetyError("refusing gh pr merge -- merge is Anthony's")
    return parts


def assert_safe_pr_check(command, clone_dir):
    """Refuse checks that could leave the fresh clone or break a git rail.

    The check is a shell string from the register, run by /bin/bash inside the
    clone, so this is a refusal list rather than a proof of safety: a check may
    only ever test the clone. Anything that names a place under a home
    directory (other than the clone itself), walks up with "..", pushes, calls
    gh, forces git, reroutes git with GIT_DIR/--work-tree, or reaches the Mac
    through sudo/launchctl is refused outright. False refusals are cheap (the
    run log says so and the item waits); a false pass is not.
    """
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ConfigError("the pull-request check is not a usable shell command")
    lowered = command.lower()
    if re.search(r"\bgit\b[^|;&]*\bpush\b", lowered):
        raise SafetyError("refusing a pull-request check that pushes -- only the lane pushes")
    if re.search(r"(^|[\s;|&(`$])gh(\s|$)", lowered):
        raise SafetyError("refusing a pull-request check that calls gh -- "
                         "pull requests and merges are never a check's business")
    if "--force" in lowered or re.search(r"\bgit\b[^|;&]*\s-[a-z]*f\b", lowered):
        raise SafetyError("refusing a pull-request check that can force a git operation")
    if re.search(r"\bgit_(dir|work_tree|index_file|common_dir)\b|--git-dir|--work-tree",
                 lowered):
        raise SafetyError("refusing a pull-request check that reroutes git to another tree")
    if re.search(r"\b(sudo|launchctl|crontab|osascript)\b", lowered):
        raise SafetyError("refusing a pull-request check that reaches outside the clone")
    without_clone = command.replace(os.path.realpath(clone_dir), "").replace(clone_dir, "")
    if "/Users/" in without_clone or "$HOME" in without_clone or "${HOME}" in without_clone:
        raise SafetyError("refusing a pull-request check that names a path outside the clone")
    if re.search(r"(^|[\s;|&=\"'(])~", without_clone):
        raise SafetyError("refusing a pull-request check that names a home directory")
    if re.search(r"(?<![A-Za-z0-9_.])\.\.(?![A-Za-z0-9_])", without_clone):
        raise SafetyError("refusing a pull-request check that walks up out of the clone")
    return command


def assert_not_inside_worktree(path):
    """The clone parent must not sit inside anybody's git work tree."""
    probe = os.path.realpath(os.path.abspath(path))
    while True:
        marker = os.path.join(probe, ".git")
        if os.path.exists(marker):
            raise SafetyError(
                "refusing to clone under %s: %s is already a git work tree" % (path, probe)
            )
        parent = os.path.dirname(probe)
        if parent == probe:
            return True
        probe = parent


def parse_porcelain_z(text):
    """`git status --porcelain -z` as a list of (code, path).

    NUL-separated on purpose: it is the only porcelain form that never quotes
    or escapes a path, so a filename with a space, a quote or a newline in it
    cannot slip past the rail below. A rename entry carries the old path as an
    extra field, which is consumed and ignored -- the new path is what would be
    committed.
    """
    fields = [f for f in (text or "").split("\0")]
    entries = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            continue
        code = field[:2]
        path = field[3:]
        if code[0] in ("R", "C"):
            i += 1  # the source path of the rename/copy
        entries.append((code, path))
    return entries


def unsafe_staged_paths(entries, clone_dir):
    """Why the rail refuses to stage this working tree, or [] when it is fine.

    Three refusals, in the order they are cheap to check: an untracked file
    whose NAME looks like a credential; anything outside the clone; anything
    over a megabyte. Codex can read this whole Mac even inside a workspace-write
    sandbox, so this is the rail on what leaves.
    """
    root = os.path.realpath(clone_dir)
    problems = []
    for code, path in entries:
        full = os.path.realpath(os.path.join(clone_dir, path))
        if full != root and not full.startswith(root + os.sep):
            problems.append("%s resolves outside the clone" % path)
            continue
        if code == "??" and CREDENTIAL_RE.search(path):
            problems.append("%s is untracked and named like a credential" % path)
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_STAGED_BYTES:
            problems.append("%s is %d bytes, over the %d byte limit"
                            % (path, size, MAX_STAGED_BYTES))
    return problems


# --------------------------------------------------------------------------
# command runner (injectable so tests never touch codex, git or gh)
# --------------------------------------------------------------------------

class Result(object):
    def __init__(self, rc, output=""):
        self.rc = rc
        self.output = output or ""

    def __repr__(self):
        return "Result(rc=%r, output=%r)" % (self.rc, self.output[:80])


class Runner(object):
    """Runs real commands. Every external call in this script goes through it."""

    def run(self, argv, cwd=None, timeout=None, stdin_path=None, log_path=None):
        stdin = None
        try:
            if stdin_path:
                stdin = open(stdin_path, "rb")
            try:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=child_env(),
                    stdin=stdin if stdin else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
                out = proc.stdout.decode("utf-8", "replace")
                rc = proc.returncode
            except subprocess.TimeoutExpired as exc:
                out = (exc.output or b"").decode("utf-8", "replace")
                out += "\n[nightly-fix] timed out after %ss\n" % timeout
                rc = 124
            except FileNotFoundError as exc:
                out = "[nightly-fix] command not found: %s\n" % exc
                rc = 127
        finally:
            if stdin:
                stdin.close()
        if log_path:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("$ %s\n" % " ".join(argv))
                fh.write(out)
                fh.write("\n[exit %s]\n" % rc)
        return Result(rc, out)


def guarded_run(runner, argv, **kwargs):
    assert_safe_argv(argv)
    return runner.run([str(a) for a in argv], **kwargs)


# --------------------------------------------------------------------------
# status file, register, attempts
# --------------------------------------------------------------------------

def load_status(path):
    if not os.path.exists(path):
        raise MissingStatus(
            "no status file at %s -- the morning register job has not run" % path
        )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        raise ConfigError("cannot read status file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("status file %s is not a JSON object" % path)
    if data.get("schema") != "defect-status/v1":
        raise ConfigError("status file %s is not defect-status/v1" % path)
    items = data.get("items")
    if not isinstance(items, list):
        raise ConfigError("status file %s has no items list" % path)
    if not isinstance(data.get("nightly_eligible"), list):
        raise ConfigError("status file %s has no nightly_eligible list" % path)
    return data


REGISTER_FIELDS = (
    "repo", "remote", "verify", "pr_check", "provisional", "not_before",
    "symptom", "fix_hint", "system", "severity", "owner", "fix_scope", "size",
)


def merge_register(items, register_path):
    """Fill fields the status file left out from the register (same ids)."""
    if not register_path or not os.path.exists(register_path):
        return items
    try:
        with open(register_path, encoding="utf-8") as fh:
            reg = json.load(fh)
    except (ValueError, OSError):
        return items
    if not isinstance(reg, dict):
        raise ConfigError("register file %s is not a JSON object" % register_path)
    entries = reg.get("items", [])
    if not isinstance(entries, list):
        raise ConfigError("register file %s has no items list" % register_path)
    by_id = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            by_id[entry["id"]] = entry
    merged = []
    for item in items:
        copy = dict(item)
        source = by_id.get(copy.get("id"))
        if source:
            for field in REGISTER_FIELDS:
                if copy.get(field) is None and source.get(field) is not None:
                    copy[field] = source[field]
            if copy.get("open_since") is None and source.get("since"):
                copy["open_since"] = source["since"]
        merged.append(copy)
    return merged


def read_attempts(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        # tolerate a JSON-lines file written by an older run
        records = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        found = data.get("attempts")
        return found if isinstance(found, list) else []
    return []


def write_attempts(path, records):
    lines = ["{\"schema\": \"nightly-fix-attempts/v1\", \"attempts\": ["]
    body = [json.dumps(rec, sort_keys=True) for rec in records]
    lines.append(",\n".join(body))
    lines.append("]}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def record_attempt(path, item_id, now, mode, outcome, detail="", branch=""):
    records = read_attempts(path)
    records.append({
        "id": item_id,
        "at": now.isoformat(),
        "mode": mode,
        "outcome": outcome,
        "detail": detail[:200],
        "branch": branch or "",
    })
    write_attempts(path, records)
    return records


def last_attempt_at(records, item_id):
    latest = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("id") != item_id:
            continue
        when = parse_stamp(rec.get("at"))
        if when and (latest is None or when > latest):
            latest = when
    return latest


def pending_pr_branch(records, item_id):
    """The branch of the most recent pull request this lane opened for an item.

    None means it has never opened one. An empty string means it did but the
    record is from an older version that did not save the branch name -- the
    caller cannot ask GitHub about it, and treats it as still open.

    This deliberately reads the OUTCOME, not the timestamp: a pull request that
    is still waiting for Anthony must keep blocking the item however many nights
    pass, or the lane opens a second one for the same defect every 72 hours.
    """
    branch = None
    latest = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("id") != item_id:
            continue
        if rec.get("outcome") != "pr-opened":
            continue
        when = parse_stamp(rec.get("at"))
        if when is None:
            continue
        if latest is None or when >= latest:
            latest = when
            branch = str(rec.get("branch") or "")
    return branch


def pr_state(runner, remote, branch):
    """MERGED / CLOSED / OPEN / UNKNOWN for a branch's pull request.

    Anything that is not a clean answer comes back UNKNOWN, and the caller
    treats UNKNOWN as "still open". Failing closed here costs one night of not
    working an item; failing open costs Anthony a duplicate pull request.
    """
    if not branch or not remote:
        return "UNKNOWN"
    argv = ["gh", "pr", "view", branch, "--repo", str(remote), "--json", "state"]
    try:
        res = guarded_run(runner, argv, timeout=GIT_TIMEOUT_S)
    except SafetyError:
        return "UNKNOWN"
    if res.rc != 0:
        return "UNKNOWN"
    try:
        data = json.loads(res.output.strip() or "{}")
    except ValueError:
        return "UNKNOWN"
    if not isinstance(data, dict):
        return "UNKNOWN"
    return str(data.get("state") or "UNKNOWN").upper()


def status_age_seconds(status, now):
    """How old the morning snapshot is, or None when it does not say."""
    when = parse_stamp((status or {}).get("generated_at"))
    if when is None:
        return None
    return (now - when).total_seconds()


# --------------------------------------------------------------------------
# eligibility and selection
# --------------------------------------------------------------------------

def item_open_since(item):
    return parse_stamp(
        item.get("open_since") or item.get("since") or item.get("added")
    )


def ineligibility_reason(item, now, attempts, ignore_backoff=False, pr_blocked=()):
    """Return None when the nightly lane may take this item, else why not."""
    if not isinstance(item, dict) or not item.get("id"):
        return "not an item"
    status = str(item.get("status") or "").lower()
    if status == "deferred":
        return "deferred"
    if status != "open":
        return "status is %r, not open" % (item.get("status"),)
    if str(item.get("owner") or "").lower() != "codex":
        return "owner is %r, not codex" % (item.get("owner"),)
    # A P0 is the kind of thing Anthony wants to see fixed himself. The status
    # job never lists one as nightly-eligible today; saying so here as well
    # means a future change to that job cannot quietly hand one to the lane.
    if str(item.get("severity") or "").upper() == "P0":
        return "severity is P0; the nightly lane never takes a P0 unattended"
    if str(item.get("fix_scope") or "").lower() != "repo-pr":
        return "fix_scope is %r, not repo-pr" % (item.get("fix_scope"),)
    if str(item.get("size") or "").upper() != "S":
        return "size is %r, not S" % (item.get("size"),)
    if item.get("provisional"):
        return "provisional verify"
    not_before = parse_stamp(item.get("not_before"))
    if not_before and not_before > now:
        return "deferred until %s" % item.get("not_before")
    remote = item.get("remote")
    if not remote or not str(remote).strip():
        return "no remote to open a pull request against"
    if "/" not in str(remote):
        return "remote %r is not org/name" % (remote,)
    if item["id"] in pr_blocked:
        return "a pull request this lane opened for it is still waiting"
    if not ignore_backoff:
        when = last_attempt_at(attempts, item["id"])
        if when and (now - when).total_seconds() < ATTEMPT_BACKOFF_S:
            hours = int((now - when).total_seconds() // 3600)
            return "tried %sh ago (72h backoff)" % hours
    return None


def select_item(items, now, attempts, forced_id=None, pr_blocked=()):
    """Oldest eligible item first. Returns (item, note) -- item may be None."""
    if forced_id:
        for item in items:
            if item.get("id") == forced_id:
                reason = ineligibility_reason(item, now, attempts, pr_blocked=pr_blocked)
                if reason:
                    return None, "%s cannot be forced: %s" % (forced_id, reason)
                return item, "forced"
        return None, "no item with id %s in the status file" % forced_id

    eligible = []
    for item in items:
        if ineligibility_reason(item, now, attempts, pr_blocked=pr_blocked) is None:
            eligible.append(item)
    if not eligible:
        return None, "no eligible item tonight"

    def sort_key(item):
        when = item_open_since(item)
        return (when or now, str(item.get("id")))

    eligible.sort(key=sort_key)
    chosen = eligible[0]
    note = "oldest of %d eligible (open since %s)" % (
        len(eligible), chosen.get("open_since") or chosen.get("since") or "unknown"
    )
    return chosen, note


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------

class Lock(object):
    def __init__(self, path, now):
        self.path = path
        self.now = now
        self.held = False

    def live_run_present(self):
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            when = parse_stamp(data.get("at"))
        except (ValueError, OSError):
            when = None
        if when is None:
            when = datetime.datetime.fromtimestamp(
                os.path.getmtime(self.path)
            ).astimezone()
        age = (self.now - when).total_seconds()
        return age < LOCK_MAX_AGE_S

    def acquire(self):
        payload = {"pid": os.getpid(), "at": self.now.isoformat()}
        # A guard file that is never deleted serialises the whole
        # check / remove-stale / create sequence, so two runs starting in the
        # same instant can never both take over one stale lock.
        try:
            guard_fd = os.open(self.path + ".guard", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ConfigError("cannot open the lock guard %s.guard: %s" % (self.path, exc))
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            return self._acquire_locked(payload)
        finally:
            try:
                fcntl.flock(guard_fd, fcntl.LOCK_UN)
            finally:
                os.close(guard_fd)

    def _acquire_locked(self, payload):
        while True:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self.live_run_present():
                    return False
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise ConfigError("cannot replace stale lock %s: %s" % (self.path, exc))
                continue
            except OSError as exc:
                raise ConfigError("cannot write the lock file %s: %s" % (self.path, exc))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                    fh.write("\n")
            except OSError as exc:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                raise ConfigError("cannot write the lock file %s: %s" % (self.path, exc))
            self.held = True
            return True

    def release(self):
        if self.held:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    mine = json.load(fh).get("pid") == os.getpid()
            except (OSError, ValueError, AttributeError):
                mine = False
            if mine:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
        self.held = False


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------

def prune_clones(parent, now, keep_days=CLONE_KEEP_DAYS):
    """Delete clone dirs older than a week. Only ours, only under the parent."""
    removed = []
    if not os.path.isdir(parent):
        return removed
    cutoff = now.timestamp() - keep_days * 86400
    for name in sorted(os.listdir(parent)):
        path = os.path.join(parent, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if not CLONE_DIR_RE.match(name):
            continue
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            shutil.rmtree(path)
            removed.append(name)
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------

PROMPT_CONSTRAINTS = """Constraints, all of them hard:

- Work only inside this clone. Do not touch any other repository or anything
  else on this machine.
- Make the smallest correct diff that fixes the defect. No refactors, no
  drive-by cleanups, no reformatting of untouched code.
- Add no new dependencies. If the code is Python, standard library only.
- Do not touch CI configuration (.github/workflows and friends).
- Do not force-push, do not rewrite history, do not merge anything, do not open
  the pull request yourself. The lane does the commit and the pull request; a
  human does the merge.
- Where the repository has tests, update an existing test or add one that fails
  before your change and passes after it.
- Do not edit the defect register itself.
"""


def build_prompt(item, branch, clone_dir, now):
    item_id = item.get("id", "UNKNOWN")
    verify = item.get("verify") or ""
    pr_check = item.get("pr_check") or ""

    parts = []
    parts.append("# Nightly defect fix: %s\n" % item_id)
    parts.append(
        "You are the overnight fix lane for Anthony's defect register. Nobody is\n"
        "watching. Fix exactly one recorded defect in this fresh clone and stop.\n"
    )
    parts.append("\n## The defect\n")
    parts.append("- Id: %s\n" % item_id)
    parts.append("- System: %s\n" % (item.get("system") or "unknown"))
    parts.append("- Severity: %s\n" % (item.get("severity") or "unknown"))
    parts.append("- Open since: %s\n" % (item.get("open_since") or item.get("since") or "unknown"))
    parts.append("- Repository: %s\n" % (item.get("remote") or "unknown"))
    parts.append("- Clone you are working in: %s\n" % clone_dir)
    parts.append("- Branch already checked out for you: %s\n" % branch)
    parts.append("\nSymptom (what is wrong, in Anthony's words):\n\n    %s\n"
                 % (item.get("symptom") or "not recorded"))
    if item.get("fix_hint"):
        parts.append("\nFix hint recorded with the defect (a strong suggestion, not a spec):\n\n    %s\n"
                     % item["fix_hint"])

    parts.append("\n## The two commands\n")
    if verify:
        parts.append(
            "\nThe register's verify command is:\n\n    %s\n\n"
            "This is how the defect is judged CLOSED on Anthony's machine, and it\n"
            "usually passes only after your pull request is merged and the daily job\n"
            "runs again. Do NOT chase it, do not try to make it pass here, and do not\n"
            "edit state or generated files to force it green. It is context only.\n"
            % verify
        )
    else:
        parts.append("\nThis defect records no verify command.\n")

    if pr_check:
        parts.append(
            "\nThe pull-request check, which runs INSIDE this clone, is:\n\n    %s\n\n"
            "This one is your gate. It must exit 0 before you finish. Run it yourself,\n"
            "read the output, and fix what it reports.\n" % pr_check
        )
    else:
        parts.append(
            "\nThis defect records no pull-request check. Run whatever test suite the\n"
            "repository already has and leave it green.\n"
        )

    parts.append("\n## " + PROMPT_CONSTRAINTS)
    parts.append(
        "\n## Finishing\n\n"
        "When the change is complete and the check above exits 0, print the exact\n"
        "line\n\n    %s\n\nfollowed by a three-line summary: line 1 what you changed,\n"
        "line 2 how you proved it, line 3 anything Anthony should know before he\n"
        "merges. Leave the changes in the working tree; do not commit.\n" % DONE_MARKER
    )
    parts.append("\nPrompt written %s by tools/nightly-fix.py.\n" % now.isoformat())
    return "".join(parts)


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------

_TOML_SECTION_RE = re.compile(r"^\s*\[\s*(mcp_servers|plugins)\s*\.\s*(.+?)\s*\]\s*$")
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_first_key(rest):
    """First key of a dotted TOML path, honouring quotes.

    'node_repl.env'                  -> 'node_repl'
    '"firecrawl-mcp".tools.scrape'   -> 'firecrawl-mcp'
    '"github@openai-curated"'        -> 'github@openai-curated'
    """
    rest = rest.strip()
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        out = []
        i = 1
        while i < len(rest):
            char = rest[i]
            if quote == '"' and char == "\\" and i + 1 < len(rest):
                out.append(rest[i + 1])
                i += 2
                continue
            if char == quote:
                break
            out.append(char)
            i += 1
        return "".join(out)
    return rest.split(".", 1)[0].strip()


def _toml_quote_key(name):
    """Render a name as a TOML key: bare when it can be, quoted when it cannot."""
    if _TOML_BARE_KEY_RE.match(name):
        return name
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


def codex_tool_names(config_text):
    """Every [mcp_servers.<name>] and [plugins.<name>] name in a config file.

    Sub-tables collapse to their parent, so mcp_servers.node_repl.env counts
    once. Returns (servers, plugins), both sorted and deduplicated.
    """
    servers = set()
    plugins = set()
    for line in (config_text or "").splitlines():
        found = _TOML_SECTION_RE.match(line)
        if not found:
            continue
        name = _toml_first_key(found.group(2))
        if not name:
            continue
        (servers if found.group(1) == "mcp_servers" else plugins).add(name)
    return sorted(servers), sorted(plugins)


def read_codex_config_text(path=None):
    """The global Codex config as text. Missing or unreadable reads as empty.

    Deliberately NOT parsed as TOML: /usr/bin/python3 here is 3.9, which has no
    tomllib, and the lane only ever needs the section headers.
    """
    path = path or CODEX_CONFIG_FILE
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def codex_isolation_argv(servers, plugins):
    """The overrides that strip Codex down to a model and a shell, for one run.

    Nothing here edits ~/.codex/config.toml; these are command-line overrides
    that live and die with the process.
    """
    argv = []
    for name in servers:
        argv += ["-c", "mcp_servers.%s.enabled=false" % _toml_quote_key(name)]
    for name in plugins:
        argv += ["-c", "plugins.%s.enabled=false" % _toml_quote_key(name)]
    for feature in CODEX_DISABLED_FEATURES:
        argv += ["--disable", feature]
    argv += ["-c", "approval_policy=never"]
    return argv


def codex_overrides(config_text=None):
    text = read_codex_config_text() if config_text is None else config_text
    servers, plugins = codex_tool_names(text)
    return codex_isolation_argv(servers, plugins)


_MCP_COLUMN_RE = re.compile(r"\s{2,}")


def parse_mcp_list(text):
    """Rows of `codex mcp list` as (name, status).

    The command prints two tables (local servers, then remote connectors) with
    different columns, so the status is found by looking for the last standalone
    "enabled"/"disabled" cell rather than by counting columns. A row whose status
    cannot be read comes back as "unknown", which the caller treats as NOT
    disabled -- this rail fails closed.
    """
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cells = [cell for cell in _MCP_COLUMN_RE.split(stripped) if cell]
        if len(cells) < 2 or cells[0] == "Name":
            continue
        status = "unknown"
        for cell in cells[1:]:
            if cell in ("enabled", "disabled"):
                status = cell
        rows.append((cells[0], status))
    return rows


def mcp_rows_still_live(text):
    """Names from `codex mcp list` that are not reported disabled."""
    return [name for name, status in parse_mcp_list(text) if status != "disabled"]


def codex_mcp_list_argv(overrides):
    return [codex_binary()] + list(overrides) + ["mcp", "list"]


def codex_argv(clone_dir, last_message_path, prompt, overrides=()):
    return [
        codex_binary(), "exec",
    ] + list(overrides) + [
        "-C", clone_dir,
        "-s", "workspace-write",
        "-o", last_message_path,
        prompt,
    ]


def warm_check_scratch(runner, command):
    """Fill the scratch uv cache before the seatbelt closes, if the check needs it.

    The seatbelt denies the network, so a check that installs its test runner
    through uvx has to find it already cached, and the cache has to live where
    the seatbelt allows writes. This runs ONE fixed lane-owned command with
    fixed arguments -- nothing from the register reaches it, the register's
    check string is only inspected for the word "uv". It is a few milliseconds
    once the cache is warm, and a failure here is not fatal: the check simply
    runs and reports for itself.
    """
    if "uv" not in (command or ""):
        return False
    try:
        for value in CHECK_ENV:
            os.makedirs(value.split("=", 1)[1], exist_ok=True)
    except OSError:
        return False
    argv = ["/usr/bin/env"] + list(CHECK_ENV) + list(CHECK_WARM_ARGV)
    runner.run(argv, timeout=PR_CHECK_TIMEOUT_S)
    return True


def pr_check_argv(clone_dir, command):
    """The item's pr_check, wrapped in a Codex seatbelt.

    The check is a shell string out of the register, so it gets the same
    treatment as model output: the profile denies the network and denies every
    write outside the clone. /usr/bin/env supplies the scratch cache locations
    uv needs, because the seatbelt will not let it write under $HOME.
    """
    return [
        codex_binary(), "sandbox",
        "--permission-profile", CODEX_SANDBOX_PROFILE,
        "-C", clone_dir,
        "--",
        "/usr/bin/env",
    ] + list(CHECK_ENV) + [
        "/bin/bash", "-o", "pipefail", "-c", command,
    ]


# --------------------------------------------------------------------------
# the run log
# --------------------------------------------------------------------------

def write_run_log(path, item, mode, now, outcome, story, pr_url=None, extra=None):
    lines = []
    lines.append("# Nightly fix %s -- %s\n" % (item.get("id", "?"), now.strftime("%Y-%m-%d")))
    lines.append("")
    lines.append("Mode: %s" % mode)
    lines.append("System: %s" % (item.get("system") or "unknown"))
    lines.append("Repository: %s" % (item.get("remote") or "none"))
    lines.append("Outcome: %s" % outcome)
    lines.append("")
    lines.append("## What happened")
    lines.append("")
    for line in story:
        lines.append("- %s" % line)
    lines.append("")
    lines.append("## Pull request")
    lines.append("")
    if pr_url:
        lines.append("%s" % pr_url)
        lines.append("")
        lines.append("It is open and waiting. Merge is Anthony's.")
    else:
        lines.append("None. %s" % (extra or "See the outcome above for why."))
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------

def commit_message(item):
    symptom = " ".join((item.get("symptom") or "fix recorded defect").split())
    subject_body = symptom[:70].rstrip()
    return "fix(%s): %s" % (item.get("id", "UNKNOWN"), subject_body)


def commit_body(item, pr_check_note):
    lines = []
    lines.append("Defect %s from the register." % item.get("id", "UNKNOWN"))
    lines.append("")
    lines.append("Symptom: %s" % " ".join((item.get("symptom") or "not recorded").split()))
    lines.append("")
    lines.append("Verify (passes on Anthony's machine after this merges):")
    lines.append("  %s" % (item.get("verify") or "none recorded"))
    lines.append("")
    lines.append("Pull-request check: %s" % pr_check_note)
    lines.append("")
    lines.append("opened by the fully-aware nightly lane; merge is Anthony's")
    return "\n".join(lines) + "\n"


def run_item(item, mode, now, paths, runner, codex_runner=None):
    """Clone, fix, prove, commit, and (live only) open the pull request."""
    item_id = item["id"]
    assert_safe_item_id(item_id)
    codex_runner = codex_runner or runner
    stamp = date_stamp(now)
    remote = item["remote"]
    branch = "fix/%s-%s" % (item_id.lower(), stamp)
    assert_safe_branch(branch)

    clone_dir = os.path.join(paths["clone_parent"], "%s-%s" % (item_id, stamp))
    prompt_path = os.path.join(paths["state_dir"], "%s-%s.prompt.md" % (stamp, item_id))
    codex_log = os.path.join(paths["state_dir"], "%s-%s.codex.log" % (stamp, item_id))
    codex_last = os.path.join(paths["state_dir"], "%s-%s.last.md" % (stamp, item_id))
    run_log = os.path.join(paths["state_dir"], "%s-%s.md" % (stamp, item_id))

    story = []

    def finish(outcome, note, pr_url=None):
        write_run_log(run_log, item, mode, now, outcome, story, pr_url=pr_url, extra=note)
        record_attempt(paths["attempts"], item_id, now, mode, outcome, note or "",
                       branch=branch)
        code = OUTCOME_EXIT_CODES.get(outcome, 0)
        log("outcome: %s -- %s" % (outcome, note or ""))
        log("run log: %s" % run_log)
        if code:
            log("exit %d: the lane could not finish this item" % code)
        plan = outcome_push(item_id, outcome, note, pr_url)
        if plan:
            notify.push(plan[0], plan[1], priority=plan[2])
        return code

    # The recorded check is validated BEFORE anything is cloned or Codex is
    # paid for. A refused check is a handled outcome -- run log plus attempt
    # record -- so the 72-hour backoff stops the lane re-trying it every night.
    pr_check = item.get("pr_check")
    if pr_check:
        try:
            assert_safe_pr_check(pr_check, clone_dir)
        except (SafetyError, ConfigError) as exc:
            story.append("The recorded pull-request check was refused before anything "
                         "was cloned: %s." % exc)
            story.append("Fix the check in registers/defects.json; nothing else was done.")
            return finish("pr-check-refused",
                          "the recorded pull-request check was refused: %s" % exc)

    # A leftover clone is a handled outcome, so it is checked FIRST. The other
    # order made the branch unreachable: a leftover clone is by definition a git
    # work tree, so the assertion below always fired before this could run, and
    # a kept clone crashed the lane with exit 2 instead of logging.
    if os.path.exists(clone_dir):
        story.append("A clone directory from an earlier run today is still at %s." % clone_dir)
        return finish("clone-exists",
                      "A clone from an earlier run today is still on disk at %s; "
                      "inspect or delete it, then re-run." % clone_dir)

    # The clone PARENT must not sit inside anyone's work tree -- the clone
    # itself does not exist yet, and once it does it is a work tree by design.
    assert_not_inside_worktree(paths["clone_parent"])
    os.makedirs(paths["clone_parent"], exist_ok=True)

    log("cloning %s into %s" % (remote, clone_dir))
    res = guarded_run(runner, ["gh", "repo", "clone", remote, clone_dir],
                      timeout=GIT_TIMEOUT_S, log_path=codex_log)
    if res.rc != 0 or not os.path.isdir(clone_dir):
        story.append("The clone of %s failed." % remote)
        return finish("clone-failed", "cloning %s failed (exit %s)" % (remote, res.rc))
    story.append("Cloned %s fresh into %s." % (remote, clone_dir))

    res = guarded_run(runner, ["git", "-C", clone_dir, "checkout", "-b", branch],
                      timeout=GIT_TIMEOUT_S, log_path=codex_log)
    if res.rc != 0:
        story.append("Could not create the branch %s." % branch)
        return finish("branch-failed", "creating branch %s failed (exit %s)" % (branch, res.rc))
    story.append("Created the branch %s." % branch)

    prompt = build_prompt(item, branch, clone_dir, now)
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    story.append("Wrote the prompt to %s." % prompt_path)

    # Strip Codex down to a model and a sandboxed shell for this run, then PROVE
    # it before spending anything. `codex mcp list` is asked with exactly the
    # overrides the model will get, and only the names and statuses it reports
    # are written down -- never the commands, arguments or environment the
    # table also prints, which come from Anthony's private config.
    overrides = codex_overrides()
    log("codex isolation: %d override(s) covering every server and plugin named "
        "in the global config" % len(overrides))
    probe = runner.run(codex_mcp_list_argv(overrides), timeout=MCP_LIST_TIMEOUT_S)
    rows = parse_mcp_list(probe.output)
    with open(codex_log, "a", encoding="utf-8") as fh:
        fh.write("$ codex <isolation overrides> mcp list\n")
        for name, state in rows:
            fh.write("%s %s\n" % (name, state))
        fh.write("[exit %s]\n" % probe.rc)
    if probe.rc != 0:
        story.append("Could not verify the Codex isolation: `codex mcp list` exited "
                     "%s. Nothing was run." % probe.rc)
        if mode != "trial":
            _drop_clone(clone_dir, story)
        return finish("codex-isolation-failed",
                      "codex mcp list exited %s, so the isolation could not be "
                      "verified" % probe.rc)
    live = mcp_rows_still_live(probe.output)
    if live or not rows:
        detail = ", ".join(live) if live else "the table was empty"
        story.append("Refusing to launch Codex: these tools were still reachable "
                     "after the isolation overrides: %s." % detail)
        if mode != "trial":
            _drop_clone(clone_dir, story)
        return finish("codex-isolation-failed",
                      "still reachable after isolation: %s" % detail)
    story.append("Verified the Codex isolation: all %d server(s) report disabled, "
                 "and the plugin system is off." % len(rows))

    argv = codex_argv(clone_dir, codex_last, prompt, overrides)
    log("running codex with the configured default model (up to %d minutes)"
        % (CODEX_TIMEOUT_S // 60))
    res = guarded_run(codex_runner, argv, cwd=clone_dir, timeout=CODEX_TIMEOUT_S,
                      log_path=codex_log)
    if res.rc != 0:
        story.append("Codex exited %s. Its output is in %s." % (res.rc, codex_log))
        if mode != "trial":
            _drop_clone(clone_dir, story)
        return finish("codex-failed",
                      "Codex exited %s; read %s" % (res.rc, codex_log))
    story.append("Codex finished. Its output is in %s." % codex_log)

    res = guarded_run(runner, ["git", "-C", clone_dir, "status", "--porcelain"],
                      timeout=GIT_TIMEOUT_S)
    if res.rc != 0:
        story.append("Could not read the working tree state.")
        return finish("git-status-failed", "git status failed (exit %s)" % res.rc)
    if not res.output.strip():
        story.append("Codex changed no files, so there is nothing to propose.")
        log("no changes")
        if mode != "trial":
            _drop_clone(clone_dir, story)
        return finish("no-changes", "Codex made no changes; the item stays open")
    changed = len([ln for ln in res.output.splitlines() if ln.strip()])
    story.append("Codex changed %d file(s)." % changed)

    # Codex may have switched branches inside the clone; never commit or push
    # from anywhere but the branch this run created.
    res = guarded_run(runner, ["git", "-C", clone_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                      timeout=GIT_TIMEOUT_S)
    head = (res.output or "").strip()
    if res.rc != 0 or head != branch:
        story.append("The clone is on %r, not %s; nothing was committed or pushed."
                     % (head or "?", branch))
        story.append("The clone is kept at %s so the work can be inspected." % clone_dir)
        return finish("wrong-branch",
                      "the clone ended on %r instead of %s; clone kept at %s"
                      % (head or "?", branch, clone_dir))

    pr_check = item.get("pr_check")
    if pr_check:
        # Re-checked now that the clone exists: this call resolves the real
        # path (symlinks included), so it can refuse what the pre-clone call
        # accepted. Handled the same way -- run log plus attempt record --
        # rather than escaping to main() as an exit-2 crash with no run log.
        try:
            assert_safe_pr_check(pr_check, clone_dir)
        except (SafetyError, ConfigError) as exc:
            story.append("The recorded pull-request check was refused after the "
                         "clone was made: %s." % exc)
            story.append("Nothing was committed or pushed. The clone is kept at %s."
                         % clone_dir)
            return finish("pr-check-refused",
                          "the recorded pull-request check was refused after cloning: "
                          "%s; clone kept at %s" % (exc, clone_dir))
        log("running the pull-request check inside a codex sandbox")
        warm_check_scratch(runner, pr_check)
        res = guarded_run(runner, pr_check_argv(clone_dir, pr_check),
                          cwd=clone_dir, timeout=PR_CHECK_TIMEOUT_S, log_path=codex_log)
        if res.rc != 0:
            tail = " ".join(res.output.strip().splitlines()[-2:])[:200]
            story.append("The pull-request check failed (exit %s): %s" % (res.rc, tail))
            story.append("The clone is kept at %s so the failure can be read." % clone_dir)
            return finish("pr-check-failed",
                          "the pull-request check failed (exit %s); clone kept at %s"
                          % (res.rc, clone_dir))
        story.append("The pull-request check passed.")
        pr_check_note = "passed (%s)" % pr_check
    else:
        story.append("This defect records no pull-request check, so none was run.")
        pr_check_note = "none recorded for this defect"

    subject = commit_message(item)
    body = commit_body(item, pr_check_note)
    msg_fd, msg_path = tempfile.mkstemp(prefix="nightly-fix-msg-", suffix=".txt")
    with os.fdopen(msg_fd, "w", encoding="utf-8") as fh:
        fh.write(subject + "\n\n" + body)

    # Read what is about to be staged BEFORE staging it. `git add -A` on its own
    # commits whatever Codex left in the clone, and Codex can read this whole
    # Mac even inside a workspace-write sandbox.
    res = guarded_run(runner,
                      ["git", "-C", clone_dir, "status", "--porcelain", "-z"],
                      timeout=GIT_TIMEOUT_S)
    if res.rc != 0:
        story.append("Could not list what would be staged.")
        return finish("git-status-failed", "git status -z failed (exit %s)" % res.rc)
    entries = parse_porcelain_z(res.output)
    story.append("About to stage %d path(s): %s"
                 % (len(entries), ", ".join("%s %s" % (c, p) for c, p in entries) or "none"))
    problems = unsafe_staged_paths(entries, clone_dir)
    if problems:
        story.append("The staging rail refused this working tree: %s."
                     % "; ".join(problems))
        story.append("Nothing was staged, committed or pushed. The clone is kept "
                     "at %s so it can be read." % clone_dir)
        return finish("unsafe-diff",
                      "the staging rail refused %d path(s): %s; clone kept at %s"
                      % (len(problems), "; ".join(problems), clone_dir))

    res = guarded_run(runner, ["git", "-C", clone_dir, "add", "-A"], timeout=GIT_TIMEOUT_S)
    if res.rc != 0:
        story.append("Staging the change failed.")
        return finish("commit-failed", "git add failed (exit %s)" % res.rc)
    res = guarded_run(runner,
                      ["git", "-C", clone_dir, "-c", "commit.gpgsign=false",
                       "commit", "-F", msg_path],
                      timeout=GIT_TIMEOUT_S, log_path=codex_log)
    if res.rc != 0:
        story.append("The commit failed.")
        return finish("commit-failed", "git commit failed (exit %s)" % res.rc)
    story.append("Committed as: %s" % subject)

    if mode == "trial":
        story.append("Trial mode: nothing was pushed and no pull request was opened.")
        story.append("The clone is kept at %s." % clone_dir)
        try:
            os.remove(msg_path)
        except OSError:
            pass
        return finish("committed-locally",
                      "trial mode stops at the commit; clone kept at %s" % clone_dir)

    res = guarded_run(runner, ["git", "-C", clone_dir, "push", "-u", "origin", branch],
                      timeout=GIT_TIMEOUT_S, log_path=codex_log)
    if res.rc != 0:
        story.append("The push failed; the clone is kept at %s." % clone_dir)
        return finish("push-failed", "git push failed (exit %s); clone kept at %s"
                      % (res.rc, clone_dir))
    story.append("Pushed the branch %s." % branch)

    pr_fd, pr_body_path = tempfile.mkstemp(prefix="nightly-fix-pr-", suffix=".md")
    with os.fdopen(pr_fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    res = guarded_run(runner,
                      ["gh", "pr", "create", "--repo", remote, "--head", branch,
                       "--title", subject, "--body-file", pr_body_path],
                      cwd=clone_dir, timeout=GIT_TIMEOUT_S, log_path=codex_log)
    try:
        os.remove(pr_body_path)
        os.remove(msg_path)
    except OSError:
        pass
    if res.rc != 0:
        story.append("Opening the pull request failed; the branch is pushed, the clone is kept.")
        return finish("pr-create-failed",
                      "gh pr create failed (exit %s); branch %s is pushed, clone kept at %s"
                      % (res.rc, branch, clone_dir))

    pr_url = ""
    for line in res.output.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            pr_url = line
    story.append("Opened the pull request%s." % ((": " + pr_url) if pr_url else ""))
    log("pull request: %s" % (pr_url or "opened (no url printed)"))
    _drop_clone(clone_dir, story)
    return finish("pr-opened", pr_url or "opened", pr_url=pr_url or "opened, url not printed")


def _drop_clone(clone_dir, story):
    try:
        shutil.rmtree(clone_dir)
        story.append("Deleted the clone.")
    except OSError as exc:
        story.append("Could not delete the clone at %s: %s" % (clone_dir, exc))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nightly-fix.py",
        description="Fix the oldest small nightly-eligible defect in a fresh clone.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run",
                      help="pick the item and write the prompt only (default)")
    mode.add_argument("--trial", dest="mode", action="store_const", const="trial",
                      help="clone, run codex, run the check, commit locally; no push, no PR")
    mode.add_argument("--live", dest="mode", action="store_const", const="live",
                      help="everything, ending in a pull request")
    parser.set_defaults(mode="dry-run")
    parser.add_argument("--item", help="force a specific defect id")
    parser.add_argument("--now", help="ISO timestamp to use as the clock")
    parser.add_argument("--status-file", default=None)
    parser.add_argument("--register-file", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--clone-parent", default=None)
    return parser.parse_args(argv)


def resolve_paths(args):
    def rooted(value):
        value = os.path.expanduser(str(value))
        if not os.path.isabs(value):
            value = os.path.join(REPO_ROOT, value)
        return os.path.abspath(value)

    state_dir = rooted(args.state_dir or os.environ.get("NIGHTLY_FIX_STATE_DIR")
                       or DEFAULT_STATE_DIR)
    return {
        "status": rooted(args.status_file or os.environ.get("NIGHTLY_FIX_STATUS_FILE")
                         or DEFAULT_STATUS_FILE),
        "register": rooted(args.register_file or os.environ.get("NIGHTLY_FIX_REGISTER_FILE")
                           or DEFAULT_REGISTER_FILE),
        "state_dir": state_dir,
        "clone_parent": rooted(args.clone_parent or os.environ.get("NIGHTLY_FIX_CLONE_PARENT")
                               or DEFAULT_CLONE_PARENT),
        "attempts": os.path.join(state_dir, "attempts.json"),
        "lock": os.path.join(state_dir, "LOCK"),
    }


def main(argv=None, runner=None, codex_runner=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    runner = runner or Runner()
    codex_runner = codex_runner or runner
    mode = args.mode
    lock = None

    try:
        now = parse_now(args.now)
        paths = resolve_paths(args)
        status = load_status(paths["status"])
    except MissingStatus as exc:
        log(str(exc))
        # Same class of silence as a stale status: the morning job never
        # produced its file, and without this push nobody learns until the
        # next session boots.
        notify.push("Nightly fixer standing down: no morning status",
                    "%s -- the 05:45 morning job may never have run. "
                    "Run tools/morning-pack.sh, then check its launchd lane."
                    % exc, priority="high")
        return 0
    except ConfigError as exc:
        log("configuration problem: %s" % exc)
        return 2

    if mode != "dry-run":
        try:
            os.makedirs(paths["state_dir"], exist_ok=True)
        except OSError as exc:
            log("cannot create %s: %s" % (paths["state_dir"], exc))
            return 2

    lock = Lock(paths["lock"], now)
    try:
        if mode == "dry-run":
            if lock.live_run_present():
                log("another run is live (lock at %s is under 6 hours old); doing nothing"
                    % paths["lock"])
                return 0
        else:
            if not lock.acquire():
                log("another run is live (lock at %s is under 6 hours old); doing nothing"
                    % paths["lock"])
                return 0
            assert_not_inside_worktree(paths["clone_parent"])
            removed = prune_clones(paths["clone_parent"], now)
            if removed:
                log("pruned %d clone dir(s) older than %d days: %s"
                    % (len(removed), CLONE_KEEP_DAYS, ", ".join(removed)))

        # The lane's whole picture of what is broken comes from the morning job.
        # The 02:30 run always uses the previous morning's snapshot by design,
        # but if the morning job stops the snapshot silently ages forever, and
        # the lane would keep acting on it. Standing down is the right answer:
        # nothing is broken, so the exit code stays 0.
        age = status_age_seconds(status, now)
        if age is not None and age > STATUS_MAX_AGE_S:
            log("the morning status file is %d hours old (limit %d); standing down"
                % (int(age // 3600), STATUS_MAX_AGE_S // 3600))
            log("outcome: status-stale -- re-run the morning pack, then this lane")
            # Standing down is right, but a %d-hour-old status means the 05:45
            # job stopped -- exactly the silent-death the audit found, so this
            # one stand-down does reach the phone.
            notify.push("Nightly fixer standing down: morning status stale",
                        "The status file is %d hours old (limit %d) -- the "
                        "05:45 morning job may be dead. Re-run "
                        "tools/morning-pack.sh, then check its launchd lane."
                        % (int(age // 3600), STATUS_MAX_AGE_S // 3600),
                        priority="high")
            return 0
        if age is None:
            log("the morning status file has no generated_at; treating it as fresh")

        approved_ids = {str(item_id) for item_id in status["nightly_eligible"]}
        merged = merge_register(status["items"], paths["register"])
        items = [entry for entry in merged if str(entry.get("id")) in approved_ids]
        attempts = read_attempts(paths["attempts"])

        # An item this lane already opened a pull request for stays blocked
        # until that pull request is merged or closed, or until the register
        # closes the item. The 72-hour backoff is not the right instrument for
        # this: it expires, and the lane would open a second pull request for
        # the same defect on night four, and a third on night seven.
        pr_blocked = {}
        for entry in items:
            branch = pending_pr_branch(attempts, str(entry.get("id")))
            if branch is None:
                continue
            state = pr_state(runner, entry.get("remote"), branch)
            if state in ("MERGED", "CLOSED"):
                log("%s: the pull request on %s is %s; the item is free again"
                    % (entry.get("id"), branch, state.lower()))
                continue
            pr_blocked[str(entry.get("id"))] = (branch, state)

        item, note = select_item(items, now, attempts, forced_id=args.item,
                                 pr_blocked=set(pr_blocked))

        if item is None:
            if pr_blocked:
                for blocked_id, (branch, state) in sorted(pr_blocked.items()):
                    log("%s: a pull request from an earlier run is still %s on %s"
                        % (blocked_id, state.lower(), branch or "an unrecorded branch"))
                log("outcome: pr-pending -- merge or close it and the item comes back")
                return 0
            if args.item and str(args.item) not in approved_ids and any(
                    str(entry.get("id")) == str(args.item) for entry in merged):
                note = ("%s is in the register but the status file does not list it as "
                        "nightly-eligible (open, owner codex, fix scope repo-pr, size S, "
                        "with a GitHub remote, not provisional or deferred)" % args.item)
            log("nothing to do: %s" % note)
            eligible_ids = status.get("nightly_eligible")
            if eligible_ids:
                log("the status file lists these as nightly-eligible: %s"
                    % ", ".join(str(i) for i in eligible_ids))
            return 0

        log("mode: %s" % mode)
        log("selected %s -- %s" % (item["id"], note))
        log("  system:  %s" % (item.get("system") or "unknown"))
        log("  repo:    %s" % (item.get("remote") or "none"))
        log("  symptom: %s" % " ".join((item.get("symptom") or "").split())[:160])
        log("  check:   %s" % (item.get("pr_check") or "none recorded"))

        if mode == "dry-run":
            stamp = date_stamp(now)
            assert_safe_item_id(item["id"])
            branch = "fix/%s-%s" % (item["id"].lower(), stamp)
            assert_safe_branch(branch)
            clone_dir = os.path.join(paths["clone_parent"], "%s-%s" % (item["id"], stamp))
            check = item.get("pr_check")
            if check:
                try:
                    assert_safe_pr_check(check, clone_dir)
                except (SafetyError, ConfigError) as exc:
                    log("dry run: the recorded pull-request check would be REFUSED: %s" % exc)
                    log("dry run: fix the check in registers/defects.json; nothing was written.")
                    return 0
            prompt_path = os.path.join(paths["state_dir"],
                                       "%s-%s.prompt.md" % (stamp, item["id"]))
            os.makedirs(paths["state_dir"], exist_ok=True)
            with open(prompt_path, "w", encoding="utf-8") as fh:
                fh.write(build_prompt(item, branch, clone_dir, now))
            log("dry run: wrote the prompt it would send to %s" % prompt_path)
            log("dry run: it would clone %s into %s on branch %s"
                % (item.get("remote"), clone_dir, branch))
            log("dry run: codex command would be: %s"
                % " ".join(codex_argv(clone_dir,
                                      os.path.join(paths["state_dir"],
                                                   "%s-%s.last.md" % (stamp, item["id"])),
                                      "<prompt>", codex_overrides())))
            if check:
                log("dry run: the check would run as: %s"
                    % " ".join(pr_check_argv(clone_dir, check)))
            log("dry run: nothing else was written, nothing was cloned, no pull request.")
            return 0

        return run_item(item, mode, now, paths, runner, codex_runner=codex_runner)

    except SafetyError as exc:
        log("SAFETY: %s" % exc)
        notify.push("Nightly fixer: safety stop",
                    "A safety rail refused to continue: %s" % exc,
                    priority="high")
        return 2
    except ConfigError as exc:
        log("configuration problem: %s" % exc)
        notify.push("Nightly fixer: configuration stop",
                    "The lane could not run: %s" % exc, priority="high")
        return 2
    except OSError as exc:
        log("configuration problem: filesystem operation failed: %s" % exc)
        notify.push("Nightly fixer: configuration stop",
                    "Filesystem operation failed: %s" % exc, priority="high")
        return 2
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
