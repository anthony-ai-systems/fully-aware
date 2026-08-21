#!/usr/bin/env python3
"""Tests for tools/daily-scan/deadman_lanes.py -- pytest.

All fixtures are SYNTHETIC and live in tmp_path: a hand-built
automation-map.json (schema automation-map/v1, launchd nodes only -- the
subcommand ignores every other lane), captured `launchctl list` text, and
artifact log files whose mtimes are pinned with os.utime. The clock is
frozen via --now, so every staleness assertion is deterministic. The script
lives in tools/daily-scan/, outside any package, so it is loaded via
importlib like the sibling generate-automation-map tests. Run with:

    pytest tools/test_deadman_lanes.py -q
"""

import datetime
import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

NOW = "2026-08-20T12:00:00+00:00"
NOW_DT = datetime.datetime(2026, 8, 20, 12, 0,
                           tzinfo=datetime.timezone.utc)


def _load():
    spec = importlib.util.spec_from_file_location(
        "deadman_lanes",
        os.path.join(_HERE, "daily-scan", "deadman_lanes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dl = _load()


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #

def _node(tmp_path, label, subtitle="every 1800s", age_s=None,
          no_artifact=False):
    """A launchd node in the real automation-map/v1 shape. Unless
    no_artifact, a stdout log exists, back-dated age_s seconds before NOW
    (0 = written exactly at NOW)."""
    detail = {"program": "/bin/true", "status_note": "loaded, last exit 0",
              "stdout": None, "stderr": None}
    if not no_artifact:
        log = tmp_path / ("%s.out.log" % label)
        log.write_text("ran\n")
        ts = (NOW_DT - datetime.timedelta(seconds=age_s or 0)).timestamp()
        os.utime(log, (ts, ts))
        detail["stdout"] = str(log)
    return {"id": "la:" + label, "lane": "launchd", "title": label,
            "subtitle": subtitle, "executor": "script", "status": "ok",
            "source": "/x/%s.plist" % label, "detail": detail,
            "human_gated": False}


def _write_map(tmp_path, nodes, generated_at="2026-08-20T06:00:00+00:00"):
    path = tmp_path / "automation-map.json"
    path.write_text(json.dumps({
        "schema": "automation-map/v1", "generated_at": generated_at,
        "advisory": "test", "lanes": [], "launchd_excluded":
        {"count": 0, "labels": []}, "nodes": nodes, "edges": []}))
    return str(path)


def _write_launchctl(tmp_path, labels):
    path = tmp_path / "launchctl.txt"
    lines = ["PID\tStatus\tLabel"] + ["-\t0\t%s" % lbl for lbl in labels]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _deadman(capsys, map_path, lc_path, max_lines=None):
    argv = ["deadman", "--map", map_path, "--launchctl-list", lc_path,
            "--now", NOW]
    if max_lines is not None:
        argv += ["--max-lines", str(max_lines)]
    rc = dl.main(argv)
    out = capsys.readouterr().out
    assert rc == 0                       # dead lanes are a report, not an error
    assert out.startswith("### LANES THAT DID NOT RUN\n")
    return out


# --------------------------------------------------------------------------- #
# deadman
# --------------------------------------------------------------------------- #

def test_disarmed_listed_before_stale_armed(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.stale", age_s=2 * 86400),
             _node(tmp_path, "com.anthony.dead", age_s=0)]
    # "dead" is fresh on disk but absent from launchctl: DISARMED anyway --
    # arming comes from the live list, never from artifacts or map status.
    lc = _write_launchctl(tmp_path, ["com.anthony.stale"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "- com.anthony.dead — DISARMED" in out
    assert "com.anthony.stale — armed, no artifact since" in out
    assert out.index("com.anthony.dead") < out.index("com.anthony.stale")


def test_stale_interval_lane_listed_with_age_fresh_not(tmp_path, capsys):
    # every 1800s -> threshold max(3*1800, 3h) = 3h. 4h+ is stale; 10 min is not.
    nodes = [_node(tmp_path, "com.anthony.quiet", age_s=4 * 3600 + 90),
             _node(tmp_path, "com.anthony.fresh", age_s=600)]
    lc = _write_launchctl(tmp_path,
                          ["com.anthony.quiet", "com.anthony.fresh"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert ("- com.anthony.quiet — armed, no artifact since "
            "2026-08-20T07:58:30+00:00 (4h 1m)") in out
    assert "com.anthony.fresh" not in out


def test_no_artifact_ever_counts_as_stalest(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.silent", no_artifact=True),
             _node(tmp_path, "com.anthony.quiet", age_s=4 * 3600)]
    lc = _write_launchctl(tmp_path,
                          ["com.anthony.silent", "com.anthony.quiet"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "- com.anthony.silent — armed, no artifact ever" in out
    assert out.index("com.anthony.silent") < out.index("com.anthony.quiet")


def test_calendar_subtitle_uses_26h_threshold(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.daily", subtitle="daily 05:45",
                   age_s=20 * 3600)]
    lc = _write_launchctl(tmp_path, ["com.anthony.daily"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "All 1 armed launchd lanes ran within threshold." in out

    nodes = [_node(tmp_path, "com.anthony.daily", subtitle="daily 05:45",
                   age_s=30 * 3600 + 90)]
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "com.anthony.daily — armed, no artifact since" in out
    assert "(1d 6h)" in out


def test_all_healthy_reports_the_armed_count(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.a", age_s=60),
             _node(tmp_path, "com.anthony.b", subtitle="daily 06:15",
                   age_s=3600)]
    lc = _write_launchctl(tmp_path, ["com.anthony.a", "com.anthony.b"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "All 2 armed launchd lanes ran within threshold." in out
    assert "DISARMED" not in out


def test_non_launchd_lanes_are_ignored(tmp_path, capsys):
    codex = {"id": "cx:pulse", "lane": "codex", "title": "pulse",
             "subtitle": "FREQ=DAILY", "executor": "ai", "status": "ok",
             "source": "/x/automation.toml", "detail": {},
             "human_gated": False}
    nodes = [codex, _node(tmp_path, "com.anthony.a", age_s=60)]
    lc = _write_launchctl(tmp_path, ["com.anthony.a"])
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "pulse" not in out
    assert "All 1 armed launchd lanes ran within threshold." in out


def test_empty_launchctl_input_is_inconclusive_not_green(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.a", age_s=60)]
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    out = _deadman(capsys, _write_map(tmp_path, nodes), str(empty))
    assert "DEAD-MAN CHECK INCONCLUSIVE" in out
    assert "ran within threshold" not in out


def test_unreadable_map_is_inconclusive_not_green(tmp_path, capsys):
    lc = _write_launchctl(tmp_path, ["com.anthony.a"])
    out = _deadman(capsys, str(tmp_path / "no-such-map.json"), lc)
    assert "DEAD-MAN CHECK INCONCLUSIVE" in out
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    out = _deadman(capsys, str(bad), lc)
    assert "DEAD-MAN CHECK INCONCLUSIVE" in out


def test_stale_generated_at_warns_and_still_reports(tmp_path, capsys):
    nodes = [_node(tmp_path, "com.anthony.a", age_s=60)]
    lc = _write_launchctl(tmp_path, ["com.anthony.a"])
    out = _deadman(capsys, _write_map(
        tmp_path, nodes, generated_at="2026-08-10T06:00:00+00:00"), lc)
    assert "WARNING: automation-map.json is stale (generated 2026-08-10)." in out
    assert "All 1 armed launchd lanes ran within threshold." in out
    # fresh map -> no warning
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert "WARNING" not in out


def test_more_than_ten_dead_lanes_capped_with_more_line(tmp_path, capsys):
    labels = ["com.anthony.lane%02d" % i for i in range(13)]
    nodes = [_node(tmp_path, lbl, no_artifact=True) for lbl in labels]
    lc = _write_launchctl(tmp_path, [])  # header only: nothing armed
    # header-only launchctl output still parses (the "Label" header row),
    # so this is a real all-disarmed report, not INCONCLUSIVE.
    out = _deadman(capsys, _write_map(tmp_path, nodes), lc)
    assert out.count("— DISARMED") == 10
    assert "- (+3 more)" in out
    assert "com.anthony.lane12" not in out    # sorted; the tail is suppressed


# --------------------------------------------------------------------------- #
# cap
# --------------------------------------------------------------------------- #

def _cap(path, max_items=10):
    assert dl.main(["cap", "--brief", str(path),
                    "--max-items", str(max_items)]) == 0


def test_cap_truncates_fifteen_item_list(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\n"
                     + "".join("- item %02d\n" % i for i in range(15))
                     + "\nAfter.\n")
    _cap(brief)
    text = brief.read_text()
    assert "- item 09\n- (+5 more suppressed)\n" in text
    assert "- item 10" not in text
    assert text.endswith("\nAfter.\n")


def test_cap_leaves_ten_item_list_byte_identical(tmp_path):
    brief = tmp_path / "brief.md"
    original = ("# Brief\n\n"
                + "".join("- item %02d\n" % i for i in range(10))
                + "\nAfter.\n")
    brief.write_bytes(original.encode("utf-8"))
    _cap(brief)
    assert brief.read_bytes() == original.encode("utf-8")


def test_cap_handles_numbered_lists(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("".join("%d. finding %d\n" % (i, i)
                             for i in range(1, 13)))
    _cap(brief)
    text = brief.read_text()
    assert "10. finding 10\n11. (+2 more suppressed)\n" in text
    assert "finding 11" not in text


def test_cap_continuations_follow_their_item(tmp_path):
    brief = tmp_path / "brief.md"
    items = []
    for i in range(12):
        items.append("- item %02d\n  detail for %02d\n" % (i, i))
    brief.write_text("".join(items))
    _cap(brief)
    text = brief.read_text()
    assert "  detail for 09\n" in text      # continuation under a kept item
    assert "detail for 10" not in text      # continuation under a suppressed one
    assert "- (+2 more suppressed)\n" in text


def test_cap_two_runs_capped_independently(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("".join("- a%02d\n" % i for i in range(4))
                     + "\n"
                     + "".join("- b%02d\n" % i for i in range(12)))
    _cap(brief)
    text = brief.read_text()
    assert "- a03\n" in text                 # short run untouched
    assert "- b09\n- (+2 more suppressed)" in text
    assert "- b10" not in text
