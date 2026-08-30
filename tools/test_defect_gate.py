#!/usr/bin/env python3
"""Tests for hooks/defect_gate.py and install-defect-gate.sh.

Everything is exercised through a subprocess, so the assertions are on the real
contract a Claude Code hook has -- stdin JSON in, exit code out, one message on
stderr -- not on internals. All fixtures are synthetic: temp status files, temp
marker directories, and a temp fake $HOME for the installer. Nothing here reads
or writes the real register, the real ~/.claude, or the real status file.

    uvx --with pytest python -m pytest -q tools/test_defect_gate.py
"""

import datetime
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(_HERE, "hooks", "defect_gate.py")
INSTALLER = os.path.join(_HERE, "install-defect-gate.sh")

UTC = datetime.timezone.utc
HOUR = 3600.0

MATCHER = "Task|Agent|Workflow"
INSTALLED_COMMAND_TAIL = ".claude/hooks/defect_gate.py"


# --------------------------------------------------------------- fixtures --

def _clean_env(**overrides):
    """A parent environment with every DEFECT_GATE_* leak stripped out."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEFECT_GATE_")}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def _stamp(hours_ago=0.0):
    when = datetime.datetime.now(UTC) - datetime.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(ident="LEAK-1", severity="P0", status="open", days_open=8, **extra):
    open_since = (datetime.date.today()
                  - datetime.timedelta(days=days_open if days_open else 0))
    item = {
        "id": ident,
        "severity": severity,
        "owner": "anthony",
        "fix_scope": "decision",
        "size": "S",
        "system": "leaked client copy deck",
        "symptom": "The leaked file still sits in the default-branch tree.",
        "fix_hint": "One deletion commit on main in each repo, no force-push.",
        "status": status,
        "open_since": open_since.isoformat(),
        "days_open": days_open,
    }
    item.update(extra)
    return item


def _status(tmp_path, items, generated_hours_ago=1.0, name="defects-status.json"):
    payload = {
        "schema": "defect-status/v1",
        "generated_at": _stamp(generated_hours_ago),
        "counts": {"p0_open": len(items)},
        "yours_today": [],
        "items": items,
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _run(status=None, marker_dir=None, tool_name="Task", session_id="sess-abc",
         **env_overrides):
    """Run the hook exactly as the harness would. Returns (rc, stderr).

    ``session_id=None`` omits the key entirely -- the payload shape a harness
    that forgot to send an id would produce.
    """
    payload = {
        "transcript_path": "/tmp/does-not-exist.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"subagent_type": "general-purpose", "prompt": "go"},
    }
    if session_id is not None:
        payload["session_id"] = session_id
    env = _clean_env(
        DEFECT_GATE_STATUS=status if status is not None else "/nonexistent/x.json",
        DEFECT_GATE_MARKER_DIR=marker_dir,
        **env_overrides
    )
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    return proc.returncode, proc.stderr


def _marker(tmp_path, name, hours_old=0.0):
    directory = tmp_path / "fix-mode"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text("", encoding="utf-8")
    if hours_old:
        when = os.path.getmtime(str(path)) - hours_old * HOUR
        os.utime(str(path), (when, when))
    return str(directory)


# ------------------------------------------------------------- pass-through --

def test_missing_status_file_passes():
    rc, err = _run(status="/nonexistent/defects-status.json")
    assert rc == 0
    assert err == ""


def test_unparseable_status_file_passes(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json at all", encoding="utf-8")
    rc, err = _run(status=str(path))
    assert rc == 0
    assert err == ""


def test_stale_status_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)], generated_hours_ago=40)
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_status_stamped_far_in_the_future_passes(tmp_path):
    """A stamp ahead of the clock is a bad file, not a very fresh one."""
    items = [_item(ident="FUTURE-1", days_open=30)]
    # The same items block when the stamp is honest ...
    assert _run(status=_status(tmp_path, items, name="honest.json"))[0] == 2
    # ... and pass when it is 48h in the future.
    status = _status(tmp_path, items, generated_hours_ago=-48, name="future.json")
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_a_stamp_barely_ahead_of_the_clock_is_still_trusted(tmp_path):
    """Ordinary clock jitter must not switch the gate off."""
    status = _status(tmp_path, [_item(days_open=30)], generated_hours_ago=-0.25)
    rc, err = _run(status=status)
    assert rc == 2, err


def test_crash_inside_the_hook_still_passes(tmp_path):
    """The outer net: any surprise exception is exit 0, never a jammed session."""
    item = _item(ident="NAN-1", days_open=30)
    item["days_open"] = float("nan")     # json.dumps writes a bare NaN
    status = _status(tmp_path, [item])
    with open(status, "r", encoding="utf-8") as handle:
        assert "NaN" in handle.read()
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_status_without_generated_at_passes(tmp_path):
    path = tmp_path / "no-stamp.json"
    path.write_text(json.dumps({"schema": "defect-status/v1",
                                "items": [_item(days_open=30)]}), encoding="utf-8")
    rc, err = _run(status=str(path))
    assert rc == 0, err


def test_ungated_tool_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    rc, err = _run(status=status, tool_name="Bash")
    assert rc == 0, err
    assert err == ""


def test_disable_env_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    rc, err = _run(status=status, DEFECT_GATE_DISABLE="1")
    assert rc == 0, err


# ----------------------------------------------------------- the block rule --

@pytest.mark.parametrize("tool_name", ["Task", "Agent", "Workflow"])
def test_old_p0_blocks_every_gated_tool(tmp_path, tool_name):
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=8)])
    rc, err = _run(status=status, tool_name=tool_name)
    assert rc == 2
    assert "LEAK-1" in err
    assert "8 days" in err


def test_block_message_is_short_and_actionable(tmp_path):
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=9)])
    rc, err = _run(status=status, session_id="sess-xyz")
    assert rc == 2
    lines = [line for line in err.strip().splitlines() if line.strip()]
    assert len(lines) <= 6, err
    assert err.startswith("defect-gate: LEAK-1 has blocked unattended work for 9 days:")
    assert "Fix: One deletion commit" in err
    assert "touch ~/.claude/defect-fix-mode/sess-xyz" in err
    assert "~/code/fully-aware/state/DEFECTS.md" in err


def test_the_message_says_how_to_clear_the_block_after_fixing_it(tmp_path):
    """The hook reads the status file, not the register: fixing it is not enough
    on its own, and nothing used to say so (review 2026-08-28, GATE-INSTALL-2)."""
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=9)])
    rc, err = _run(status=status)
    assert rc == 2
    assert "tools/verify-defects.py" in err
    assert "refresh the status so the gate reopens" in err


def test_a_payload_with_no_session_id_is_told_to_use_the_all_marker(tmp_path):
    """With no id there is no per-session marker to create; the old text printed
    a literal <session_id>, which is not even valid shell (GATE-INSTALL-6)."""
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=9)])
    rc, err = _run(status=status, session_id=None)
    assert rc == 2
    assert "<session_id>" not in err
    assert "touch ~/.claude/defect-fix-mode/ALL" in err
    assert "tools/verify-defects.py" in err


def test_an_empty_session_id_gets_the_same_all_marker_instruction(tmp_path):
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=9)])
    rc, err = _run(status=status, session_id="")
    assert rc == 2
    assert "<session_id>" not in err
    assert "touch ~/.claude/defect-fix-mode/ALL" in err


def test_the_all_marker_the_message_names_actually_opens_the_gate(tmp_path):
    """The instruction has to be one a session can follow and get through."""
    status = _status(tmp_path, [_item(ident="LEAK-1", days_open=9)])
    markers = _marker(tmp_path, "ALL")
    rc, err = _run(status=status, marker_dir=markers, session_id=None)
    assert rc == 0, err
    assert err == ""


def test_an_accepted_p0_never_blocks_however_old_it_is(tmp_path):
    """A standing decision not to fix something must not jam every session shut
    (ruling E 2026-08-26; the register records it, the gate ignores it)."""
    status = _status(tmp_path, [_item(ident="LEAK-3", days_open=400,
                                      status="accepted")])
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_p0_open_six_days_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=6)])
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_p0_open_exactly_seven_days_blocks(tmp_path):
    status = _status(tmp_path, [_item(days_open=7)])
    rc, err = _run(status=status)
    assert rc == 2, err


@pytest.mark.parametrize("status_value", ["error", "provisional", "deferred", "fixed"])
def test_non_open_p0_passes(tmp_path, status_value):
    status = _status(tmp_path, [_item(status=status_value, days_open=30)])
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_old_p1_passes(tmp_path):
    status = _status(tmp_path, [_item(severity="P1", days_open=30)])
    rc, err = _run(status=status)
    assert rc == 0, err
    assert err == ""


def test_days_open_null_is_computed_from_open_since(tmp_path):
    item = _item(ident="AGE-1", days_open=11)
    item["days_open"] = None
    status = _status(tmp_path, [item])
    rc, err = _run(status=status)
    assert rc == 2
    assert "AGE-1" in err
    assert "11 days" in err


def test_threshold_is_overridable(tmp_path):
    status = _status(tmp_path, [_item(days_open=3)])
    assert _run(status=status)[0] == 0
    rc, err = _run(status=status, DEFECT_GATE_DAYS="2")
    assert rc == 2, err


def test_oldest_of_two_p0s_is_named(tmp_path):
    status = _status(tmp_path, [
        _item(ident="NEWER-1", days_open=8),
        _item(ident="OLDER-1", days_open=21),
    ])
    rc, err = _run(status=status)
    assert rc == 2
    assert "OLDER-1" in err
    assert err.startswith("defect-gate: OLDER-1")
    assert "2 P0 defects are past the line" in err


# ------------------------------------------------------------------ fix mode --

def test_marker_for_this_session_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    markers = _marker(tmp_path, "sess-abc")
    rc, err = _run(status=status, marker_dir=markers, session_id="sess-abc")
    assert rc == 0, err
    assert err == ""


def test_marker_for_another_session_still_blocks(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    markers = _marker(tmp_path, "sess-other")
    rc, err = _run(status=status, marker_dir=markers, session_id="sess-abc")
    assert rc == 2, err


def test_marker_for_all_passes(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    markers = _marker(tmp_path, "ALL")
    rc, err = _run(status=status, marker_dir=markers, session_id="sess-abc")
    assert rc == 0, err


def test_marker_older_than_twelve_hours_blocks(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    markers = _marker(tmp_path, "sess-abc", hours_old=13)
    rc, err = _run(status=status, marker_dir=markers, session_id="sess-abc")
    assert rc == 2, err


def test_expired_all_marker_blocks(tmp_path):
    status = _status(tmp_path, [_item(days_open=30)])
    markers = _marker(tmp_path, "ALL", hours_old=13)
    rc, err = _run(status=status, marker_dir=markers, session_id="sess-abc")
    assert rc == 2, err


# ----------------------------------------------------------------- installer --

def _fake_home(tmp_path):
    """A throwaway $HOME with the four settings roots this Mac really has."""
    home = tmp_path / "home"
    roots = [home / ".claude"]
    for account in ("agentic-acumen", "stanford-alumni", "tonyflo-gmail"):
        roots.append(home / ".claude-accounts" / account)
    base = {
        "model": "opus",
        "hooks": {"PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "python3 check_careful.py"}]},
        ]},
    }
    for root in roots:
        root.mkdir(parents=True)
        (root / "settings.json").write_text(
            json.dumps(base, indent=2) + "\n", encoding="utf-8")
    return home, [root / "settings.json" for root in roots]


def _install(home, *args):
    proc = subprocess.run(
        ["bash", INSTALLER, "--root", str(home)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _gate_entries(settings_path):
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return [
        entry for entry in data.get("hooks", {}).get("PreToolUse", [])
        if any(str(h.get("command", "")).endswith(INSTALLED_COMMAND_TAIL)
               for h in entry.get("hooks", []))
    ]


def test_dry_run_reports_every_root_and_changes_nothing(tmp_path):
    home, settings = _fake_home(tmp_path)
    before = {p: (p.read_text(encoding="utf-8"), p.stat().st_mtime) for p in settings}

    out = _install(home)

    assert "DRY RUN" in out
    assert MATCHER in out
    for path in settings:
        assert str(path) in out
        assert path.read_text(encoding="utf-8") == before[path][0]
        assert path.stat().st_mtime == before[path][1]
    assert out.count("would add 1 PreToolUse entry") == len(settings)
    # Nothing on disk moved: no hook body copied, no backups written.
    assert not (home / ".claude" / "hooks" / "defect_gate.py").exists()
    assert list(tmp_path.glob("**/*.bak")) == []


def test_apply_is_idempotent(tmp_path):
    home, settings = _fake_home(tmp_path)

    first = _install(home, "--apply")
    assert first.count("added 1 PreToolUse entry") == len(settings)
    hook_body = home / ".claude" / "hooks" / "defect_gate.py"
    assert hook_body.exists()

    second = _install(home, "--apply")
    assert second.count("already installed") == len(settings)
    assert "added 1 PreToolUse entry" not in second

    for path in settings:
        entries = _gate_entries(path)
        assert len(entries) == 1, path
        assert entries[0]["matcher"] == MATCHER
        assert entries[0]["hooks"][0]["type"] == "command"
        # The pre-existing hook survived untouched.
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["model"] == "opus"
        assert data["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
        assert path.read_text(encoding="utf-8").endswith("}\n")
        assert len(list(path.parent.glob("settings.json.pre-defect-gate-*.bak"))) == 1


def test_remove_reverses_the_install(tmp_path):
    home, settings = _fake_home(tmp_path)
    _install(home, "--apply")

    dry = _install(home, "--remove")
    assert "would remove 1 PreToolUse entry" in dry
    assert all(len(_gate_entries(path)) == 1 for path in settings)

    _install(home, "--remove", "--apply")
    for path in settings:
        assert _gate_entries(path) == []
        assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert not (home / ".claude" / "hooks" / "defect_gate.py").exists()

    again = _install(home, "--remove", "--apply")
    assert again.count("not installed -- no change") == len(settings)
