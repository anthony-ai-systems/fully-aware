#!/usr/bin/env python3
"""Tests for nightly-fix.py -- stdlib unittest, no network, no codex, no gh.

Every external command (git, gh, codex, the pull-request check) is intercepted
by a fake runner, so running these tests never clones anything, never spends a
token, and never opens a pull request.

    uvx --with pytest python -m pytest -q tools/test_nightly_fix.py
    /usr/bin/python3 -m unittest tools.test_nightly_fix   (from the repo root)
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import plistlib
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_HERE, "nightly-fix.py")

_spec = importlib.util.spec_from_file_location("nightly_fix", _SCRIPT)
nf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nf)

NOW = "2026-08-27T02:30:00"


def at(text):
    return nf.parse_now(text)


def item(**over):
    base = {
        "id": "MAP-1",
        "severity": "P1",
        "system": "fully-aware automation map",
        "owner": "codex",
        "fix_scope": "repo-pr",
        "size": "S",
        "status": "open",
        "open_since": "2026-08-20",
        "days_open": 7,
        "symptom": "The automation map was last generated 08-20, so the brief lists dead lanes.",
        "fix_hint": "Regenerate the map inside morning-pack.sh before the assembler.",
        "remote": "anthony-ai-systems/fully-aware",
        "repo": "/Users/anthonyflores/code/fully-aware",
        "verify": "test -f /tmp/automation-map.json",
        "pr_check": "python -m pytest -q tools/test_generate_automation_map.py",
        "provisional": False,
        "not_before": None,
    }
    base.update(over)
    return base


def label(argv):
    exe = os.path.basename(str(argv[0]))
    rest = [str(a) for a in argv[1:]]
    if exe == "gh" and rest[:2] == ["repo", "clone"]:
        return "clone"
    if exe == "gh" and rest[:2] == ["pr", "create"]:
        return "pr-create"
    if exe == "gh" and rest[:2] == ["pr", "view"]:
        return "pr-view"
    if exe == "git":
        if "rev-parse" in rest:
            return "git-rev-parse"
        if "status" in rest:
            return "git-status-z" if "-z" in rest else "git-status"
        for verb in ("checkout", "add", "commit", "push"):
            if verb in rest:
                return "git-" + verb
        return "git-other"
    if exe.startswith("codex"):
        if "mcp" in rest:
            return "codex-mcp-list"
        if "sandbox" in rest:
            return "pr-check"
        return "codex"
    if exe == "env":
        return "check-warm"
    if exe.endswith("bash"):
        return "pr-check"
    return exe


# What `codex mcp list` prints once the isolation overrides have landed: every
# row disabled. Names only -- no command, no argument, no environment.
MCP_ALL_DISABLED = (
    "Name              Status    Auth       \n"
    "apify             disabled  Unsupported\n"
    "node_repl         disabled  Unsupported\n"
    "screenpipe        disabled  Unsupported\n"
    "\n"
    "Name                 Url                                Status    Auth       \n"
    "openaiDeveloperDocs  https://example.invalid/mcp        disabled  Unsupported\n"
)

MCP_ONE_ENABLED = (
    "Name              Status    Auth       \n"
    "apify             disabled  Unsupported\n"
    "node_repl         enabled   Unsupported\n"
    "screenpipe        disabled  Unsupported\n"
)


class FakeRunner(object):
    """Records every command and answers from a scripted table.

    Stateful in exactly one way: it remembers the branch the run asked for with
    ``git checkout -b`` and answers ``git rev-parse --abbrev-ref HEAD`` with it,
    so a fake clone behaves like a real one that stayed put. A test that wants
    the clone to have wandered scripts the label "git-rev-parse" explicitly.
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self.cwds = []
        self.branch = None

    def run(self, argv, cwd=None, timeout=None, stdin_path=None, log_path=None):
        argv = [str(a) for a in argv]
        name = label(argv)
        self.calls.append(argv)
        self.cwds.append(cwd)
        if name == "clone":
            os.makedirs(argv[4], exist_ok=True)
            with open(os.path.join(argv[4], "README.md"), "w") as fh:
                fh.write("clone\n")
        if name == "git-checkout" and "-b" in argv:
            self.branch = argv[-1]
        if name == "git-rev-parse" and name not in self.script:
            return nf.Result(0, "%s\n" % (self.branch or ""))
        if name == "codex-mcp-list" and name not in self.script:
            return nf.Result(0, MCP_ALL_DISABLED)
        if name == "pr-view" and name not in self.script:
            return nf.Result(1, "no pull requests found")
        rc, out = self.script.get(name, (0, ""))
        return nf.Result(rc, out)

    def labels(self):
        return [label(c) for c in self.calls]

    def cwd_of(self, name):
        """Every cwd the runner was handed for calls carrying this label."""
        return [cwd for cwd, argv in zip(self.cwds, self.calls)
                if label(argv) == name]


class Harness(unittest.TestCase):
    """A tempdir with a status file, a state dir, and a clone parent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nightly-fix-test-")
        self.state = os.path.join(self.tmp, "state")
        self.clones = os.path.join(self.tmp, "clones")
        os.makedirs(self.state)
        self.status_path = os.path.join(self.tmp, "defects-status.json")
        self.register_path = os.path.join(self.tmp, "defects.json")
        with open(self.register_path, "w") as fh:
            json.dump({"schema": "defect-register/v1", "items": []}, fh)
        self.write_status([item()])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_status(self, items, eligible_ids=None):
        with open(self.status_path, "w") as fh:
            json.dump({
                "schema": "defect-status/v1",
                "generated_at": "2026-08-27T05:45:00-07:00",
                "nightly_eligible": ([i["id"] for i in items]
                                     if eligible_ids is None else eligible_ids),
                "items": items,
            }, fh)

    def run_main(self, *extra, **kw):
        argv = [
            "--now", kw.pop("now", NOW),
            "--status-file", self.status_path,
            "--register-file", self.register_path,
            "--state-dir", self.state,
            "--clone-parent", self.clones,
        ] + list(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = nf.main(argv, runner=kw.pop("runner", None),
                         codex_runner=kw.pop("codex_runner", None))
        return rc, buf.getvalue()

    def state_files(self):
        return sorted(os.listdir(self.state))

    def attempts(self):
        return nf.read_attempts(os.path.join(self.state, "attempts.json"))


# ---------------------------------------------------------------- eligibility

class TestEligibility(unittest.TestCase):
    def reason(self, **over):
        return nf.ineligibility_reason(item(**over), at(NOW), [])

    def test_the_reference_item_is_eligible(self):
        self.assertIsNone(self.reason())

    def test_owner_must_be_codex(self):
        self.assertIn("owner", self.reason(owner="session"))

    def test_scope_must_be_repo_pr(self):
        self.assertIn("fix_scope", self.reason(fix_scope="local"))

    def test_size_must_be_small(self):
        self.assertIn("size", self.reason(size="M"))

    def test_provisional_items_are_never_nightly(self):
        self.assertIn("provisional", self.reason(provisional=True))

    def test_deferred_status_is_skipped(self):
        self.assertEqual("deferred", self.reason(status="deferred"))

    def test_closed_items_are_skipped(self):
        self.assertIn("not open", self.reason(status="fixed"))

    def test_not_before_in_the_future_defers(self):
        self.assertIn("deferred until", self.reason(not_before="2026-09-05"))

    def test_not_before_in_the_past_does_not_defer(self):
        self.assertIsNone(self.reason(not_before="2026-08-01"))

    def test_an_item_with_no_remote_cannot_be_a_pull_request(self):
        self.assertIn("no remote", self.reason(remote=None))

    def test_a_remote_must_look_like_org_slash_name(self):
        self.assertIn("org/name", self.reason(remote="fully-aware"))


class TestSelection(unittest.TestCase):
    def test_oldest_open_since_wins(self):
        items = [
            item(id="NEW-1", open_since="2026-08-25"),
            item(id="OLD-1", open_since="2026-08-02"),
            item(id="MID-1", open_since="2026-08-11"),
        ]
        chosen, note = nf.select_item(items, at(NOW), [])
        self.assertEqual("OLD-1", chosen["id"])
        self.assertIn("oldest", note)

    def test_ineligible_items_never_win_however_old(self):
        items = [
            item(id="ANCIENT", open_since="2020-01-01", owner="anthony"),
            item(id="MAP-1", open_since="2026-08-20"),
        ]
        chosen, _ = nf.select_item(items, at(NOW), [])
        self.assertEqual("MAP-1", chosen["id"])

    def test_nothing_eligible_returns_none(self):
        chosen, note = nf.select_item([item(size="L")], at(NOW), [])
        self.assertIsNone(chosen)
        self.assertIn("no eligible item", note)

    def test_an_attempt_inside_72h_blocks_the_item(self):
        attempts = [{"id": "MAP-1", "at": "2026-08-26T02:30:00"}]
        chosen, note = nf.select_item([item()], at(NOW), attempts)
        self.assertIsNone(chosen)
        self.assertIn("no eligible", note)
        self.assertIn("backoff", nf.ineligibility_reason(item(), at(NOW), attempts))

    def test_an_attempt_older_than_72h_lets_it_through(self):
        attempts = [{"id": "MAP-1", "at": "2026-08-22T02:30:00"}]
        chosen, _ = nf.select_item([item()], at(NOW), attempts)
        self.assertEqual("MAP-1", chosen["id"])

    def test_forcing_an_item_does_not_override_the_backoff(self):
        attempts = [{"id": "MAP-1", "at": "2026-08-27T00:30:00"}]
        chosen, note = nf.select_item([item()], at(NOW), attempts, forced_id="MAP-1")
        self.assertIsNone(chosen)
        self.assertIn("cannot be forced", note)
        self.assertIn("backoff", note)

    def test_forcing_only_selects_an_eligible_item(self):
        chosen, note = nf.select_item([item()], at(NOW), [], forced_id="MAP-1")
        self.assertEqual("MAP-1", chosen["id"])
        self.assertEqual("forced", note)

    def test_forcing_does_not_override_owner_status_size_or_defer_rails(self):
        for candidate in (item(owner="anthony"), item(status="fixed"),
                          item(size="M"), item(not_before="2026-09-05")):
            chosen, note = nf.select_item([candidate], at(NOW), [], forced_id="MAP-1")
            self.assertIsNone(chosen)
            self.assertIn("cannot be forced", note)

    def test_forcing_an_item_with_no_remote_is_refused(self):
        chosen, note = nf.select_item([item(remote=None)], at(NOW), [], forced_id="MAP-1")
        self.assertIsNone(chosen)
        self.assertIn("cannot be forced", note)

    def test_register_fields_fill_gaps_in_the_status_file(self):
        thin = [{"id": "MAP-1", "status": "open", "owner": "codex",
                 "fix_scope": "repo-pr", "size": "S", "open_since": "2026-08-20"}]
        reg = os.path.join(tempfile.mkdtemp(prefix="nf-reg-"), "defects.json")
        with open(reg, "w") as fh:
            json.dump({"items": [{"id": "MAP-1", "remote": "org/repo",
                                  "verify": "true", "pr_check": "true",
                                  "provisional": False}]}, fh)
        merged = nf.merge_register(thin, reg)
        self.assertEqual("org/repo", merged[0]["remote"])
        self.assertIsNone(nf.ineligibility_reason(merged[0], at(NOW), []))


# ---------------------------------------------------------------- safety rails

class TestSafety(unittest.TestCase):
    def test_a_default_branch_is_refused(self):
        for name in ("main", "master", "Main", "refs/heads/main", "trunk"):
            with self.assertRaises(nf.SafetyError):
                nf.assert_safe_branch(name)

    def test_our_own_branch_name_is_fine(self):
        self.assertEqual("fix/map-1-20260827",
                         nf.assert_safe_branch("fix/map-1-20260827"))

    def test_a_forced_push_is_refused(self):
        for bad in (["git", "push", "--force", "origin", "b"],
                    ["git", "push", "-f", "origin", "b"],
                    ["git", "push", "--force-with-lease"],
                    ["git", "push", "origin", "+b:b"]):
            with self.assertRaises(nf.SafetyError):
                nf.assert_safe_argv(bad)

    def test_a_plain_push_is_allowed(self):
        nf.assert_safe_argv(["git", "-C", "/tmp/x", "push", "-u", "origin", "fix/a-1"])

    def test_merging_is_refused(self):
        with self.assertRaises(nf.SafetyError):
            nf.assert_safe_argv(["gh", "pr", "merge", "12", "--squash"])
        for verb in ("merge", "rebase", "reset", "pull", "clean", "stash"):
            with self.assertRaises(nf.SafetyError):
                nf.assert_safe_argv(["git", "-C", "/tmp/x", verb])

    def test_creating_our_branch_is_allowed_but_switching_is_not(self):
        nf.assert_safe_argv(["git", "-C", "/tmp/x", "checkout", "-b", "fix/a-1"])
        with self.assertRaises(nf.SafetyError):
            nf.assert_safe_argv(["git", "-C", "/tmp/x", "checkout", "main"])

    def test_a_clone_parent_inside_a_work_tree_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="nf-worktree-")
        try:
            os.makedirs(os.path.join(tmp, ".git"))
            inner = os.path.join(tmp, "clones")
            with self.assertRaises(nf.SafetyError):
                nf.assert_not_inside_worktree(inner)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_symlink_into_a_work_tree_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="nf-worktree-link-")
        try:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, ".git"))
            link = os.path.join(tmp, "linked")
            os.symlink(repo, link)
            with self.assertRaises(nf.SafetyError):
                nf.assert_not_inside_worktree(os.path.join(link, "clones"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_item_id_cannot_escape_its_output_directory(self):
        for bad in ("../MAP-1", "/tmp/MAP-1", "MAP/1", "-MAP-1", "MAP 1"):
            with self.assertRaises(nf.SafetyError):
                nf.assert_safe_item_id(bad)


# ------------------------------------------------- the pull-request check rail

CLONE = "/Users/anthonyflores/code/.nightly-fix/MAP-1-20260827"

# MAP-1's real recorded pr_check. It must keep passing: a rail that refuses the
# register's own check silently parks every nightly item forever.
MAP_1_CHECK = (
    "bash -n tools/morning-pack.sh && "
    "grep -qE '^[^#]*generate-automation-map\\.py' tools/morning-pack.sh && "
    "uvx --with pytest python -m pytest -q tools/ 2>&1 | tail -2"
)


class TestSafetyRails(unittest.TestCase):
    def test_a_check_cannot_target_existing_checkouts_force_push_or_merge(self):
        for bad in (
                # walking up out of the clone, quoted or not
                'cd "../../fully-aware" && git log',
                "cd ./../../fully-aware && git log",
                "cd ../other",
                # naming a home directory, however it is spelled
                "cd $HOME/code && ls",
                "cd ~/code && ls",
                'cd "$HOME/code" && ls',
                "git -C ~ status",
                "touch /Users/anthonyflores/code/fully-aware/file",
                # pushing, forcing, or touching pull requests at all
                "git push origin -f HEAD:main",
                "git push origin HEAD",
                "git push --force origin branch",
                "gh pr merge 1",
                "gh pr create",
                "$(gh api x)",
                # rerouting git at another tree
                "GIT_DIR=/tmp/x git status",
                "git --work-tree=/tmp status",
                # reaching the Mac itself
                "sudo ls",
                "launchctl list",
        ):
            with self.assertRaises(nf.SafetyError, msg=bad):
                nf.assert_safe_pr_check(bad, CLONE)

    def test_the_checks_the_register_really_records_are_accepted(self):
        for good in (MAP_1_CHECK,
                     "python -m pytest -q",
                     "true",
                     "echo {1..5}",                       # ".." inside a range
                     "test -d %s/tools" % CLONE):         # the clone's own path
            self.assertEqual(good, nf.assert_safe_pr_check(good, CLONE),
                             msg=good)

    def test_a_check_that_is_not_a_command_is_a_configuration_error(self):
        for bad in ("", "   ", None, 5, ["true"], "true\x00; rm -rf /"):
            with self.assertRaises(nf.ConfigError, msg=repr(bad)):
                nf.assert_safe_pr_check(bad, CLONE)


class TestChildEnvironment(unittest.TestCase):
    """An empty PATH element means "the current directory" to the shell."""

    def test_an_empty_inherited_path_leaves_no_empty_element(self):
        parts = nf._prefixed_path("").split(":")
        self.assertNotIn("", parts)
        self.assertFalse(nf._prefixed_path("").endswith(":"))
        self.assertTrue(nf._prefixed_path("").startswith("/opt/homebrew/bin:"))

    def test_empty_elements_anywhere_in_the_inherited_path_are_dropped(self):
        parts = nf._prefixed_path("a::b:").split(":")
        self.assertNotIn("", parts)
        self.assertIn("a", parts)
        self.assertIn("b", parts)
        self.assertEqual(parts.index("a") + 1, parts.index("b"))

    def test_the_child_environment_never_hands_a_child_the_cwd(self):
        self.assertNotIn("", nf.child_env()["PATH"].split(":"))


class TestCodexCommand(unittest.TestCase):
    def test_the_command_line_is_the_agreed_one(self):
        argv = nf.codex_argv("/tmp/clone", "/tmp/last.md", "the prompt")
        self.assertEqual([
            nf.codex_binary(), "exec", "-C", "/tmp/clone",
            "-s", "workspace-write", "-o", "/tmp/last.md", "the prompt",
        ], argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("--model", argv)
        self.assertFalse(any("gpt-5.6" in arg for arg in argv))


# ---------------------------------------------------------------- prompt

class TestPrompt(unittest.TestCase):
    def test_the_prompt_carries_the_defect_and_both_commands(self):
        text = nf.build_prompt(item(), "fix/map-1-20260827", "/tmp/clone", at(NOW))
        self.assertIn("MAP-1", text)
        self.assertIn("automation map", text)
        self.assertIn("test -f /tmp/automation-map.json", text)       # verify
        self.assertIn("test_generate_automation_map.py", text)        # pr_check
        self.assertIn("only after your pull request is merged", text)
        self.assertIn("must exit 0", text)
        self.assertIn("smallest correct diff", text)
        self.assertIn("Add no new dependencies", text)
        self.assertIn("CI configuration", text)
        self.assertIn("do not merge anything", text)
        self.assertIn("update an existing test", text)
        self.assertIn(nf.DONE_MARKER, text)
        self.assertIn("three-line summary", text)

    def test_an_item_without_a_check_says_so(self):
        text = nf.build_prompt(item(pr_check=None), "fix/x-1", "/tmp/c", at(NOW))
        self.assertIn("records no pull-request check", text)


# ---------------------------------------------------------------- dry run

class TestDryRun(Harness):
    def test_dry_run_writes_the_prompt_and_nothing_else(self):
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertEqual(["20260827-MAP-1.prompt.md"], self.state_files())
        self.assertIn("selected MAP-1", out)
        self.assertFalse(os.path.exists(self.clones))

    def test_dry_run_prints_the_codex_command_it_would_run(self):
        _, out = self.run_main()
        self.assertIn("workspace-write", out)
        self.assertIn("20260827-MAP-1.last.md", out)
        command_line = [line for line in out.splitlines()
                        if "codex command would be:" in line][0]
        self.assertNotIn(" -m ", command_line)

    def test_dry_run_never_calls_a_command(self):
        runner = FakeRunner()
        self.run_main(runner=runner)
        self.assertEqual([], runner.calls)

    def test_nothing_eligible_writes_nothing(self):
        self.write_status([item(owner="anthony")])
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("nothing to do", out)
        self.assertEqual([], self.state_files())

    def test_nothing_eligible_does_not_even_create_the_state_directory(self):
        self.write_status([item(owner="anthony")])
        absent = os.path.join(self.tmp, "absent-state")
        argv = ["--now", NOW, "--status-file", self.status_path,
                "--register-file", self.register_path, "--state-dir", absent,
                "--clone-parent", self.clones]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = nf.main(argv, runner=FakeRunner())
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(absent))

    def test_status_allowlist_is_fail_closed(self):
        self.write_status([item()], eligible_ids=[])
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("nothing to do", out)
        self.assertEqual([], self.state_files())

    def test_a_missing_status_file_is_reported_as_a_handled_outcome(self):
        os.remove(self.status_path)
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("morning register job has not run", out)
        self.assertEqual([], self.state_files())

    def test_a_refused_check_stops_the_dry_run_before_the_prompt(self):
        self.write_status([item(pr_check="cd ../other && pytest")])
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("would be REFUSED", out)
        self.assertIn("registers/defects.json", out)
        self.assertEqual([], self.state_files())
        self.assertFalse(os.path.exists(self.clones))

    def test_forcing_an_item_the_status_file_does_not_approve_says_why(self):
        self.write_status([item()], eligible_ids=[])
        rc, out = self.run_main("--item", "MAP-1")
        self.assertEqual(0, rc)
        self.assertIn("MAP-1 is in the register but the status file does not "
                      "list it as nightly-eligible", out)
        self.assertEqual([], self.state_files())


class TestLock(Harness):
    def write_lock(self, when):
        with open(os.path.join(self.state, "LOCK"), "w") as fh:
            json.dump({"pid": 1, "at": when}, fh)

    def test_a_fresh_lock_stops_the_run(self):
        self.write_lock("2026-08-27T01:00:00")
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("another run is live", out)
        self.assertNotIn("selected", out)

    def test_a_lock_older_than_six_hours_is_ignored(self):
        self.write_lock("2026-08-26T12:00:00")
        rc, out = self.run_main()
        self.assertEqual(0, rc)
        self.assertIn("selected MAP-1", out)

    def test_the_lock_is_released_after_a_real_run(self):
        runner = FakeRunner({"git-status": (0, "")})
        rc, _ = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        self.assertNotIn("LOCK", self.state_files())
        # The flock guard is a permanent sibling of the lock, never the lock
        # itself: deleting it would reopen the take-over race it closes.
        self.assertIn("LOCK.guard", self.state_files())

    def test_lock_acquisition_is_atomic_and_only_the_owner_releases_it(self):
        path = os.path.join(self.state, "LOCK")
        first = nf.Lock(path, at(NOW))
        second = nf.Lock(path, at(NOW))
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        second.release()
        self.assertTrue(os.path.exists(path))
        first.release()
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".guard"))

    def test_release_leaves_a_lock_another_pid_wrote_in_place(self):
        path = os.path.join(self.state, "LOCK")
        lock = nf.Lock(path, at(NOW))
        self.assertTrue(lock.acquire())
        # Someone else's run replaced the file while we held ours: releasing
        # must not delete a lock that is now somebody else's.
        with open(path, "w") as fh:
            json.dump({"pid": os.getpid() + 99999, "at": NOW}, fh)
        lock.release()
        self.assertTrue(os.path.exists(path))
        with open(path) as fh:
            self.assertEqual(os.getpid() + 99999, json.load(fh)["pid"])

    def test_only_one_of_two_runs_takes_over_a_stale_lock(self):
        path = os.path.join(self.state, "LOCK")
        self.write_lock("2026-08-26T12:00:00")        # more than six hours old
        first = nf.Lock(path, at(NOW))
        second = nf.Lock(path, at(NOW))
        self.assertTrue(first.acquire())
        with open(path) as fh:
            self.assertEqual(os.getpid(), json.load(fh)["pid"])
        self.assertFalse(second.acquire())            # first's lock is now live
        first.release()
        self.assertFalse(os.path.exists(path))


# ---------------------------------------------------------------- trial / live

class TestTrial(Harness):
    def test_no_changes_stops_before_the_commit(self):
        runner = FakeRunner({"git-status": (0, "")})
        rc, out = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        self.assertIn("no changes", out)
        self.assertNotIn("git-commit", runner.labels())
        self.assertNotIn("git-push", runner.labels())
        self.assertEqual("no-changes", self.attempts()[-1]["outcome"])
        self.assertIn("20260827-MAP-1.md", self.state_files())

    def test_a_failing_check_keeps_the_clone_and_opens_nothing(self):
        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh"),
                             "pr-check": (1, "1 failed")})
        rc, out = self.run_main("--trial", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["pr-check-failed"], rc)
        self.assertNotIn("git-commit", runner.labels())
        self.assertNotIn("pr-create", runner.labels())
        self.assertEqual("pr-check-failed", self.attempts()[-1]["outcome"])
        self.assertTrue(os.path.isdir(os.path.join(self.clones, "MAP-1-20260827")))

    def test_trial_commits_but_never_pushes(self):
        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh")})
        rc, _ = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        self.assertIn("git-commit", runner.labels())
        self.assertNotIn("git-push", runner.labels())
        self.assertNotIn("pr-create", runner.labels())
        self.assertEqual("committed-locally", self.attempts()[-1]["outcome"])
        self.assertTrue(os.path.isdir(os.path.join(self.clones, "MAP-1-20260827")))

    def test_a_refused_check_is_handled_before_anything_is_cloned(self):
        self.write_status([item(pr_check="cd ../other && pytest")])
        runner = FakeRunner()
        rc, out = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        self.assertEqual([], runner.calls)               # nothing was cloned
        self.assertFalse(os.path.exists(self.clones))
        self.assertIn("20260827-MAP-1.md", self.state_files())
        self.assertEqual(1, len(self.attempts()))
        self.assertEqual("pr-check-refused", self.attempts()[-1]["outcome"])
        with open(os.path.join(self.state, "20260827-MAP-1.md")) as fh:
            body = fh.read()
        self.assertIn("refused before anything was cloned", body)

    def test_a_check_refused_after_the_clone_is_handled_not_crashed(self):
        """The post-clone re-check must not escape run_item as an exit-2 crash.

        The second ``assert_safe_pr_check`` resolves the now-existing clone
        path, so it can in principle refuse what the pre-clone call accepted.
        Reaching that divergence for real needs the resolved path to change
        mid-run, so the second call is forced to raise here; what is under test
        is the handling -- run log, attempt record, exit 0, nothing pushed.
        """
        real = nf.assert_safe_pr_check
        calls = []

        def refuse_the_second_time(command, clone_dir):
            calls.append(clone_dir)
            if len(calls) == 1:
                return real(command, clone_dir)
            raise nf.SafetyError("refusing a pull-request check that names a "
                                 "path outside the clone")

        nf.assert_safe_pr_check = refuse_the_second_time
        self.addCleanup(setattr, nf, "assert_safe_pr_check", real)

        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh")})
        rc, _ = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        self.assertEqual(2, len(calls))
        labels = runner.labels()
        self.assertIn("clone", labels)                   # it got past the clone
        for forbidden in ("pr-check", "git-add", "git-commit", "git-push",
                          "pr-create"):
            self.assertNotIn(forbidden, labels)
        self.assertEqual("pr-check-refused", self.attempts()[-1]["outcome"])
        self.assertIn("refused after cloning", self.attempts()[-1]["detail"])
        with open(os.path.join(self.state, "20260827-MAP-1.md")) as fh:
            body = fh.read()
        self.assertIn("refused after the clone was made", body)
        self.assertTrue(os.path.isdir(os.path.join(self.clones, "MAP-1-20260827")))

    def test_a_clone_left_on_another_branch_is_never_committed_or_pushed(self):
        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh"),
                             "git-rev-parse": (0, "main\n")})
        rc, out = self.run_main("--trial", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["wrong-branch"], rc)
        labels = runner.labels()
        self.assertIn("git-rev-parse", labels)
        for forbidden in ("pr-check", "git-add", "git-commit", "git-push",
                          "pr-create"):
            self.assertNotIn(forbidden, labels)
        self.assertEqual("wrong-branch", self.attempts()[-1]["outcome"])
        self.assertTrue(os.path.isdir(os.path.join(self.clones, "MAP-1-20260827")))

    def test_a_rev_parse_that_fails_is_treated_as_the_wrong_branch(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-rev-parse": (128, "fatal: not a git repository\n")})
        rc, _ = self.run_main("--trial", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["wrong-branch"], rc)
        self.assertNotIn("git-commit", runner.labels())
        self.assertEqual("wrong-branch", self.attempts()[-1]["outcome"])

    def test_the_pull_request_check_runs_inside_the_clone(self):
        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh")})
        rc, _ = self.run_main("--trial", runner=runner)
        self.assertEqual(0, rc)
        clone = os.path.join(self.clones, "MAP-1-20260827")
        self.assertEqual([clone], runner.cwd_of("pr-check"))
        self.assertEqual([clone], runner.cwd_of("codex"))

    def test_the_branch_is_never_a_default_branch(self):
        runner = FakeRunner({"git-status": (0, "")})
        self.run_main("--trial", runner=runner)
        checkout = [c for c in runner.calls if label(c) == "git-checkout"][0]
        self.assertEqual("fix/map-1-20260827", checkout[-1])

    def test_codex_and_git_gh_runners_are_independently_injectable(self):
        commands = FakeRunner({"git-status": (0, "")})
        codex = FakeRunner()
        rc, _ = self.run_main("--trial", runner=commands, codex_runner=codex)
        self.assertEqual(0, rc)
        self.assertEqual(["codex"], codex.labels())
        self.assertNotIn("codex", commands.labels())
        argv = codex.calls[0]
        self.assertEqual(1, argv.count("-o"))
        self.assertTrue(argv[argv.index("-o") + 1].endswith("20260827-MAP-1.last.md"))
        self.assertIn(nf.DONE_MARKER, argv[-1])
        self.assertNotIn("-m", argv)
        self.assertNotIn("--model", argv)


class TestLive(Harness):
    def test_a_clean_run_opens_one_pull_request_and_deletes_the_clone(self):
        runner = FakeRunner({
            "git-status": (0, " M tools/morning-pack.sh"),
            "pr-create": (0, "https://github.com/anthony-ai-systems/fully-aware/pull/42\n"),
        })
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(0, rc)
        labels = runner.labels()
        self.assertEqual(["clone", "git-checkout", "codex-mcp-list", "codex",
                          "git-status", "git-rev-parse", "pr-check",
                          "git-status-z", "git-add", "git-commit",
                          "git-push", "pr-create"], labels)
        self.assertEqual(1, labels.count("git-push"))
        self.assertEqual(1, labels.count("pr-create"))
        self.assertFalse(any(c[1:3] == ["pr", "merge"] for c in runner.calls))
        self.assertIn("pull/42", out)
        self.assertEqual("pr-opened", self.attempts()[-1]["outcome"])
        self.assertFalse(os.path.isdir(os.path.join(self.clones, "MAP-1-20260827")))
        with open(os.path.join(self.state, "20260827-MAP-1.md")) as fh:
            body = fh.read()
        self.assertIn("pull/42", body)
        self.assertIn("Merge is Anthony's", body)

    def test_no_command_ever_carries_force_or_merge(self):
        runner = FakeRunner({
            "git-status": (0, " M f"),
            "pr-create": (0, "https://example.invalid/pr/1\n"),
        })
        self.run_main("--live", runner=runner)
        for call in runner.calls:
            self.assertNotIn("--force", call)
            self.assertNotIn("-f", call)
            self.assertFalse(any(call[i:i + 2] == ["pr", "merge"]
                                 for i in range(len(call) - 1)))

    def test_the_commit_subject_names_the_defect(self):
        subject = nf.commit_message(item())
        self.assertTrue(subject.startswith("fix(MAP-1): "))
        self.assertLessEqual(len(subject) - len("fix(MAP-1): "), 70)
        body = nf.commit_body(item(), "passed")
        self.assertIn("merge is Anthony's", body)
        self.assertIn("Verify", body)

    def test_a_failing_codex_records_an_attempt_and_exits_non_zero(self):
        runner = FakeRunner({"codex": (1, "model error")})
        rc, _ = self.run_main("--live", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["codex-failed"], rc)
        self.assertEqual("codex-failed", self.attempts()[-1]["outcome"])
        self.assertNotIn("git-push", runner.labels())

    def test_a_refused_check_never_reaches_a_pull_request(self):
        self.write_status([item(pr_check="cd ../other && pytest")])
        runner = FakeRunner()
        rc, _ = self.run_main("--live", runner=runner)
        self.assertEqual(0, rc)
        self.assertEqual([], runner.calls)
        self.assertFalse(os.path.exists(self.clones))
        self.assertEqual("pr-check-refused", self.attempts()[-1]["outcome"])
        self.assertIn("20260827-MAP-1.md", self.state_files())

    def test_a_clone_parent_inside_a_repo_exits_two(self):
        os.makedirs(os.path.join(self.tmp, ".git"))
        runner = FakeRunner({"git-status": (0, "")})
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(2, rc)
        self.assertIn("SAFETY", out)


class TestHousekeeping(Harness):
    def test_old_clone_dirs_are_pruned_and_recent_ones_kept(self):
        os.makedirs(self.clones)
        old = os.path.join(self.clones, "OLD-1-20260801")
        recent = os.path.join(self.clones, "NEW-1-20260826")
        stranger = os.path.join(self.clones, "someone-elses-work")
        for path in (old, recent, stranger):
            os.makedirs(path)
        ancient = (at(NOW) - datetime.timedelta(days=20)).timestamp()
        os.utime(old, (ancient, ancient))
        os.utime(stranger, (ancient, ancient))
        removed = nf.prune_clones(self.clones, at(NOW))
        self.assertEqual(["OLD-1-20260801"], removed)
        self.assertTrue(os.path.isdir(recent))
        self.assertTrue(os.path.isdir(stranger))

    def test_attempts_stay_valid_json_with_one_line_per_record(self):
        path = os.path.join(self.state, "attempts.json")
        nf.record_attempt(path, "MAP-1", at(NOW), "live", "pr-opened", "url")
        nf.record_attempt(path, "MAP-2", at(NOW), "trial", "no-changes", "")
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(2, len(data["attempts"]))
        self.assertEqual("MAP-2", data["attempts"][-1]["id"])


class TestSupportFiles(unittest.TestCase):
    def read(self, relative):
        with open(os.path.join(_ROOT, relative), encoding="utf-8") as fh:
            return fh.read()

    def test_wrapper_uses_the_frozen_path_and_relative_executor(self):
        text = self.read("tools/nightly-fix.sh")
        self.assertIn('export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"', text)
        self.assertIn('cd "${REPO_ROOT}"', text)
        self.assertIn('exec /usr/bin/python3 tools/nightly-fix.py --live "$@"', text)

    def test_plist_is_strict_xml_and_has_the_frozen_schedule(self):
        path = os.path.join(_ROOT, "launchd", "com.anthonyflores.fully-aware.nightly-fix.plist")
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
        self.assertEqual("com.anthonyflores.fully-aware.nightly-fix", data["Label"])
        self.assertEqual({"Hour": 2, "Minute": 30}, data["StartCalendarInterval"])
        self.assertFalse(data["RunAtLoad"])
        self.assertEqual("/Users/anthonyflores/code/fully-aware", data["WorkingDirectory"])

    def test_installer_never_uses_the_forbidden_state_dump(self):
        text = self.read("tools/install-nightly-fix.sh")
        self.assertNotIn("launchctl print", text)
        self.assertIn('MODE="dry-run"', text)
        self.assertIn('launchctl bootout "${DOMAIN}/${LABEL}"', text)
        self.assertIn('launchctl bootstrap "${DOMAIN}" "${DEST}"', text)

    def test_documentation_is_plain_and_short(self):
        # The cap was 50 until 2026-08-28, when the review found the doc was
        # describing a sandbox that did not exist. Saying what Codex can and
        # cannot reach, honestly, costs six lines. The cap is about keeping the
        # doc plain, not about keeping a false claim short.
        text = self.read("docs/NIGHTLY-FIX.md")
        self.assertLessEqual(len(text.splitlines()), 60)
        for phrase in ("What it does each night", "What it never does",
                       "How an item becomes eligible", "How to arm it after merge",
                       "How to read the morning log", "fast-forwards or syncs",
                       "What Codex is allowed to reach"):
            self.assertIn(phrase, text)



# ------------------------------------------------------------ codex isolation

# A config shaped like the real one: quoted names, unquoted names, and dotted
# sub-tables that must collapse to their parent. No values -- names only.
CONFIG_FIXTURE = """
approval_policy = "never"
sandbox_mode = "danger-full-access"

[mcp_servers.node_repl]
command = "node"

[mcp_servers.node_repl.env]
NODE_PATH = "somewhere"

[mcp_servers."firecrawl-mcp"]
command = "npx"

[mcp_servers."firecrawl-mcp".tools.firecrawl_scrape]
enabled = true

[mcp_servers.screenpipe]
command = "bun"

[plugins."github@openai-curated"]
enabled = true

[plugins."computer-use@openai-bundled"]
enabled = true

[plugins.simple]
enabled = true

[projects."/Users/someone/code"]
trust_level = "trusted"
"""


class TestCodexIsolationArgv(unittest.TestCase):
    def test_every_server_and_plugin_name_is_found_once(self):
        servers, plugins = nf.codex_tool_names(CONFIG_FIXTURE)
        self.assertEqual(["firecrawl-mcp", "node_repl", "screenpipe"], servers)
        self.assertEqual(["computer-use@openai-bundled", "github@openai-curated",
                          "simple"], plugins)

    def test_other_sections_are_not_mistaken_for_tools(self):
        servers, plugins = nf.codex_tool_names(CONFIG_FIXTURE)
        self.assertNotIn("/Users/someone/code", servers + plugins)

    def test_an_empty_or_missing_config_yields_nothing(self):
        self.assertEqual(([], []), nf.codex_tool_names(""))
        self.assertEqual(([], []), nf.codex_tool_names(None))

    def test_names_are_quoted_only_when_toml_requires_it(self):
        servers, plugins = nf.codex_tool_names(CONFIG_FIXTURE)
        argv = nf.codex_isolation_argv(servers, plugins)
        # hyphens and underscores are legal bare TOML keys; "@" is not
        self.assertIn("mcp_servers.firecrawl-mcp.enabled=false", argv)
        self.assertIn("mcp_servers.node_repl.enabled=false", argv)
        self.assertIn('plugins."github@openai-curated".enabled=false', argv)
        self.assertIn("plugins.simple.enabled=false", argv)
        self.assertNotIn('plugins."simple".enabled=false', argv)

    def test_every_named_tool_gets_its_own_override(self):
        servers, plugins = nf.codex_tool_names(CONFIG_FIXTURE)
        argv = nf.codex_isolation_argv(servers, plugins)
        for name in servers:
            self.assertTrue(any(a.startswith("mcp_servers.") and name in a for a in argv),
                            "no override for server %s" % name)
        for name in plugins:
            self.assertTrue(any(a.startswith("plugins.") and name in a for a in argv),
                            "no override for plugin %s" % name)

    def test_the_features_and_approval_policy_are_switched_off(self):
        argv = nf.codex_isolation_argv([], [])
        for feature in ("plugins", "apps", "browser_use", "browser_use_external",
                        "browser_use_full_cdp_access", "computer_use",
                        "code_mode_host", "memories", "chronicle"):
            self.assertEqual(["--disable", feature],
                             argv[argv.index(feature) - 1:argv.index(feature) + 1])
        self.assertIn("approval_policy=never", argv)

    def test_the_codex_command_carries_the_overrides_and_no_model(self):
        overrides = nf.codex_isolation_argv(*nf.codex_tool_names(CONFIG_FIXTURE))
        argv = nf.codex_argv("/tmp/clone", "/tmp/last.md", "the prompt", overrides)
        self.assertEqual([nf.codex_binary(), "exec"], argv[:2])
        self.assertEqual(["-C", "/tmp/clone", "-s", "workspace-write",
                          "-o", "/tmp/last.md", "the prompt"], argv[-7:])
        for override in overrides:
            self.assertIn(override, argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("--model", argv)

    def test_the_mcp_list_command_uses_the_same_overrides(self):
        overrides = nf.codex_isolation_argv(["a"], ["b"])
        argv = nf.codex_mcp_list_argv(overrides)
        self.assertEqual([nf.codex_binary()], argv[:1])
        self.assertEqual(["mcp", "list"], argv[-2:])
        for override in overrides:
            self.assertIn(override, argv)


class TestMcpListParsing(unittest.TestCase):
    def test_an_all_disabled_table_leaves_nothing_live(self):
        rows = nf.parse_mcp_list(MCP_ALL_DISABLED)
        self.assertEqual(4, len(rows))
        self.assertEqual(["disabled"] * 4, [state for _, state in rows])
        self.assertEqual([], nf.mcp_rows_still_live(MCP_ALL_DISABLED))

    def test_one_enabled_row_is_reported_by_name(self):
        self.assertEqual(["node_repl"], nf.mcp_rows_still_live(MCP_ONE_ENABLED))

    def test_both_tables_are_read_and_headers_skipped(self):
        names = [name for name, _ in nf.parse_mcp_list(MCP_ALL_DISABLED)]
        self.assertIn("node_repl", names)
        self.assertIn("openaiDeveloperDocs", names)
        self.assertNotIn("Name", names)

    def test_a_status_it_cannot_read_counts_as_live(self):
        text = "Name    Status\nweird   somethingelse\n"
        self.assertEqual([("weird", "unknown")], nf.parse_mcp_list(text))
        self.assertEqual(["weird"], nf.mcp_rows_still_live(text))

    def test_empty_output_has_no_rows(self):
        self.assertEqual([], nf.parse_mcp_list(""))


class TestIsolationIsVerifiedBeforeCodexRuns(Harness):
    def test_a_still_enabled_server_stops_the_run_before_codex(self):
        runner = FakeRunner({"codex-mcp-list": (0, MCP_ONE_ENABLED)})
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["codex-isolation-failed"], rc)
        self.assertIn("codex-mcp-list", runner.labels())
        self.assertNotIn("codex", runner.labels())
        self.assertEqual("codex-isolation-failed", self.attempts()[-1]["outcome"])
        self.assertIn("node_repl", self.attempts()[-1]["detail"])

    def test_an_mcp_list_that_fails_stops_the_run(self):
        runner = FakeRunner({"codex-mcp-list": (127, "command not found")})
        rc, _ = self.run_main("--live", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["codex-isolation-failed"], rc)
        self.assertNotIn("codex", runner.labels())

    def test_an_empty_table_is_not_taken_as_proof(self):
        runner = FakeRunner({"codex-mcp-list": (0, "")})
        rc, _ = self.run_main("--live", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["codex-isolation-failed"], rc)
        self.assertNotIn("codex", runner.labels())

    def test_the_log_records_names_and_statuses_only(self):
        runner = FakeRunner({"git-status": (0, " M tools/morning-pack.sh")})
        self.run_main("--trial", runner=runner)
        log = [f for f in self.state_files() if f.endswith(".codex.log")]
        self.assertEqual(1, len(log))
        with open(os.path.join(self.state, log[0]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("node_repl disabled", text)
        self.assertIn("<isolation overrides>", text)
        self.assertNotIn("Unsupported", text)          # the Auth column
        self.assertNotIn("example.invalid", text)      # the Url column


# ------------------------------------------------------------------ exit codes

class TestExitCodes(Harness):
    def test_every_failure_code_is_distinct_and_non_zero(self):
        codes = list(nf.OUTCOME_EXIT_CODES.values())
        self.assertEqual(len(codes), len(set(codes)))
        for outcome, code in nf.OUTCOME_EXIT_CODES.items():
            with self.subTest(outcome=outcome):
                self.assertGreater(code, 2, "2 is reserved for safety/config")

    def test_each_failure_outcome_returns_its_own_code(self):
        cases = [
            ("clone-failed", {"clone": (1, "denied")}, "--live"),
            ("branch-failed", {"git-checkout": (1, "nope")}, "--live"),
            ("codex-isolation-failed", {"codex-mcp-list": (0, MCP_ONE_ENABLED)}, "--live"),
            ("codex-failed", {"codex": (1, "model error")}, "--live"),
            ("git-status-failed", {"git-status": (1, "boom")}, "--live"),
            ("wrong-branch", {"git-status": (0, " M f"),
                              "git-rev-parse": (0, "main\n")}, "--live"),
            ("unsafe-diff", {"git-status": (0, " M f"),
                             "git-status-z": (0, "?? secrets.env\0")}, "--live"),
            ("pr-check-failed", {"git-status": (0, " M f"),
                                 "pr-check": (1, "1 failed")}, "--live"),
            ("commit-failed", {"git-status": (0, " M f"),
                               "git-status-z": (0, " M f\0"),
                               "git-commit": (1, "boom")}, "--live"),
            ("push-failed", {"git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0"),
                             "git-push": (1, "denied")}, "--live"),
            ("pr-create-failed", {"git-status": (0, " M f"),
                                  "git-status-z": (0, " M f\0"),
                                  "pr-create": (1, "denied")}, "--live"),
        ]
        for outcome, script, mode in cases:
            with self.subTest(outcome=outcome):
                self.setUp()
                try:
                    rc, _ = self.run_main(mode, runner=FakeRunner(script))
                    self.assertEqual(nf.OUTCOME_EXIT_CODES[outcome], rc)
                    self.assertEqual(outcome, self.attempts()[-1]["outcome"])
                finally:
                    self.tearDown()

    def test_deliberate_stand_downs_stay_at_zero(self):
        cases = [
            ("no-changes", {}, "--live", None),
            ("committed-locally", {"git-status": (0, " M f"),
                                   "git-status-z": (0, " M f\0")}, "--trial", None),
            ("pr-opened", {"git-status": (0, " M f"),
                           "git-status-z": (0, " M f\0"),
                           "pr-create": (0, "https://example.invalid/pull/1\n")},
             "--live", None),
            ("pr-check-refused", {}, "--live", "cd ../elsewhere && pytest"),
        ]
        for outcome, script, mode, check in cases:
            with self.subTest(outcome=outcome):
                self.setUp()
                try:
                    if check is not None:
                        self.write_status([item(pr_check=check)])
                    rc, _ = self.run_main(mode, runner=FakeRunner(script))
                    self.assertEqual(0, rc)
                    self.assertEqual(outcome, self.attempts()[-1]["outcome"])
                finally:
                    self.tearDown()

    def test_nothing_eligible_is_zero(self):
        self.write_status([item(status="closed")], eligible_ids=[])
        rc, _ = self.run_main("--live", runner=FakeRunner())
        self.assertEqual(0, rc)

    def test_the_wrapper_hands_the_code_straight_to_launchd(self):
        with open(os.path.join(_ROOT, "tools", "nightly-fix.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('exec /usr/bin/python3 tools/nightly-fix.py --live "$@"', text)
        self.assertNotIn("|| true", text)
        self.assertNotIn("exit 0", text)


# ------------------------------------------------------- a pull request pending

class TestPullRequestPending(Harness):
    def attempt(self, outcome, branch="fix/map-1-20260824", at_="2026-08-24T02:30:00"):
        return {"id": "MAP-1", "at": at_, "mode": "live",
                "outcome": outcome, "detail": "", "branch": branch}

    def test_the_branch_of_the_last_opened_pull_request_is_remembered(self):
        records = [self.attempt("codex-failed", branch="fix/map-1-20260821"),
                   self.attempt("pr-opened", branch="fix/map-1-20260824")]
        self.assertEqual("fix/map-1-20260824", nf.pending_pr_branch(records, "MAP-1"))

    def test_no_pull_request_ever_opened_reads_as_none(self):
        self.assertIsNone(nf.pending_pr_branch([self.attempt("codex-failed")], "MAP-1"))
        self.assertIsNone(nf.pending_pr_branch([], "MAP-1"))

    def test_a_later_stand_down_does_not_clear_the_pending_pull_request(self):
        records = [self.attempt("pr-opened"),
                   self.attempt("pr-pending", at_="2026-08-27T02:30:00")]
        self.assertEqual("fix/map-1-20260824", nf.pending_pr_branch(records, "MAP-1"))

    def test_an_older_record_without_a_branch_still_blocks(self):
        records = [{"id": "MAP-1", "at": "2026-08-24T02:30:00", "outcome": "pr-opened"}]
        self.assertEqual("", nf.pending_pr_branch(records, "MAP-1"))
        self.assertEqual("UNKNOWN", nf.pr_state(FakeRunner(), "org/repo", ""))

    def test_github_answers_are_read_and_anything_else_is_unknown(self):
        for reply, expected in ((json.dumps({"state": "MERGED"}), "MERGED"),
                                (json.dumps({"state": "closed"}), "CLOSED"),
                                (json.dumps({"state": "OPEN"}), "OPEN"),
                                ("not json", "UNKNOWN")):
            with self.subTest(reply=reply):
                runner = FakeRunner({"pr-view": (0, reply)})
                self.assertEqual(expected, nf.pr_state(runner, "org/repo", "fix/x"))
        self.assertEqual("UNKNOWN",
                         nf.pr_state(FakeRunner({"pr-view": (1, "no pr")}), "o/r", "fix/x"))

    def test_an_open_pull_request_blocks_the_item_past_the_backoff(self):
        # four days later: the 72 h backoff has long expired
        nf.write_attempts(os.path.join(self.state, "attempts.json"),
                          [self.attempt("pr-opened")])
        runner = FakeRunner({"pr-view": (0, json.dumps({"state": "OPEN"}))})
        rc, out = self.run_main("--live", runner=runner, now="2026-08-28T02:30:00")
        self.assertEqual(0, rc)
        self.assertIn("pr-pending", out)
        self.assertNotIn("clone", runner.labels())
        self.assertNotIn("codex", runner.labels())

    def test_github_being_unreachable_also_blocks(self):
        nf.write_attempts(os.path.join(self.state, "attempts.json"),
                          [self.attempt("pr-opened")])
        runner = FakeRunner({"pr-view": (1, "gh: could not connect")})
        rc, out = self.run_main("--live", runner=runner, now="2026-08-28T02:30:00")
        self.assertEqual(0, rc)
        self.assertIn("pr-pending", out)
        self.assertNotIn("clone", runner.labels())

    def test_a_merged_pull_request_frees_the_item_again(self):
        nf.write_attempts(os.path.join(self.state, "attempts.json"),
                          [self.attempt("pr-opened")])
        runner = FakeRunner({"pr-view": (0, json.dumps({"state": "MERGED"})),
                             "git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0")})
        rc, out = self.run_main("--live", runner=runner, now="2026-08-28T02:30:00")
        self.assertEqual(0, rc)
        self.assertIn("clone", runner.labels())

    def test_forcing_the_item_by_hand_does_not_beat_the_pending_pull_request(self):
        nf.write_attempts(os.path.join(self.state, "attempts.json"),
                          [self.attempt("pr-opened")])
        runner = FakeRunner({"pr-view": (0, json.dumps({"state": "OPEN"}))})
        rc, out = self.run_main("--live", "--item", "MAP-1", runner=runner,
                                now="2026-08-28T02:30:00")
        self.assertEqual(0, rc)
        self.assertNotIn("clone", runner.labels())

    def test_the_new_attempt_record_saves_the_branch(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0"),
                             "pr-create": (0, "https://example.invalid/pull/1\n")})
        self.run_main("--live", runner=runner)
        self.assertEqual("fix/map-1-20260827", self.attempts()[-1]["branch"])


# --------------------------------------------------------------- staging rail

class TestPorcelainParsing(unittest.TestCase):
    def test_codes_and_paths_come_apart(self):
        self.assertEqual([(" M", "a.py"), ("??", "b.txt")],
                         nf.parse_porcelain_z(" M a.py\0?? b.txt\0"))

    def test_a_path_with_a_space_survives(self):
        self.assertEqual([("??", "two words.txt")],
                         nf.parse_porcelain_z("?? two words.txt\0"))

    def test_a_rename_keeps_the_new_path_and_drops_the_old(self):
        self.assertEqual([("R ", "new.py"), (" M", "c.py")],
                         nf.parse_porcelain_z("R  new.py\0old.py\0 M c.py\0"))

    def test_empty_output_is_empty(self):
        self.assertEqual([], nf.parse_porcelain_z(""))
        self.assertEqual([], nf.parse_porcelain_z(None))


class TestStagingRail(unittest.TestCase):
    def setUp(self):
        self.clone = tempfile.mkdtemp(prefix="nightly-fix-rail-")

    def tearDown(self):
        shutil.rmtree(self.clone, ignore_errors=True)

    def refuse(self, entries):
        return nf.unsafe_staged_paths(entries, self.clone)

    def test_an_ordinary_change_passes(self):
        self.assertEqual([], self.refuse([(" M", "tools/morning-pack.sh")]))

    def test_untracked_credential_names_are_refused(self):
        for name in (".env", "config/defects.env", "key.pem", "id_rsa",
                     "GITHUB_TOKEN.txt", "my-secret.txt", "credentials.yml",
                     "auth.json"):
            with self.subTest(name=name):
                self.assertTrue(self.refuse([("??", name)]),
                                "%s should have been refused" % name)

    def test_the_credential_rule_is_case_insensitive(self):
        self.assertTrue(self.refuse([("??", "My.Secret.Notes")]))

    def test_a_tracked_file_with_a_scary_name_is_not_refused_for_the_name(self):
        # already in the repository: the rail is about what Codex ADDS
        self.assertEqual([], self.refuse([(" M", "tools/token_helper.py")]))

    def test_anything_outside_the_clone_is_refused(self):
        problems = self.refuse([(" M", "../elsewhere/file.py")])
        self.assertEqual(1, len(problems))
        self.assertIn("outside the clone", problems[0])

    def test_a_file_over_a_megabyte_is_refused(self):
        big = os.path.join(self.clone, "big.bin")
        with open(big, "wb") as fh:
            fh.write(b"0" * (nf.MAX_STAGED_BYTES + 1))
        problems = self.refuse([(" M", "big.bin")])
        self.assertEqual(1, len(problems))
        self.assertIn("over the", problems[0])

    def test_a_file_just_under_the_limit_passes(self):
        small = os.path.join(self.clone, "small.bin")
        with open(small, "wb") as fh:
            fh.write(b"0" * nf.MAX_STAGED_BYTES)
        self.assertEqual([], self.refuse([(" M", "small.bin")]))


class TestStagingRailInARun(Harness):
    def test_a_credential_file_stops_the_commit_and_exits_non_zero(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0?? .env\0")})
        rc, _ = self.run_main("--live", runner=runner)
        self.assertEqual(nf.OUTCOME_EXIT_CODES["unsafe-diff"], rc)
        for forbidden in ("git-add", "git-commit", "git-push", "pr-create"):
            self.assertNotIn(forbidden, runner.labels())
        self.assertEqual("unsafe-diff", self.attempts()[-1]["outcome"])

    def test_the_run_log_lists_what_would_have_been_staged(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-status-z": (0, " M tools/morning-pack.sh\0"),
                             "pr-create": (0, "https://example.invalid/pull/1\n")})
        self.run_main("--live", runner=runner)
        name = [f for f in self.state_files() if f == "20260827-MAP-1.md"]
        self.assertEqual(1, len(name))
        with open(os.path.join(self.state, name[0]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("About to stage 1 path(s)", text)
        self.assertIn("tools/morning-pack.sh", text)

    def test_the_rail_reads_the_tree_after_the_check_has_run(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0"),
                             "pr-create": (0, "https://example.invalid/pull/1\n")})
        self.run_main("--live", runner=runner)
        labels = runner.labels()
        self.assertLess(labels.index("pr-check"), labels.index("git-status-z"))
        self.assertLess(labels.index("git-status-z"), labels.index("git-add"))


# ------------------------------------------------------------- a leftover clone

class TestLeftoverClone(Harness):
    def test_a_leftover_clone_is_logged_not_crashed(self):
        leftover = os.path.join(self.clones, "MAP-1-20260827")
        os.makedirs(os.path.join(leftover, ".git"))
        rc, out = self.run_main("--live", runner=FakeRunner())
        self.assertEqual(0, rc)
        self.assertEqual("clone-exists", self.attempts()[-1]["outcome"])
        self.assertIn("clone-exists", out)
        self.assertTrue(os.path.isdir(leftover))

    def test_the_worktree_rail_still_guards_the_clone_parent(self):
        os.makedirs(self.clones)
        os.makedirs(os.path.join(self.clones, ".git"))
        rc, out = self.run_main("--live", runner=FakeRunner())
        self.assertEqual(2, rc)
        self.assertIn("SAFETY", out)


# ------------------------------------------------- stale status and severity

class TestStatusFreshness(Harness):
    def write_status_at(self, generated_at):
        with open(self.status_path, "w") as fh:
            json.dump({
                "schema": "defect-status/v1",
                "generated_at": generated_at,
                "nightly_eligible": ["MAP-1"],
                "items": [item()],
            }, fh)

    def test_a_stale_status_stands_the_lane_down_at_zero(self):
        self.write_status_at("2026-08-25T05:45:00-07:00")   # ~45 h before NOW
        runner = FakeRunner()
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(0, rc)
        self.assertIn("status-stale", out)
        self.assertIn("hours old", out)
        self.assertEqual([], runner.labels())

    def test_a_fresh_status_is_acted_on(self):
        self.write_status_at("2026-08-26T21:45:00-07:00")   # ~7 h before NOW
        runner = FakeRunner()
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(0, rc)
        self.assertNotIn("status-stale", out)
        self.assertIn("clone", runner.labels())

    def test_a_status_without_a_timestamp_is_treated_as_fresh(self):
        with open(self.status_path, "w") as fh:
            json.dump({"schema": "defect-status/v1", "nightly_eligible": ["MAP-1"],
                       "items": [item()]}, fh)
        runner = FakeRunner()
        rc, out = self.run_main("--live", runner=runner)
        self.assertEqual(0, rc)
        self.assertIn("no generated_at", out)


class TestSeverityIsPartOfEligibility(unittest.TestCase):
    def test_a_p0_is_never_taken_unattended(self):
        reason = nf.ineligibility_reason(item(severity="P0"), at(NOW), [])
        self.assertIsNotNone(reason)
        self.assertIn("P0", reason)

    def test_p1_and_below_are_still_fine(self):
        for severity in ("P1", "P2", "p2", "note", None):
            with self.subTest(severity=severity):
                self.assertIsNone(
                    nf.ineligibility_reason(item(severity=severity), at(NOW), []))


# ------------------------------------------------------- the check is sandboxed

class TestSandboxedCheck(unittest.TestCase):
    def test_the_check_runs_under_a_codex_seatbelt(self):
        argv = nf.pr_check_argv("/tmp/clone", "pytest -q")
        self.assertEqual([nf.codex_binary(), "sandbox",
                          "--permission-profile", nf.CODEX_SANDBOX_PROFILE,
                          "-C", "/tmp/clone", "--", "/usr/bin/env"], argv[:8])
        self.assertEqual(["/bin/bash", "-o", "pipefail", "-c", "pytest -q"], argv[-5:])

    def test_the_scratch_cache_locations_are_passed_in(self):
        argv = nf.pr_check_argv("/tmp/clone", "pytest -q")
        for assignment in nf.CHECK_ENV:
            self.assertIn(assignment, argv)
            self.assertTrue(assignment.split("=", 1)[1].startswith("/tmp/"))

    def test_the_check_string_is_one_argument_and_is_never_split(self):
        nasty = "pytest -q && echo 'a b' | tail -1"
        self.assertEqual(nasty, nf.pr_check_argv("/tmp/c", nasty)[-1])

    def test_the_warm_up_never_takes_anything_from_the_register(self):
        runner = FakeRunner()
        self.assertTrue(nf.warm_check_scratch(runner, "uvx --with pytest python -m pytest"))
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(list(nf.CHECK_WARM_ARGV), runner.calls[0][-len(nf.CHECK_WARM_ARGV):])
        self.assertNotIn("--with-editable", " ".join(runner.calls[0]))

    def test_a_check_that_does_not_use_uv_skips_the_warm_up(self):
        runner = FakeRunner()
        self.assertFalse(nf.warm_check_scratch(runner, "python -m pytest -q"))
        self.assertEqual([], runner.calls)


class TestSandboxedCheckInARun(Harness):
    def test_the_check_is_run_through_codex_sandbox_in_the_clone(self):
        runner = FakeRunner({"git-status": (0, " M f"),
                             "git-status-z": (0, " M f\0")})
        self.run_main("--trial", runner=runner)
        checks = [c for c in runner.calls if label(c) == "pr-check"]
        self.assertEqual(1, len(checks))
        self.assertEqual("sandbox", checks[0][1])
        self.assertIn(item()["pr_check"], checks[0])
        self.assertEqual([os.path.join(self.clones, "MAP-1-20260827")],
                         runner.cwd_of("pr-check"))


if __name__ == "__main__":
    unittest.main()
