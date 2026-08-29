#!/usr/bin/env python3
"""Fixture-only tests for the defect register's morning verifier."""

import datetime
import importlib.util
import json
import os
import shlex
import subprocess

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "verify-defects.py")
_SPEC = importlib.util.spec_from_file_location("verify_defects", _SCRIPT)
vd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vd)


def _item(item_id, verify="false", severity="P1", owner="session",
          fix_scope="local", since="2026-08-20", **overrides):
    item = {
        "id": item_id,
        "severity": severity,
        "owner": owner,
        "fix_scope": fix_scope,
        "size": "S",
        "system": "synthetic system",
        "symptom": "%s is broken" % item_id,
        "fix_hint": "fix %s" % item_id,
        "since": since,
        "verify": verify,
        "provisional": False,
        "not_before": None,
    }
    item.update(overrides)
    return item


def _register(items, updated="2026-08-27"):
    return {"schema": "defect-register/v1", "updated": updated,
            "rules": {}, "items": items}


def _now(day="2026-08-28"):
    return datetime.datetime.fromisoformat(day + "T12:00:00+00:00")


def _by_id(status):
    return {item["id"]: item for item in status["items"]}


def _write_register(path, items):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_register(items), fh)


def test_real_shell_exit_codes_cwd_path_and_stderr_capture():
    command = (
        'test "$PWD" = "$HOME" && '
        'case "$PATH" in "/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:"*) '
        'printf stdout-only; printf stderr-only >&2;; *) exit 9;; esac'
    )
    status, code, stderr, _duration = vd.run_verify(command)
    assert (status, code, stderr) == ("fixed", 0, "stderr-only")

    assert vd.run_verify("true")[:2] == ("fixed", 0)
    assert vd.run_verify("false")[:2] == ("open", 1)
    assert vd.run_verify("exit 3")[:2] == ("open", 3)


def test_sleep_timeout_is_an_error_with_a_tiny_test_override(monkeypatch):
    monkeypatch.setattr(vd, "VERIFY_TIMEOUT", 0.01)
    status, code, stderr, _duration = vd.run_verify("sleep 0.2")
    assert status == "error"
    assert code is None
    assert "timed out" in stderr


def test_provisional_and_future_items_are_not_run():
    called = []

    def runner(command):
        called.append(command)
        return 0, "", ""

    register = _register([
        _item("PROV", verify="exit 99", provisional=True),
        _item("LATER", verify="exit 98", not_before="2026-08-29"),
        _item("DUE", verify="true"),
    ])
    status = vd.build_status(register, _now(), runner=runner)
    records = _by_id(status)
    assert called == ["true"]
    assert records["PROV"]["status"] == "provisional"
    assert records["LATER"]["status"] == "deferred"
    assert records["DUE"]["status"] == "fixed"
    assert records["PROV"]["last_verified"] is None
    assert records["LATER"]["last_verified"] is None


def test_history_carries_forward_across_two_runs_and_resets_regressions():
    items = [
        _item("STILL", verify="still", since="2026-08-10"),
        _item("REGRESS", verify="regress", since="2026-08-11"),
        _item("STAYS-FIXED", verify="fixed", since="2026-08-12"),
    ]
    phase = {"number": 1}

    def runner(command):
        if command == "still":
            return 1, "", "still broken"
        if command == "regress":
            return (0, "", "") if phase["number"] == 1 else (3, "", "back again")
        return 0, "", ""

    first = vd.build_status(_register(items), _now("2026-08-27"), runner=runner)
    phase["number"] = 2
    second_items = items + [_item("NEW", verify="still", since="2026-08-01")]
    previous = _by_id(first)
    second = vd.build_status(_register(second_items), _now(), previous=previous,
                             runner=runner)
    records = _by_id(second)

    assert records["STILL"]["open_since"] == "2026-08-10"
    assert records["STILL"]["days_open"] == 18
    assert records["REGRESS"]["open_since"] == "2026-08-28"
    assert records["REGRESS"]["days_open"] == 0
    assert records["REGRESS"]["fixed_at"] is None
    assert records["STAYS-FIXED"]["fixed_at"] == "2026-08-27"
    assert records["NEW"]["open_since"] == "2026-08-01"
    assert records["NEW"]["days_open"] == 27


def test_counts_ordering_eligibility_and_record_shape():
    items = [
        _item("P0-NEW", severity="P0", owner="anthony", since="2026-08-27"),
        _item("P1-MID", severity="P1", owner="anthony", since="2026-08-18"),
        _item("P2-OLD", severity="P2", owner="anthony", since="2026-08-01"),
        _item("NIGHT", severity="P1", owner="codex", fix_scope="repo-pr"),
        _item("NO-SCOPE", severity="P1", owner="codex", fix_scope="local"),
        _item("PROV", severity="P2", owner="codex", fix_scope="repo-pr",
              provisional=True),
        _item("LATER", severity="P2", owner="codex", fix_scope="repo-pr",
              not_before="2026-08-29"),
        _item("ERR", severity="P0", owner="session", verify="error"),
        _item("FIXED", severity="P2", owner="session", verify="fixed"),
    ]

    def runner(command):
        if command == "fixed":
            return 0, "successful stdout", ""
        if command == "error":
            raise subprocess.TimeoutExpired(command, 60)
        return 1, "ignored stdout", "x" * 350

    status = vd.build_status(_register(items), _now(), runner=runner)
    counts = status["counts"]
    assert counts["P0"] == {"open": 1, "oldest_days": 1}
    assert counts["P1"] == {"open": 3, "oldest_days": 10}
    assert counts["P2"] == {"open": 1, "oldest_days": 27}
    assert counts["fixed_since_last"] == 1
    assert counts["provisional"] == 1
    assert counts["deferred"] == 1
    assert counts["error"] == 1
    assert counts["open_by_owner"] == {"anthony": 3, "codex": 2}
    assert status["yours_today"] == ["P0-NEW", "P2-OLD", "P1-MID"]
    assert status["nightly_eligible"] == ["NIGHT"]

    expected_fields = {
        "id", "severity", "owner", "fix_scope", "size", "system", "symptom",
        "fix_hint", "status", "exit", "last_verified", "open_since",
        "days_open", "fixed_at", "duration_ms", "stderr_tail",
    }
    for record in status["items"]:
        assert set(record) == expected_fields
        assert len(record["stderr_tail"]) <= 300
    assert _by_id(status)["P0-NEW"]["stderr_tail"] == "x" * 300
    assert "ignored stdout" not in _by_id(status)["P0-NEW"]["stderr_tail"]


def test_markdown_summary_group_order_exact_bullets_and_error_tail():
    records = [
        dict(vd.evaluate_item(_item("YOU", owner="anthony"), None,
                              "2026-08-28", runner=lambda _c: (1, "", "bad"))),
        dict(vd.evaluate_item(_item("SESSION", owner="session"), None,
                              "2026-08-28", runner=lambda _c: (1, "", "bad"))),
        dict(vd.evaluate_item(_item("NIGHT", owner="codex", fix_scope="repo-pr"),
                              None, "2026-08-28",
                              runner=lambda _c: (1, "", "bad"))),
        dict(vd.evaluate_item(_item("JAY", owner="jay", fix_scope="external"),
                              None, "2026-08-28",
                              runner=lambda _c: (1, "", "bad"))),
        dict(vd.evaluate_item(_item("PROV", provisional=True), None,
                              "2026-08-28")),
        dict(vd.evaluate_item(_item("LATER", not_before="2026-08-29"), None,
                              "2026-08-28")),
        dict(vd.evaluate_item(_item("FIX", verify="true"), None,
                              "2026-08-28", runner=lambda _c: (0, "", ""))),
        dict(vd.evaluate_item(_item("ERR"), None, "2026-08-28",
                              runner=lambda _c: (_ for _ in ()).throw(
                                  subprocess.TimeoutExpired("sleep", 60)))),
    ]
    status = {
        "schema": "defect-status/v1",
        "generated_at": "2026-08-28T12:00:00+00:00",
        "counts": vd.build_counts(records, "2026-08-28"),
        "items": records,
    }
    md = vd.render_md(status)
    lines = md.splitlines()
    expected_summary = (
        "DEFECTS -- P0: 0 · P1: 4 · P2: 0 · fixed since yesterday: 1 · "
        "yours today: 1 · no real check yet: 1"
    )
    assert lines[:3] == [vd.MD_TITLE, "", expected_summary]
    headings = [line for line in lines if line.startswith("## ")]
    assert headings == [
        "## Only you can do these (1)",
        "## A supervised session on this Mac (1)",
        "## The nightly lane can take these (1)",
        "## Waiting on someone else (1)",
        "## No real check yet (1)",
        "## Deferred (1)",
        "## Fixed since yesterday (1)",
        "## Check errored (1)",
    ]
    assert "- YOU — 8d open — YOU is broken — fix: fix YOU" in lines
    error_line = next(line for line in lines if line.startswith("- ERR "))
    assert "the check errored: verify timed out after 60s" in error_line


def test_empty_groups_are_still_rendered():
    status = {
        "generated_at": "2026-08-28T12:00:00+00:00",
        "counts": vd.build_counts([], "2026-08-28"),
        "items": [],
    }
    assert vd.render_md(status).count(" (0)") == 8


def test_only_runs_one_and_writes_nothing(tmp_path, capsys):
    marker = tmp_path / "ran"
    register = tmp_path / "register.json"
    _write_register(str(register), [
        _item("ONE", verify="printf ONE >> %s" % shlex.quote(str(marker))),
        _item("TWO", verify="printf TWO >> %s" % shlex.quote(str(marker))),
    ])
    status_out = tmp_path / "status.json"
    md_out = tmp_path / "DEFECTS.md"
    rc = vd.main(["--register", str(register), "--status-out", str(status_out),
                  "--md-out", str(md_out), "--only", "TWO",
                  "--now", "2026-08-28T12:00:00Z"])
    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "TWO"
    assert not status_out.exists()
    assert not md_out.exists()
    assert "TWO: fixed" in capsys.readouterr().out


def test_dry_run_honors_only_runs_nothing_and_writes_nothing(tmp_path, capsys):
    marker = tmp_path / "ran"
    register = tmp_path / "register.json"
    _write_register(str(register), [
        _item("ONE", verify="printf ONE >> %s" % shlex.quote(str(marker))),
        _item("TWO", verify="printf TWO >> %s" % shlex.quote(str(marker))),
    ])
    status_out = tmp_path / "status.json"
    md_out = tmp_path / "DEFECTS.md"
    rc = vd.main(["--register", str(register), "--status-out", str(status_out),
                  "--md-out", str(md_out), "--only", "ONE", "--dry-run",
                  "--now", "2026-08-28T12:00:00Z"])
    assert rc == 0
    assert not marker.exists()
    assert not status_out.exists()
    assert not md_out.exists()
    output = capsys.readouterr().out
    assert "ONE" in output
    assert "TWO" not in output


def test_main_exit_codes_and_failing_checks_are_data(tmp_path):
    missing = tmp_path / "missing.json"
    assert vd.main(["--register", str(missing), "--dry-run"]) == 2

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert vd.main(["--register", str(broken), "--dry-run"]) == 2

    register = tmp_path / "register.json"
    _write_register(str(register), [_item("OPEN", verify="exit 3")])
    status_out = tmp_path / "status.json"
    md_out = tmp_path / "DEFECTS.md"
    assert vd.main(["--register", str(register),
                    "--status-out", str(status_out), "--md-out", str(md_out),
                    "--now", "2026-08-28T12:00:00Z"]) == 0
    with open(status_out, "r", encoding="utf-8") as fh:
        status = json.load(fh)
    assert status["items"][0]["status"] == "open"
    assert status["items"][0]["exit"] == 3

    # An output path that cannot be written is exit 2 (a run whose outputs are
    # missing must be loud), never 0 and never the register-failure code alone.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a regular file\n", encoding="utf-8")
    assert vd.main(["--register", str(register),
                    "--status-out", str(tmp_path / "second.json"),
                    "--md-out", str(blocker / "DEFECTS.md"),
                    "--now", "2026-08-28T12:00:00Z"]) == 2
    assert not (tmp_path / "second.json").exists()


# --------------------------------------------------------------- private env --

def test_private_env_parses_only_well_formed_defect_keys(tmp_path):
    path = tmp_path / "defects.env"
    path.write_text("\n".join([
        "# a comment line",
        "",
        "   ",
        "DEFECT_PLAIN=plain",
        'DEFECT_DQ="double quoted"',
        "DEFECT_SQ='single quoted'",
        "export DEFECT_EXPORTED=exported",
        "DEFECT_EMPTY=",
        "  DEFECT_SPACED  =  spaced  ",
        "DEFECT_INNER=a=b=c",
        "lower_case=skipped",
        "9LEADING=skipped",
        "DEFECT-DASH=skipped",
        "NO_EQUALS_SIGN",
        "OTHER_UPPERCASE=skipped",          # uppercase but not DEFECT_
        "DEFECT=skipped",                   # the bare prefix is not a key
        "DEFECT_=skipped",                  # nothing after the prefix
        "defect_lower_prefix=skipped",
    ]) + "\n", encoding="utf-8")
    assert vd.load_private_env(str(path)) == {
        "DEFECT_PLAIN": "plain",
        "DEFECT_DQ": "double quoted",
        "DEFECT_SQ": "single quoted",
        "DEFECT_EXPORTED": "exported",
        "DEFECT_EMPTY": "",
        "DEFECT_SPACED": "spaced",
        "DEFECT_INNER": "a=b=c",
    }


def test_private_env_ignores_path_and_other_environment_overrides(tmp_path,
                                                                  monkeypatch):
    """A ``PATH=`` line in the private file must not reach a verify.

    ``_shell_runner`` applies the private values AFTER setting the
    Homebrew-first PATH the register's rules promise, so an unfiltered file
    could redirect every check at what it runs.
    """
    path = tmp_path / "defects.env"
    path.write_text("\n".join([
        "PATH=/tmp/evil",
        "HOME=/tmp/evil-home",
        "LD_PRELOAD=/tmp/evil.so",
        "DEFECT_KEPT=kept",
    ]) + "\n", encoding="utf-8")
    assert vd.load_private_env(str(path)) == {"DEFECT_KEPT": "kept"}

    # And end to end: the verify still sees the real PATH and $HOME.
    monkeypatch.setattr(vd, "PRIVATE_ENV_PATH", str(path))
    assert vd.run_verify('[ "$PATH" != /tmp/evil ] && [ "$HOME" != /tmp/evil-home ]'
                         ' && [ "$DEFECT_KEPT" = kept ]')[:2] == ("fixed", 0)


def test_private_env_is_empty_when_the_file_is_missing_or_unreadable(tmp_path):
    assert vd.load_private_env(str(tmp_path / "nope.env")) == {}
    assert vd.load_private_env(str(tmp_path)) == {}      # a directory: OSError


def test_private_env_falls_back_to_the_module_constant(tmp_path, monkeypatch):
    path = tmp_path / "defects.env"
    path.write_text("DEFECT_X=hello\n", encoding="utf-8")
    monkeypatch.setattr(vd, "PRIVATE_ENV_PATH", str(path))
    assert vd.load_private_env() == {"DEFECT_X": "hello"}


def test_a_verify_reads_private_values_and_fails_open_without_them(tmp_path,
                                                                  monkeypatch):
    """A missing private file must make the verify FAIL (open), never pass."""
    monkeypatch.delenv("DEFECT_X", raising=False)
    command = '[ "$DEFECT_X" = hello ]'

    env_file = tmp_path / "defects.env"
    env_file.write_text('DEFECT_X="hello"\n', encoding="utf-8")
    monkeypatch.setattr(vd, "PRIVATE_ENV_PATH", str(env_file))
    assert vd.run_verify(command)[:2] == ("fixed", 0)

    monkeypatch.setattr(vd, "PRIVATE_ENV_PATH", str(tmp_path / "gone.env"))
    assert vd.run_verify(command)[:2] == ("open", 1)


def test_a_refused_write_exits_two_and_writes_nothing(tmp_path, monkeypatch):
    register = tmp_path / "register.json"
    _write_register(str(register), [_item("OPEN", verify="true")])
    status_out = tmp_path / "status.json"
    md_out = tmp_path / "DEFECTS.md"

    def refuse(path):
        raise vd.WriteRefused("refusing to write %s" % path)

    monkeypatch.setattr(vd, "assert_gitignored", refuse)
    rc = vd.main(["--register", str(register), "--status-out", str(status_out),
                  "--md-out", str(md_out), "--now", "2026-08-28T12:00:00Z"])
    assert rc == 2
    assert not status_out.exists()
    assert not md_out.exists()


def test_relative_cli_paths_resolve_from_repo_root():
    assert vd.resolve_repo_path("registers/example.json") == os.path.join(
        vd._REPO_ROOT, "registers", "example.json")
    absolute = os.path.abspath(os.path.join(os.sep, "tmp", "example.json"))
    assert vd.resolve_repo_path(absolute) == absolute

