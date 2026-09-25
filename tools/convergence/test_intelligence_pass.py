import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

import intelligence_pass as ip

UTC = dt.timezone.utc
# 2026-09-25 08:00 America/Los_Angeles (inside the proposed 06:15-11:00 window).
NOW = dt.datetime(2026, 9, 25, 15, tzinfo=UTC)
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def receipt(date, *, mode="scheduled", outcome="coverage_gap", perspective="workflows", covers=None, **extra):
    base = {"schema": ip.SCHEMA, "date": date, "mode": mode, "covers_date": covers or date,
            "started_at": date + "T14:00:00Z", "finished_at": date + "T14:20:00Z",
            "allocation": ip.budget(ip.POLICY),
            "used": {"wall_seconds": 1200, "model_launches": 3, "source_opens": 9},
            "perspective": perspective, "questions": ["q1", "q2", "q3"],
            "coverage": {"internal": "complete", "external": "partial", "gaps": ["one source paywalled"]},
            "candidates": [], "outcome": outcome, "preempted_by": None, "recover_by": None}
    base.update(extra)
    return base


def candidate(**changes):
    c = {
        "dedupe_key": "batch-review-exports",
        "question": "Could review exports be batched so client feedback lands in one pass?",
        "perspective": "workflows",
        "goal_link": "Serves the delivery-speed priority by cutting review round trips.",
        "novelty": "Nobody has measured the round-trip cost; batching is not on the backlog.",
        "internal_evidence": [{"ref": "board:item-142", "observed_at": "2026-09-24T18:00:00Z"},
                              {"ref": "state/daily-scan/2026-09-24-brief.md", "observed_at": "2026-09-24T14:00:00Z"}],
        "external_evidence": [{"source": "https://example.org/batching-study", "published_or_accessed": "2026-08-01",
                               "claim": "Batched reviews cut cycle time in a published case study.",
                               "status": "verified"}],
        "counterevidence": ["Clients may prefer rolling feedback.", "Batching can delay urgent fixes."],
        "challenge": {"by": "claude-fable-5-1", "verdict": "survives", "notes": "Held up."},
        "generated_by": "gpt-5.6-sol",
        "proof": {"kind": "analysis", "ref": "state/intelligence/proofs/batching.md"},
        "recommendation": "Try batching on one project for two weeks and compare round trips.",
        "effort": "2 hours",
        "confidence": 0.6,
        "recheck_after": "2026-10-09",
        "decision_question": "Should one active project batch its review exports for two weeks, with round "
                             "trips counted before and after?",
        "why_now": "Two review rounds slipped this week; one project is starting a new cut on Monday.",
        "proof_body": "# Batching analysis\n\nRound trips last month: 14. Expected with batching: 6.\n",
    }
    c.update(changes)
    return c


# --------------------------------------------------------------------------- #
# A LOCAL re-implementation of the IRIS docket's propose rules (initiative_docket.py
# on vault origin/main, plus the opportunity_proposal additions in
# SPEC-iris-docket-discovery-origin.md). Deliberately copied, never imported.
# --------------------------------------------------------------------------- #
class DocketError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DocketError(code)


D_SHA = re.compile(r"[a-f0-9]{64}\Z")
D_OPP = re.compile(r"opportunity-[a-f0-9]{24}\Z")
D_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
D_PRIVATE = re.compile(r"(?:/Users/|/home/|file://|[A-Z]:\\|sk-[A-Za-z0-9]{12}|Bearer\s|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)
D_PHONE = re.compile(r"\+?\d[\d ()-]{8,}\d")


def d_clock(value):
    require(type(value) is str, "timestamp_required")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "aware_timestamp_required")
    return parsed.astimezone(UTC)


def d_shape(value, fields):
    require(type(value) is dict and set(value) == set(fields.split()), "invalid_fields")


def d_token(value):
    require(type(value) is str and D_TOKEN.fullmatch(value) is not None and ".." not in value, "invalid_reference")
    require(not value.startswith("/") and "//" not in value, "invalid_reference")


def d_safe_text(value, limit=1000):
    require(type(value) is str and 0 < len(value.strip()) <= limit and value == value.strip(), "invalid_text")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value), "unsafe_text")
    require(D_PRIVATE.search(value) is None, "private_text_refused")
    require(not any(sum(c.isdigit() for c in m.group()) >= 10 for m in D_PHONE.finditer(value)),
            "private_text_refused")


def d_reference(value):
    d_shape(value, "kind id")
    require(value["kind"] in {"notion_read", "artifact", "owner_task", "codex_message", "tool_ack"}, "invalid_reference_kind")
    d_token(value["id"])


def docket_accepts(value, now):
    d_shape(value, "event_id action_class source packet next_check_at origin")
    d_token(value["event_id"])
    require(value["action_class"] == "opportunity_proposal", "invalid_action_class")
    src = value["source"]
    d_shape(src, "work_item_id work_revision verified_at reference terminal")
    require(D_OPP.fullmatch(src["work_item_id"]) is not None, "opportunity_identity_mismatch")
    require(D_SHA.fullmatch(src["work_revision"]) is not None, "invalid_work_revision")
    require(type(src["terminal"]) is bool, "invalid_terminal")
    require(dt.timedelta(0) <= now - d_clock(src["verified_at"]) <= dt.timedelta(seconds=900), "source_recheck_not_current")
    d_reference(src["reference"])
    pk = value["packet"]
    d_shape(pk, "why_now recommendation question options authority_needed scope next_owner follow_through evidence_refs")
    for field in ("why_now", "recommendation", "question", "authority_needed", "scope", "next_owner", "follow_through"):
        d_safe_text(pk[field])
    require(type(pk["options"]) is list and 2 <= len(pk["options"]) <= 4, "invalid_options")
    ids = set()
    for option in pk["options"]:
        d_shape(option, "id label tradeoff")
        d_token(option["id"])
        require(option["id"] not in ids, "duplicate_option")
        ids.add(option["id"])
        d_safe_text(option["label"], 200)
        d_safe_text(option["tradeoff"], 500)
    require(type(pk["evidence_refs"]) is list and len(pk["evidence_refs"]) <= 5, "invalid_evidence_refs")
    for ref in pk["evidence_refs"]:
        d_reference(ref)
    require(dt.timedelta(0) < d_clock(value["next_check_at"]) - now <= dt.timedelta(days=30), "invalid_next_check")
    origin = value["origin"]
    d_shape(origin, "kind question goal_link novelty counterevidence proof_ref discovered_at")
    require(origin["kind"] == "independent_discovery", "invalid_origin_kind")
    for field in ("question", "goal_link", "novelty", "counterevidence"):
        d_safe_text(origin[field])
    d_reference(origin["proof_ref"])
    require(origin["proof_ref"]["kind"] == "artifact", "invalid_proof_ref")
    require(dt.timedelta(0) <= now - d_clock(origin["discovered_at"]) <= dt.timedelta(days=30), "invalid_discovered_at")
    require(len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()) <= 32768,
            "input_too_large")
    return True


def docket_event(seq, command, data, at="2026-09-20T17:00:00Z"):
    return {"sequence": seq, "at": at, "command": command, "input": data}


def source_for(c, revision=None):
    return {"work_item_id": ip.opportunity_id(c["dedupe_key"]),
            "work_revision": revision or ip.evidence_revision(c),
            "verified_at": "2026-09-20T16:59:00Z", "reference": {"kind": "artifact", "id": "x"}, "terminal": False}


class AllocationTest(unittest.TestCase):
    def test_busy_and_empty_backlog_both_run(self):
        for backlog in (500, 0):
            plan = ip.plan_pass([], NOW, backlog_count=backlog)
            self.assertTrue(plan["run"], backlog)
            self.assertEqual((plan["mode"], plan["reason"], plan["covers_date"]), ("scheduled", "due_today", "2026-09-25"))
            self.assertEqual(plan["reserved"]["present_max"], 1)
            self.assertIn(plan["perspective"], ip.PERSPECTIVES)

    def test_urgent_preemption_is_recorded_with_recover_by(self):
        plan = ip.plan_pass([], NOW, backlog_count=3, urgent="incident-42")
        self.assertEqual((plan["run"], plan["mode"], plan["reason"], plan["preempted_by"]),
                         (False, "skip", "preempted_by_incident", "incident-42"))
        # Next local day's window start: 2026-09-26 06:15 PDT == 13:15Z.
        self.assertEqual(plan["recover_by"], "2026-09-26T13:15:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ip.main(["plan", "--dir", tmp + "/intel", "--urgent", "incident-42", "--record-preemption",
                              "--now", iso(NOW)])
            self.assertEqual(rc, 4)
            written = json.loads(Path(tmp, "intel", "2026-09-25.json").read_text())
            self.assertEqual((written["outcome"], written["mode"], written["preempted_by"]),
                             ("preempted", "skip", "incident-42"))
            history, _ = ip.load_history(tmp + "/intel")
        again = ip.plan_pass(history, NOW + dt.timedelta(hours=1), backlog_count=0)
        self.assertEqual((again["run"], again["reason"]), (False, "preempted_today"))
        tomorrow = ip.plan_pass(history, NOW + dt.timedelta(days=1), backlog_count=0)
        # The preempted day is listed as missed; the next pass still covers its own day.
        self.assertEqual((tomorrow["run"], tomorrow["mode"], tomorrow["covers_date"], tomorrow["gap_dates"]),
                         (True, "scheduled_after_gap", "2026-09-26", ["2026-09-25"]))
        self.assertEqual(tomorrow["reason"], "due_today_after_gap")

    def test_pass_after_gap_covers_today_lists_missed_days_and_one_pass_per_day(self):
        history = [receipt("2026-09-21")]  # 22, 23 and 24 missed
        plan = ip.plan_pass(history, NOW, backlog_count=0)
        self.assertEqual((plan["mode"], plan["covers_date"], plan["gap_dates"]),
                         ("scheduled_after_gap", "2026-09-25", ["2026-09-22", "2026-09-23", "2026-09-24"]))
        history.append(receipt("2026-09-25", mode="scheduled_after_gap", gap_dates=plan["gap_dates"]))
        same_day = ip.plan_pass(history, NOW + dt.timedelta(hours=2), backlog_count=0)
        self.assertEqual((same_day["run"], same_day["reason"]), (False, "already_ran_today"))
        next_day = ip.plan_pass(history, NOW + dt.timedelta(days=1), backlog_count=0)
        self.assertEqual((next_day["run"], next_day["mode"], next_day["gap_dates"]), (True, "scheduled", []))
        # Never backdated: every pass covers its own day, and gap_dates must match the mode.
        with self.assertRaisesRegex(ValueError, "pass_must_cover_its_own_day"):
            ip.finalize_receipt(receipt("2026-09-25", mode="scheduled_after_gap", covers="2026-09-24",
                                        gap_dates=["2026-09-24"]))
        with self.assertRaisesRegex(ValueError, "gap_dates_do_not_match_mode"):
            ip.finalize_receipt(receipt("2026-09-25", mode="scheduled_after_gap"))
        with self.assertRaisesRegex(ValueError, "gap_dates_do_not_match_mode"):
            ip.finalize_receipt(receipt("2026-09-25", gap_dates=["2026-09-24"]))
        with self.assertRaisesRegex(ValueError, "invalid_gap_dates"):
            ip.finalize_receipt(receipt("2026-09-25", mode="scheduled_after_gap", gap_dates=["2026-09-25"]))

    def test_failed_day_stays_in_the_gap_until_a_pass_completes(self):
        history = [receipt("2026-09-22"), receipt("2026-09-24", outcome="failed")]  # 23 missed, 24 failed
        plan = ip.plan_pass(history, NOW, backlog_count=0)
        self.assertEqual(plan["gap_dates"], ["2026-09-23", "2026-09-24"])
        self.assertEqual(ip.plan_pass([], NOW, backlog_count=0)["mode"], "scheduled")  # first pass ever

    def test_gap_lookback_days_and_its_former_name(self):
        history = [receipt("2026-09-15", outcome="coverage_gap")]
        default = ip.plan_pass(history, NOW, backlog_count=0)
        self.assertEqual(len(default["gap_dates"]), 7)
        for policy in ({"gap_lookback_days": 2}, {"recovery_lookback_days": 2},
                       {"gap_lookback_days": 2, "recovery_lookback_days": 5}):
            plan = ip.plan_pass(history, NOW, backlog_count=0, policy=policy)
            self.assertEqual(plan["gap_dates"], ["2026-09-23", "2026-09-24"], policy)
        self.assertNotIn("recovery_lookback_days", ip.merged({"recovery_lookback_days": 3}))
        self.assertEqual(ip.merged({"recovery_lookback_days": 3})["gap_lookback_days"], 3)

    def test_window_bounds(self):
        early = dt.datetime(2026, 9, 25, 12, tzinfo=UTC)  # 05:00 PDT
        late = dt.datetime(2026, 9, 25, 19, tzinfo=UTC)   # 12:00 PDT
        self.assertEqual(ip.plan_pass([], early, backlog_count=0)["reason"], "before_window")
        closed = ip.plan_pass([], late, backlog_count=0)
        self.assertEqual((closed["run"], closed["reason"]), (False, "window_closed"))
        self.assertTrue(ip.plan_pass([], late, backlog_count=0, ignore_window=True)["run"])
        with self.assertRaisesRegex(ValueError, "invalid_backlog_count"):
            ip.plan_pass([], NOW, backlog_count=-1)

    def test_missed_day_detection(self):
        history = [receipt("2026-09-21"), receipt("2026-09-23", mode="skip", outcome="preempted", perspective=None,
                                                   allocation={}, preempted_by="inc-1",
                                                   recover_by="2026-09-24T13:15:00Z")]
        self.assertEqual(ip.missed_days(history, NOW, "2026-09-20"), ["2026-09-20", "2026-09-22", "2026-09-24"])

    def test_perspective_rotation_never_repeats_previous_three(self):
        history, day = [], dt.date(2026, 9, 1)
        for _ in range(40):
            choice = ip.choose_perspective(day, history)
            self.assertEqual(choice, ip.choose_perspective(day.isoformat(), history))  # deterministic
            self.assertNotIn(choice, [r["perspective"] for r in history[-3:]])
            history.append(receipt(day.isoformat(), perspective=choice))
            day += dt.timedelta(days=1)
        self.assertEqual(len({r["perspective"] for r in history}), len(ip.PERSPECTIVES))


class CandidateTest(unittest.TestCase):
    def test_valid_candidate(self):
        self.assertEqual(ip.validate_candidate(candidate()), [])

    def test_missing_counterevidence_or_challenge_or_self_challenge_is_invalid(self):
        for field in ("counterevidence", "challenge"):
            c = candidate()
            del c[field]
            self.assertIn("missing_field:" + field, ip.validate_candidate(c))
        self.assertIn("counterevidence_required", ip.validate_candidate(candidate(counterevidence=[])))
        same = candidate(challenge={"by": "gpt-5.6-sol", "verdict": "survives", "notes": ""})
        self.assertIn("challenger_is_generator", ip.validate_candidate(same))
        refuted = candidate(challenge={"by": "claude-fable-5-1", "verdict": "refuted", "notes": "no"})
        self.assertIn("challenge_refuted", ip.validate_candidate(refuted))

    def test_ratified_and_preference_keys_refused(self):
        self.assertIn("forbidden_key:ratified_preference", ip.validate_candidate(candidate(ratified_preference="x")))
        nested = candidate(proof={"kind": "analysis", "ref": "a", "preference_note": "x"})
        reasons = ip.validate_candidate(nested)
        self.assertIn("forbidden_key:proof.preference_note", reasons)
        self.assertIn("invalid_proof", reasons)

    def test_shape_rules(self):
        c = candidate(external_gap="no network")
        self.assertIn("external_evidence_and_gap_both_present", ip.validate_candidate(c))
        c = candidate()
        del c["external_evidence"]
        self.assertIn("external_evidence_or_gap_required", ip.validate_candidate(c))
        self.assertIn("experiment_required",
                      ip.validate_candidate(candidate(proof={"kind": "prototype", "ref": "p"})))
        self.assertIn("private_text:goal_link",
                      ip.validate_candidate(candidate(goal_link="See /Users/someone/notes.md")))
        self.assertIn("invalid_dedupe_key", ip.validate_candidate(candidate(dedupe_key="Not A Slug")))
        self.assertIn("invalid_confidence", ip.validate_candidate(candidate(confidence=True)))

    def test_novelty_near_duplicate_backlog_item_is_not_novel(self):
        corpus = [{"id": "backlog-7", "kind": "backlog",
                   "text": "Batch the review exports so client feedback lands in one pass; try batching on one "
                           "project for two weeks and compare round trips."},
                  {"id": "radar-1", "kind": "radar", "text": "New vector database release notes."}]
        result = ip.novelty_check(candidate(), corpus)
        self.assertFalse(result["novel"])
        self.assertEqual(result["nearest"][0][0], "backlog-7")
        self.assertTrue(ip.novelty_check(candidate(), corpus[1:])["novel"])

    def test_identity_is_stable(self):
        expected = "opportunity-" + hashlib.sha256(b"batch-review-exports").hexdigest()[:24]
        self.assertEqual(ip.opportunity_id("batch-review-exports"), expected)
        self.assertEqual(ip.opportunity_id("batch-review-exports"), ip.opportunity_id("batch-review-exports"))
        reordered = candidate(internal_evidence=list(reversed(candidate()["internal_evidence"])))
        reobserved = candidate(internal_evidence=[dict(i, observed_at="2026-09-25T01:00:00Z")
                                                  for i in candidate()["internal_evidence"]])
        self.assertEqual(ip.evidence_revision(reordered), ip.evidence_revision(candidate()))
        self.assertEqual(ip.evidence_revision(reobserved), ip.evidence_revision(candidate()))
        # Model-written prose is not evidence identity.
        self.assertEqual(ip.evidence_revision(candidate(counterevidence=["new"])), ip.evidence_revision(candidate()))
        added = candidate(internal_evidence=candidate()["internal_evidence"]
                          + [{"ref": "board:item-999", "observed_at": "2026-09-25T01:00:00Z"}])
        self.assertNotEqual(ip.evidence_revision(added), ip.evidence_revision(candidate()))

    def test_identity_normalisation(self):
        same = [
            (ip.internal_identity, "board:8c7450d0,", "board:8c7450d0"),
            (ip.internal_identity, "board:8c7450d0.", "board:8c7450d0"),
            (ip.internal_identity, "board:8c7450d0; next", "board:8c7450d0"),
            (ip.internal_identity, "board:8c7450d0: overdue", "board:8c7450d0"),
            (ip.internal_identity, "state/daily-scan/", "state/daily-scan"),
            (ip.internal_identity, "state/daily-scan/, see brief", "state/daily-scan"),
            (ip.internal_identity, "https://Example.org/Path/?a=1#frag, ok", "https://example.org/path"),
            (ip.external_identity, ext("x", "https://example.org/a/"), "https://example.org/a"),
            (ip.external_identity, ext("x", "https://example.org/a?utm_source=feed"), "https://example.org/a"),
            (ip.external_identity, ext("x", "https://example.org/a#section-2"), "https://example.org/a"),
            (ip.external_identity, ext("x", "https://example.org/a/?q=1#f"), "https://example.org/a"),
            (ip.external_identity, ext("https://example.org/a/?ref=rss"), "https://example.org/a"),
            (ip.external_identity, ext("Some Publisher Weekly."), "some publisher weekly"),
            (ip.external_identity, ext("Some Publisher Weekly;"), "some publisher weekly"),
        ]
        for fn, raw, expected in same:
            self.assertEqual(fn(raw), expected, raw)
        self.assertIsNone(ip.internal_identity(",.;"))
        self.assertNotEqual(ip.external_identity(ext("x", "https://example.org/a/b")),
                            ip.external_identity(ext("x", "https://example.org/a")))
        base = candidate(internal_evidence=[ev("board:item-142"), ev("state/notes/")],
                         external_evidence=[ext("x", "https://example.org/study")])
        variant = candidate(internal_evidence=[ev("board:item-142, the item"), ev("state/notes; section 2")],
                            external_evidence=[ext("x", "https://example.org/study/?utm_campaign=a#results")])
        self.assertEqual(ip.evidence_revision(variant), ip.evidence_revision(base))
        self.assertEqual(ip.evidence_tokens(variant), ip.evidence_tokens(base))

    def test_identity_normalisation_variants(self):
        # Every spelling in a row must collapse to the row's identity.
        internal = {
            "board:item-142": ["board:item-142", "(board:item-142)", "`board:item-142`", '"board:item-142"',
                               "'board:item-142'", "[board:item-142]", "{board:item-142}", "<board:item-142>",
                               "\u201cboard:item-142\u201d", "board:item-142).", "board:item-142!",
                               "board:item-142?", "board:item-142\u2026", "board:item-142...",
                               "(board:item-142), see note", "  board:item-142  "],
            "tools/convergence/intelligence_pass.py": [
                "./tools/convergence/intelligence_pass.py", "tools/convergence/intelligence_pass.py#anchor",
                "tools/convergence/intelligence_pass.py:12", "tools/convergence/intelligence_pass.py:12:5",
                "tools/convergence/intelligence_pass.py:12-20", "./tools/convergence/intelligence_pass.py#L12).",
                "(tools/convergence/intelligence_pass.py:12)", "`./tools/convergence/intelligence_pass.py`"],
            "readme.md": ["README.md", "README.md:12", "README.md#setup", "./README.md"],
            "https://example.org/a b": [
                "https://example.org/a%20b", "http://example.org/a%20b", "https://www.example.org/a%20b",
                "https://example.org:443/a%20b", "http://example.org:80/a%20b", "https://example.org/a%20b;jsessionid=9",
                "<https://example.org/a%20b>", "(https://example.org/a%20b).", "HTTPS://WWW.Example.org/A%20B/",
                "https://example.org/a%20b?utm_source=x#frag"],
            "https://en.wikipedia.org/wiki/foo_(bar)": [
                "https://en.wikipedia.org/wiki/Foo_(bar)", "(https://en.wikipedia.org/wiki/Foo_(bar)).",
                "<https://en.wikipedia.org/wiki/Foo_%28bar%29>"],
        }
        for expected, spellings in internal.items():
            for raw in spellings:
                self.assertEqual(ip.internal_identity(raw), expected, raw)
        external = {
            "https://example.org/study": [
                ext("x", "https://example.org/study"), ext("x", "http://example.org/study"),
                ext("x", "https://www.example.org/study/"), ext("x", "https://example.org:443/study"),
                ext("x", "http://www.example.org:80/study;v=2?ref=rss#f"), ext("x", "<https://example.org/study>"),
                ext("x", "https://example.org/%73tudy"), ext("x", "(https://example.org/study)."),
                ext("<https://www.example.org/study>"), ext("http://example.org/study).")],
            "some publisher weekly": [
                ext("Some Publisher Weekly"), ext('"Some Publisher Weekly"'), ext("(Some Publisher Weekly)."),
                ext("Some  Publisher Weekly!"), ext("`Some Publisher Weekly`"), ext("Some Publisher Weekly\u2026")],
        }
        for expected, items in external.items():
            for item in items:
                self.assertEqual(ip.external_identity(item), expected, item)
        # A source-only item keeps the whole normalised source, not its first token.
        self.assertEqual(ip.external_identity(ext("Some Publisher Weekly")), "some publisher weekly")
        # Distinct identities stay distinct.
        distinct = [ip.internal_identity(r) for r in ("board:item-142", "board:item-143", "board:142", "board:143",
                                                       "board:item#3", "tools/a.py", "tools/b.py")]
        self.assertEqual(len(set(distinct)), len(distinct), distinct)
        self.assertEqual(ip.internal_identity("board:142"), "board:142")  # not path-like: no line suffix
        self.assertNotEqual(ip.external_identity(ext("x", "https://example.org/a/b")),
                            ip.external_identity(ext("x", "https://example.org/a")))
        self.assertNotEqual(ip.external_identity(ext("x", "https://example.org:8443/a")),
                            ip.external_identity(ext("x", "https://example.org/a")))
        for empty in ("", "()", '""', "`", "<>", "...", "\u2026"):
            self.assertIsNone(ip.internal_identity(empty), empty)
        # Respelled evidence keeps the same revision and tokens, so suppression holds.
        base = candidate(internal_evidence=[ev("board:item-142"), ev("tools/convergence/intelligence_pass.py")],
                         external_evidence=[ext("x", "https://example.org/study")])
        respelled = candidate(internal_evidence=[ev("(board:item-142)."),
                                                 ev("./tools/convergence/intelligence_pass.py:88")],
                              external_evidence=[ext("x", "<http://www.example.org:443/study;s=1>")])
        self.assertEqual(ip.evidence_revision(respelled), ip.evidence_revision(base))
        self.assertEqual(ip.evidence_tokens(respelled), ip.evidence_tokens(base))

    def test_evidence_identities(self):
        c = candidate(internal_evidence=[{"ref": "Board:8C7450D0 the finder item, next action overdue",
                                          "observed_at": "2026-09-24T18:00:00Z"},
                                         {"ref": "board:8c7450d0", "observed_at": "2026-09-24T19:00:00Z"}],
                      external_evidence=[{"source": "  Some   Publisher\tWeekly  ", "published_or_accessed": "2026-08-01",
                                          "claim": "x", "status": "verified"},
                                         {"source": "Anything", "url": "https://example.org/a",
                                          "published_or_accessed": "2026-08-01", "claim": "y", "status": "unverified"}])
        self.assertEqual(ip.validate_candidate(c), [])
        self.assertEqual(ip.evidence_identities(c),
                         (["board:8c7450d0"], ["some publisher weekly", "https://example.org/a"]))
        long_source = candidate(external_evidence=[{"source": "A" * 200, "published_or_accessed": "2026-08-01",
                                                    "claim": "x", "status": "verified"}])
        self.assertEqual(ip.evidence_identities(long_source)[1], ["a" * 80])
        bad_url = candidate(external_evidence=[{"source": "s", "url": "not a url", "published_or_accessed": "2026-08-01",
                                                "claim": "x", "status": "verified"}])
        self.assertIn("invalid_external_evidence", ip.validate_candidate(bad_url))


PROPOSED_AT = dt.datetime(2026, 9, 20, 17, tzinfo=UTC)


def proposal_for(c):
    """The real propose input this module would have handed the docket for ``c``."""
    return ip.to_docket_propose(c, now=PROPOSED_AT, event_id="e-" + ip.evidence_revision(c)[:8],
                                verified_at=PROPOSED_AT - dt.timedelta(minutes=1))


def resolved_docket(*cs, disposition="declined", refs=None):
    """Each candidate proposed (its real packet) and then resolved, in order."""
    key = "initiative-" + "0" * 32
    events = []
    for c in cs:
        proposal = proposal_for(c)
        if refs is not None:
            proposal["packet"]["evidence_refs"] = refs
        events.append(docket_event(len(events) + 1, "propose", proposal))
        events.append(docket_event(len(events) + 1, "respond", {
            "event_id": "r%d" % len(events), "action_key": key, "proposal_revision": "0" * 64, "response_ref": {},
            "disposition": disposition, "option_id": None, "source": proposal["source"]}))
    return {"schema": "iris-initiative-docket/v1", "generation": len(events), "events": events}


def ev(ref):
    return {"ref": ref, "observed_at": "2026-09-24T18:00:00Z"}


def ext(source, url=None):
    item = {"source": source, "published_or_accessed": "2026-08-01", "claim": "c", "status": "verified"}
    if url:
        item["url"] = url
    return item


class SuppressionTest(unittest.TestCase):
    def events(self, c, disposition="declined"):
        return resolved_docket(c, disposition=disposition)

    def test_same_evidence_decline_suppressed_changed_evidence_eligible(self):
        c = candidate()
        docket = self.events(c)
        self.assertEqual(ip.suppression(c, docket, NOW)["status"], "suppressed")
        reworded = candidate(recommendation="Entirely different wording of the same idea.")
        self.assertEqual(ip.suppression(reworded, docket, NOW)["status"], "suppressed")
        changed = candidate(external_evidence=candidate()["external_evidence"] + [
            {"source": "A new contrary study", "url": "https://example.org/contrary", "published_or_accessed":
             "2026-09-20", "claim": "Batching raised defect rates.", "status": "verified"}])
        self.assertEqual(ip.suppression(changed, docket, NOW)["status"], "eligible_changed_evidence")

    def test_rewording_the_same_evidence_stays_suppressed_after_decline(self):
        c = candidate()
        docket = self.events(c)
        reworded = candidate(
            counterevidence=["Some clients like feedback to arrive continuously.", "Urgent fixes could wait longer."],
            internal_evidence=[{"ref": "BOARD:item-142 (review exports item, still open)", "observed_at":
                                "2026-09-25T09:00:00Z"},
                               {"ref": "state/daily-scan/2026-09-24-brief.md  (section on reviews)",
                                "observed_at": "2026-09-25T09:00:00Z"}],
            external_evidence=[{"source": "https://example.org/batching-study", "published_or_accessed": "2026-09-25",
                                "claim": "A case study reports shorter cycles when reviews are batched.",
                                "status": "unverified"}],
            question="Would batching review exports reduce client round trips?",
            novelty="Differently worded novelty.", why_now="Differently worded why now.")
        self.assertEqual(ip.evidence_revision(reworded), ip.evidence_revision(c))
        self.assertEqual(ip.suppression(reworded, docket, NOW)["status"], "suppressed")
        plan = ip.plan_pass([], NOW, backlog_count=0)
        draft = ip.assemble_draft(plan, {"questions": ["a"], "used": {"source_opens": 1},
                                         "coverage": {"internal": "complete", "external": "complete", "gaps": []},
                                         "candidates": [reworded]}, {0: {"verdict": "survives", "notes": ""}},
                                  generator="gpt-5.6-sol", challenger="claude-fable-5-1", started_at=NOW, now=NOW,
                                  model_launches=2)
        settled, proposals = ip.settle(draft, now=NOW, corpus=[], docket=docket)
        self.assertEqual(([r["status"] for r in settled["candidates"]], proposals), (["suppressed"], []))
        new_identity = candidate(internal_evidence=reworded["internal_evidence"]
                                 + [{"ref": "board:item-777 a new related item", "observed_at": "2026-09-25T09:00:00Z"}])
        self.assertEqual(ip.suppression(new_identity, docket, NOW)["status"], "eligible_changed_evidence")
        self.assertEqual(ip.suppression(candidate(dedupe_key="other-idea"), docket, NOW)["status"], "eligible")

    def test_only_a_new_identity_resurfaces_a_declined_opportunity(self):
        c = candidate()
        for disposition in ("declined", "answered"):
            docket = self.events(c, disposition)
            removed = candidate(internal_evidence=c["internal_evidence"][:1])
            verdict = ip.suppression(removed, docket, NOW)
            self.assertEqual((verdict["status"], verdict["reason"]), ("suppressed", "no_new_evidence_identity"))
            reordered = candidate(internal_evidence=list(reversed(c["internal_evidence"])),
                                  external_evidence=list(reversed(c["external_evidence"])))
            self.assertEqual(ip.suppression(reordered, docket, NOW)["reason"], "same_evidence_as_resolved_opportunity")
            swapped = dict(candidate(external_gap="No external source could be opened today."))
            del swapped["external_evidence"]
            self.assertEqual(ip.validate_candidate(swapped), [])
            verdict = ip.suppression(swapped, docket, NOW)
            self.assertEqual((verdict["status"], verdict["reason"]), ("suppressed", "no_new_evidence_identity"))
            # Removing some identities while re-spelling the rest still adds nothing new.
            respelled = candidate(internal_evidence=[ev("BOARD:item-142, the review item")],
                                  external_evidence=[ext("https://Example.org/batching-study/?utm=x#top")])
            self.assertEqual(ip.suppression(respelled, docket, NOW)["reason"], "no_new_evidence_identity")
            added = candidate(internal_evidence=c["internal_evidence"][:1] + [ev("board:item-900 new")])
            verdict = ip.suppression(added, docket, NOW)
            self.assertEqual((verdict["status"], verdict["new_identities"]),
                             ("eligible_changed_evidence", ["board:item-900"]))

    def test_followed_through_opportunity_needs_a_new_identity_too(self):
        c = candidate()
        proposal = proposal_for(c)
        docket = {"events": [docket_event(1, "propose", proposal),
                             docket_event(2, "follow-through", {"event_id": "f", "outcome": "recorded",
                                                                "source": proposal["source"]})]}
        fewer = candidate(internal_evidence=c["internal_evidence"][:1])
        self.assertEqual(ip.suppression(fewer, docket, NOW)["status"], "suppressed")
        more = candidate(external_evidence=c["external_evidence"] + [ext("New Journal")])
        self.assertEqual(ip.suppression(more, docket, NOW)["status"], "eligible_changed_evidence")

    def test_identity_must_be_new_against_every_resolved_proposal(self):
        first = candidate(internal_evidence=[ev("board:a"), ev("board:b")], external_evidence=[ext("Source One")])
        second = candidate(internal_evidence=[ev("board:c"), ev("board:d")], external_evidence=[ext("Source Two")])
        docket = resolved_docket(first, second)
        mixed = candidate(internal_evidence=[ev("board:a"), ev("board:c")], external_evidence=[ext("Source Two")])
        self.assertEqual(ip.suppression(mixed, docket, NOW)["reason"], "no_new_evidence_identity")
        fresh = candidate(internal_evidence=[ev("board:a"), ev("board:e")], external_evidence=[ext("Source Two")])
        self.assertEqual(ip.suppression(fresh, docket, NOW)["new_identities"], ["board:e"])

    def test_unknown_or_truncated_prior_identities_stay_suppressed_unless_the_ledger_knows(self):
        c = candidate()
        grown = candidate(internal_evidence=c["internal_evidence"] + [ev("board:item-900")])
        # A resolved proposal whose packet carries no evidence refs: nothing can be shown to be new.
        unknown = resolved_docket(c, refs=[])
        unknown["events"][0]["input"]["packet"].pop("evidence_refs")
        verdict = ip.suppression(grown, unknown, NOW)
        self.assertEqual((verdict["status"], verdict["reason"]), ("suppressed", "prior_evidence_identities_unknown"))
        ledger = {ip.evidence_revision(c): ip.evidence_tokens(c)}
        self.assertEqual(ip.suppression(grown, unknown, NOW, ledger)["status"], "eligible_changed_evidence")
        # A packet holds at most five refs, so five visible refs may hide a sixth identity.
        six = candidate(internal_evidence=[ev("board:i%d" % i) for i in range(5)], external_evidence=[ext("Six")])
        self.assertEqual(len(proposal_for(six)["packet"]["evidence_refs"]), 5)
        docket = resolved_docket(six)
        same_six = candidate(internal_evidence=[ev("board:i%d" % i) for i in range(1, 5)],
                             external_evidence=[ext("six.")])
        self.assertEqual(ip.suppression(same_six, docket, NOW)["reason"], "prior_evidence_identities_unknown")
        ledger = {ip.evidence_revision(six): ip.evidence_tokens(six)}
        self.assertEqual(ip.suppression(same_six, docket, NOW, ledger)["reason"], "no_new_evidence_identity")
        seventh = candidate(internal_evidence=[ev("board:i0"), ev("board:i9")], external_evidence=[ext("Six")])
        self.assertEqual(ip.suppression(seventh, docket, NOW, ledger)["status"], "eligible_changed_evidence")

    def test_settle_reports_the_suppression_reason(self):
        c = candidate()
        fewer = candidate(internal_evidence=c["internal_evidence"][:1])
        settled, proposals = ReceiptTest.settle_one(self, fewer, corpus=[], docket=self.events(c))
        self.assertEqual((settled["candidates"][0]["status"], settled["candidates"][0]["reasons"], proposals),
                         ("suppressed", ["no_new_evidence_identity"], []))

    def test_future_snooze_holds(self):
        c = candidate()
        docket = {"events": [docket_event(1, "snooze", {"event_id": "s", "until": "2026-10-01T00:00:00Z",
                                                        "source": source_for(c)})]}
        self.assertEqual(ip.suppression(c, docket, NOW)["status"], "snoozed")
        self.assertEqual(ip.suppression(c, docket, dt.datetime(2026, 10, 2, tzinfo=UTC))["status"], "eligible")


class HandoffTest(unittest.TestCase):
    def test_propose_passes_docket_rules_with_exactly_four_options(self):
        c = candidate(counterevidence=["x" * 700, "y" * 700])
        p = ip.to_docket_propose(c, now=NOW, event_id="fa-intelligence-2026-09-25-a", verified_at=NOW)
        self.assertTrue(docket_accepts(p, NOW + dt.timedelta(minutes=5)))
        self.assertEqual([o["id"] for o in p["packet"]["options"]], ["proceed", "modify", "defer", "decline"])
        self.assertTrue(p["packet"]["why_now"].startswith("Independent discovery:"))
        self.assertEqual(p["source"]["work_item_id"], ip.opportunity_id(c["dedupe_key"]))
        self.assertEqual(p["source"]["work_revision"], ip.evidence_revision(c))
        self.assertLessEqual(len(p["origin"]["counterevidence"]), 1000)
        for ref in p["packet"]["evidence_refs"] + [p["origin"]["proof_ref"]]:
            self.assertNotIn("/", ref["id"])  # paths and URLs become short hashed tokens
        self.assertEqual(p["next_check_at"], "2026-10-09T16:00:00Z")  # recheck_after 09:00 PDT
        far = ip.to_docket_propose(candidate(recheck_after="2027-06-01"), now=NOW, event_id="e", verified_at=NOW)
        self.assertEqual(ip.instant(far["next_check_at"]) - NOW, dt.timedelta(days=30))

    def test_packet_asks_the_specific_decision_and_says_why_now(self):
        c = candidate()
        p = ip.to_docket_propose(c, now=NOW, event_id="e", verified_at=NOW)
        self.assertEqual(p["packet"]["question"], c["decision_question"])
        self.assertEqual(p["packet"]["why_now"], "Independent discovery: " + c["why_now"])
        self.assertNotIn("independently discovered opportunity proceed as prepared", p["packet"]["question"])
        self.assertEqual([o["id"] for o in p["packet"]["options"]], ["proceed", "modify", "defer", "decline"])
        for field in ("decision_question", "why_now"):
            missing = candidate()
            del missing[field]
            self.assertIn("missing_field:" + field, ip.validate_candidate(missing))
            self.assertIn("text_too_long:" + field, ip.validate_candidate(candidate(**{field: "x" * 1001})))

    def test_discovered_at_uses_the_candidate_time_when_valid(self):
        earlier = "2026-09-24T20:00:00Z"
        p = ip.to_docket_propose(candidate(discovered_at=earlier), now=NOW, event_id="e", verified_at=NOW)
        self.assertEqual(p["origin"]["discovered_at"], earlier)
        for bad in ("2026-09-26T00:00:00Z", "2026-07-01T00:00:00Z", "yesterday", 5, None):
            p = ip.to_docket_propose(candidate(discovered_at=bad), now=NOW, event_id="e", verified_at=NOW)
            self.assertEqual(p["origin"]["discovered_at"], iso(NOW), bad)
            self.assertTrue(docket_accepts(p, NOW))

    def test_private_or_invalid_candidate_never_reaches_the_docket(self):
        with self.assertRaisesRegex(ValueError, "private_text"):
            ip.to_docket_propose(candidate(novelty="Email someone@example.com"), now=NOW, event_id="e", verified_at=NOW)
        with self.assertRaisesRegex(ValueError, "invalid_candidate"):
            ip.to_docket_propose(candidate(challenge={"by": "gpt-5.6-sol", "verdict": "survives", "notes": ""}),
                                 now=NOW, event_id="e", verified_at=NOW)


class ReceiptTest(unittest.TestCase):
    def test_no_qualifying_opportunity_with_partial_coverage_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires_complete_coverage"):
            ip.finalize_receipt(receipt("2026-09-25", outcome="no_qualifying_opportunity"))
        complete = receipt("2026-09-25", outcome="no_qualifying_opportunity",
                           coverage={"internal": "complete", "external": "complete", "gaps": []})
        self.assertEqual(ip.finalize_receipt(complete)["outcome"], "no_qualifying_opportunity")

    def test_write_receipt_is_private_atomic_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "intelligence"
            path = ip.write_receipt(target, receipt("2026-09-25"))
            self.assertEqual(path.name, "2026-09-25.json")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, "receipt_exists"):
                ip.write_receipt(target, receipt("2026-09-25", outcome="failed"))
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["2026-09-25.json"])
            with self.assertRaisesRegex(ValueError, "parent_missing"):
                ip.write_receipt(Path(tmp) / "missing" / "intelligence", receipt("2026-09-25"))

    def test_settle_holds_suppresses_and_caps(self):
        plan = ip.plan_pass([], NOW, backlog_count=0)
        output = {"questions": ["a", "b", "c"], "used": {"source_opens": 4},
                  "coverage": {"internal": "complete", "external": "complete", "gaps": []},
                  "candidates": [dict(candidate(), challenge={"by": "self", "verdict": "survives", "notes": ""}),
                                 candidate(dedupe_key="second-idea", question="Would a weekly digest of drafts help?",
                                           recommendation="Draft one weekly digest and compare.")]}
        challenges = {0: {"verdict": "survives", "notes": "ok"}, 1: {"verdict": "weakened", "notes": "cost"}}
        draft = ip.assemble_draft(plan, json.dumps(output), challenges, generator="gpt-5.6-sol",
                                  challenger="claude-fable-5-1", started_at=NOW, now=NOW + dt.timedelta(minutes=10),
                                  model_launches=3)
        self.assertEqual(draft["candidates"][0]["challenge"]["by"], "claude-fable-5-1")  # self-challenge discarded
        receipt_value, proposals = ip.settle(draft, now=NOW, docket={"events": []})
        self.assertEqual([r["status"] for r in receipt_value["candidates"]], ["presented_to_outbox", "held"])
        self.assertEqual(receipt_value["candidates"][1]["reasons"], ["present_max_reached"])
        self.assertEqual((receipt_value["outcome"], len(proposals)), ("opportunities_prepared", 1))
        self.assertEqual(receipt_value["used"], {"wall_seconds": 600, "model_launches": 3, "source_opens": 4})
        held, none = ip.settle(draft, now=NOW, docket_problem="unreadable")
        self.assertEqual({r["status"] for r in held["candidates"]}, {"held"})
        self.assertEqual((held["outcome"], held["coverage"]["internal"], none), ("coverage_gap", "partial", []))
        failed = ip.assemble_draft(plan, "not json", {}, generator="g", challenger="c", started_at=NOW, now=NOW,
                                   model_launches=1)
        self.assertEqual(ip.settle(failed, now=NOW)[0]["outcome"], "failed")

    def settle_one(self, c, **kwargs):
        plan = ip.plan_pass([], NOW, backlog_count=0)
        output = {"questions": ["a"], "used": {"source_opens": 1},
                  "coverage": {"internal": "complete", "external": "complete", "gaps": []}, "candidates": [c]}
        draft = ip.assemble_draft(plan, output, {0: {"verdict": "survives", "notes": ""}}, generator="gpt-5.6-sol",
                                  challenger="claude-fable-5-1", started_at=NOW, now=NOW, model_launches=2)
        return ip.settle(draft, now=NOW, **kwargs)

    def test_candidate_without_proof_body_is_held_and_presented_proof_ref_is_the_opportunity(self):
        c = candidate()
        del c["proof_body"]
        settled, proposals = self.settle_one(c, corpus=[], docket={"events": []})
        self.assertEqual((settled["candidates"][0]["status"], settled["candidates"][0]["reasons"], proposals),
                         ("held", ["proof_missing"], []))
        self.assertEqual(settled["outcome"], "no_qualifying_opportunity")
        settled, proposals = self.settle_one(candidate(proof={"kind": "analysis"}), corpus=[], docket={"events": []})
        oid = ip.opportunity_id("batch-review-exports")
        self.assertEqual(proposals[0]["source"]["reference"], {"kind": "artifact", "id": oid})
        self.assertEqual(proposals[0]["origin"]["proof_ref"], {"kind": "artifact", "id": oid})
        self.assertIn("invalid_proof_body", ip.validate_candidate(candidate(proof_body="x" * (20 * 1024 + 1))))
        self.assertIn("invalid_proof_body", ip.validate_candidate(candidate(proof_body="  ")))
        # Private-text rules do not apply to the local proof file.
        self.assertEqual(ip.validate_candidate(candidate(proof_body="See /Users/someone/notes.md")), [])

    def test_corpus_gaps_lower_internal_coverage(self):
        settled, _ = self.settle_one(candidate(), corpus=[], corpus_gaps=["board: unavailable (URLError)"],
                                     docket={"events": []})
        self.assertEqual(settled["coverage"]["internal"], "partial")
        self.assertIn("novelty_corpus_gap: board: unavailable (URLError)", settled["coverage"]["gaps"])
        settled, _ = self.settle_one(candidate(), docket={"events": []})
        self.assertEqual(settled["coverage"]["internal"], "partial")
        self.assertIn("novelty_corpus_not_built", settled["coverage"]["gaps"])
        settled, _ = self.settle_one(candidate(), corpus=[], docket={"events": []})
        self.assertEqual(settled["coverage"]["internal"], "complete")

    def test_allocation_excess_requires_a_reason(self):
        within = ip.finalize_receipt(receipt("2026-09-25"))
        self.assertEqual((within["allocation_exceeded"], within["allocation_exceeded_reason"]), (False, None))
        for used in ({"wall_seconds": 1501, "model_launches": 3, "source_opens": 9},
                     {"wall_seconds": 60, "model_launches": 4, "source_opens": 9},
                     {"wall_seconds": 60, "model_launches": 3, "source_opens": 13}):
            with self.assertRaisesRegex(ValueError, "allocation_exceeded_without_reason"):
                ip.finalize_receipt(receipt("2026-09-25", used=used))
            with self.assertRaisesRegex(ValueError, "allocation_exceeded_without_reason"):
                ip.finalize_receipt(receipt("2026-09-25", used=used, allocation_exceeded_reason=None))
            with self.assertRaisesRegex(ValueError, "invalid_allocation_exceeded_reason"):
                ip.finalize_receipt(receipt("2026-09-25", used=used, allocation_exceeded_reason="  "))
            ok = ip.finalize_receipt(receipt("2026-09-25", used=used, allocation_exceeded_reason="watchdog"))
            self.assertTrue(ok["allocation_exceeded"])
        # A non-numeric allocation value cannot be compared, so it is refused, never skipped.
        for key, bad in (("source_opens", "about twelve"), ("wall_minutes", "25"), ("present_max", "1"),
                         ("deep_candidates", None), ("model_launches", True), ("wall_minutes", float("nan"))):
            with self.assertRaisesRegex(ValueError, "^allocation_not_numeric:" + key):
                ip.finalize_receipt(receipt("2026-09-25", allocation=dict(ip.budget(ip.POLICY), **{key: bad})))
        # The flag is never taken from the input.
        honest = ip.finalize_receipt(receipt("2026-09-25", allocation_exceeded=True))
        self.assertFalse(honest["allocation_exceeded"])
        plan = ip.plan_pass([], NOW, backlog_count=0)
        draft = ip.assemble_draft(plan, {"used": {"source_opens": 40}, "allocation_exceeded_reason": "one long paper",
                                         "coverage": {"internal": "complete", "external": "complete", "gaps": []}},
                                  {}, generator="g", challenger="c", started_at=NOW, now=NOW, model_launches=1)
        self.assertEqual(draft["allocation_exceeded_reason"], "model: one long paper")
        self.assertTrue(ip.settle(draft, now=NOW, corpus=[], docket={"events": []})[0]["allocation_exceeded"])
        host = ip.assemble_draft(plan, None, {}, generator="g", challenger="c", started_at=NOW,
                                 now=NOW + dt.timedelta(minutes=26), model_launches=1, failed="watchdog",
                                 exceeded_reason="model calls hit the 1440s watchdog")
        settled, _ = ip.settle(host, now=NOW)
        self.assertEqual((settled["outcome"], settled["allocation_exceeded"]), ("failed", True))

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ip.main(list(args) + ["--now", iso(NOW)])
        return rc, out.getvalue()

    def over_budget_draft(self, tmp):
        plan = ip.plan_pass([], NOW, backlog_count=0)
        draft = ip.assemble_draft(plan, {"questions": ["a"], "used": {"source_opens": 2},
                                         "coverage": {"internal": "complete", "external": "complete", "gaps": []},
                                         "candidates": [candidate(), {"dedupe_key": "Not A Slug"}]},
                                  {0: {"verdict": "survives", "notes": ""}}, generator="gpt-5.6-sol",
                                  challenger="claude-fable-5-1", started_at=NOW - dt.timedelta(minutes=40), now=NOW,
                                  model_launches=2, corpus_build_seconds=7)
        self.assertIsNone(draft["allocation_exceeded_reason"])
        path = Path(tmp) / "draft.json"
        path.write_text(json.dumps(draft))
        return plan, path

    def test_refused_finalize_still_writes_a_failed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            intel = Path(tmp) / "intelligence"
            plan, draft = self.over_budget_draft(tmp)
            # Validate-only: the refusal is reported and nothing is written.
            rc, text = self.run_cli("finalize", "--receipt", str(draft))
            self.assertEqual((rc, json.loads(text)["refused"]), (1, "allocation_exceeded_without_reason:wall_seconds"))
            rc, text = self.run_cli("finalize", "--receipt", str(draft), "--dir", str(intel), "--corpus", "")
            self.assertEqual(rc, 5, text)
            result = json.loads(text)
            self.assertEqual((result["outcome"], result["outbox"], result["proofs"]), ("failed", [], []))
            self.assertEqual(result["refused"], "allocation_exceeded_without_reason:wall_seconds")
            value = json.loads(Path(result["receipt_path"]).read_text())
            self.assertEqual((value["date"], value["outcome"], value["perspective"]),
                             ("2026-09-25", "failed", plan["perspective"]))
            self.assertEqual(value["refusal_reasons"], ["allocation_exceeded_without_reason:wall_seconds"])
            self.assertIn("receipt_refused: allocation_exceeded_without_reason:wall_seconds", value["coverage"]["gaps"])
            self.assertEqual(value["allocation"], plan["reserved"])
            self.assertEqual(value["used"], {"wall_seconds": 2400, "model_launches": 2, "source_opens": 2})
            self.assertTrue(value["allocation_exceeded"])
            self.assertTrue(value["allocation_exceeded_reason"].startswith("not stated"))
            self.assertEqual(value["corpus_build_seconds"], 7)
            self.assertEqual([(r["dedupe_key"], r["status"], r["reasons"]) for r in value["candidates"]],
                             [("batch-review-exports", "held", ["receipt_refused"]),
                              (None, "held", ["receipt_refused"])])
            self.assertEqual(sorted(p.name for p in intel.iterdir()), ["2026-09-25.json"])  # no outbox, proof, ledger
            # Recorded as ran-and-failed: no second pass today, and not a missed day.
            history, _ = ip.load_history(intel)
            self.assertEqual(ip.plan_pass(history, NOW + dt.timedelta(hours=1), backlog_count=0)["reason"],
                             "already_ran_today")
            self.assertEqual(ip.missed_days(history, NOW + dt.timedelta(days=1), "2026-09-25"), [])
            rc, text = self.run_cli("plan", "--dir", str(intel))
            self.assertEqual((rc, json.loads(text)["reason"]), (4, "already_ran_today"))
            # A second refusal the same day does not overwrite the recorded one.
            rc, text = self.run_cli("finalize", "--receipt", str(draft), "--dir", str(intel))
            self.assertEqual((rc, json.loads(text)["refused"]), (1, "receipt_exists"))

    def test_unreadable_draft_still_records_the_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            intel = Path(tmp) / "intelligence"
            bad = Path(tmp) / "draft.json"
            bad.write_text('{"schema": "fa-intelligence-pass/v1", "refused": "draft blew up"}')
            rc, text = self.run_cli("finalize", "--receipt", str(bad), "--dir", str(intel))
            self.assertEqual(rc, 5, text)
            value = json.loads((intel / "2026-09-25.json").read_text())
            self.assertEqual((value["outcome"], value["mode"], value["perspective"], value["candidates"]),
                             ("failed", "scheduled", None, []))
            self.assertEqual(value["refusal_reasons"], ["invalid_receipt_fields"])
            rc, text = self.run_cli("finalize", "--receipt", str(Path(tmp) / "missing.json"),
                                    "--dir", str(Path(tmp) / "other"))
            self.assertEqual((rc, json.loads(text)["refused"]), (5, "FileNotFoundError"))

    def refuse_draft(self, tmp, draft):
        """Finalize a host-written draft into a fresh dir; it must refuse with a receipt."""
        intel = Path(tmp) / ("intelligence-%d" % len(list(Path(tmp).iterdir())))
        path = Path(tmp) / "draft.json"
        path.write_text(json.dumps(draft))
        rc, text = self.run_cli("finalize", "--receipt", str(path), "--dir", str(intel))
        self.assertEqual(rc, 5, text)
        result = json.loads(text)
        value = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual((value["outcome"], value["refusal_reasons"]), ("failed", [result["refused"]]))
        self.assertEqual((result["outbox"], result["proofs"]), ([], []))
        self.assertIn("receipt_refused: " + result["refused"], value["coverage"]["gaps"])
        self.assertEqual(ip.finalize_receipt(value)["outcome"], "failed")  # the failed receipt itself validates
        return result["refused"], value

    def good_draft(self):
        plan = ip.plan_pass([], NOW, backlog_count=0)
        return ip.assemble_draft(plan, {"questions": ["a"], "used": {"source_opens": 2},
                                        "coverage": {"internal": "complete", "external": "complete", "gaps": []},
                                        "candidates": [candidate()]},
                                 {0: {"verdict": "survives", "notes": ""}}, generator="g", challenger="c",
                                 started_at=NOW - dt.timedelta(minutes=5), now=NOW, model_launches=2)

    def test_malformed_host_draft_is_a_refusal_with_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                (dict(self.good_draft(), allocation=["not", "a", "dict"]), "invalid_allocation"),
                (dict(self.good_draft(), allocation="25 minutes"), "invalid_allocation"),
                ({k: v for k, v in self.good_draft().items() if k != "coverage"}, "invalid_coverage"),
                (dict(self.good_draft(), coverage=None), "invalid_coverage"),
                (dict(self.good_draft(), coverage={"internal": ["x"], "external": {}, "gaps": None}),
                 "invalid_coverage"),
                ({"schema": ip.DRAFT_SCHEMA}, "invalid_coverage"),
                (dict(self.good_draft(), allocation=dict(self.good_draft()["allocation"], present_max="1")),
                 "allocation_not_numeric:present_max"),
                (dict(self.good_draft(), allocation=dict(self.good_draft()["allocation"], wall_minutes="25")),
                 "allocation_not_numeric:wall_minutes"),
                (dict(self.good_draft(), candidates={"dedupe_key": "x"}), "invalid_candidates"),
            ]
            for draft, reason in cases:
                refused, value = self.refuse_draft(tmp, draft)
                self.assertEqual(refused, reason, draft)
                self.assertEqual(value["date"], "2026-09-25")
            # A string budget value is dropped from the salvaged allocation, the rest is kept.
            draft = dict(self.good_draft(), allocation=dict(self.good_draft()["allocation"], wall_minutes="25"))
            _, value = self.refuse_draft(tmp, draft)
            self.assertNotIn("wall_minutes", value["allocation"])
            self.assertEqual(value["allocation"]["present_max"], 1)
            self.assertEqual([(r["status"], r["reasons"]) for r in value["candidates"]],
                             [("held", ["receipt_refused"])])

    def test_unexpected_settle_error_is_still_a_refusal_with_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            for exc in (TypeError("x"), KeyError("coverage"), AttributeError("x"), ValueError("odd_shape")):
                with mock.patch.object(ip, "settle", side_effect=exc):
                    refused, _ = self.refuse_draft(tmp, self.good_draft())
                expected = "odd_shape" if isinstance(exc, ValueError) else \
                    "draft_not_settleable:" + type(exc).__name__
                self.assertEqual(refused, expected)
            # Even when salvaging the draft fails, the day is recorded.
            real = ip.refusal_receipt
            calls = []

            def flaky(value, reason, **kw):
                calls.append(value)
                if len(calls) == 1:
                    raise TypeError("salvage failed")
                return real(value, reason, **kw)
            with mock.patch.object(ip, "settle", side_effect=TypeError("x")), \
                    mock.patch.object(ip, "refusal_receipt", side_effect=flaky):
                refused, value = self.refuse_draft(tmp, self.good_draft())
            self.assertEqual((refused, value["date"], calls[-1]), ("draft_not_settleable:TypeError", "2026-09-25",
                                                                   {"date": "2026-09-25"}))

    def test_refusal_receipt_keeps_gap_dates_and_is_itself_valid(self):
        draft = {"schema": ip.DRAFT_SCHEMA, "date": "2026-09-25", "mode": "scheduled_after_gap",
                 "gap_dates": ["2026-09-23", "2026-09-26", "junk"], "perspective": "nonsense",
                 "used": {"wall_seconds": float("inf"), "model_launches": True, "source_opens": 3},
                 "allocation": ip.budget(ip.POLICY), "candidates": "not a list"}
        value = ip.refusal_receipt(draft, "boom", now=NOW)
        self.assertEqual((value["mode"], value["gap_dates"], value["perspective"]),
                         ("scheduled_after_gap", ["2026-09-23"], None))
        self.assertEqual(value["used"], {"wall_seconds": 0, "model_launches": 0, "source_opens": 3})
        self.assertEqual((value["allocation_exceeded"], value["allocation_exceeded_reason"]), (False, None))
        with self.assertRaisesRegex(ValueError, "refusal_reasons_require_failed_outcome"):
            ip.finalize_receipt(receipt("2026-09-25", refusal_reasons=["x"]))

    def test_corpus_build_time_is_recorded_and_not_counted(self):
        plan = ip.plan_pass([], NOW, backlog_count=0)
        output = {"questions": ["a"], "used": {"source_opens": 1},
                  "coverage": {"internal": "complete", "external": "complete", "gaps": []}}
        # The corpus took 30 minutes; the pass itself (timed from after the build) took 10.
        draft = ip.assemble_draft(plan, output, {}, generator="g", challenger="c", started_at=NOW,
                                  now=NOW + dt.timedelta(minutes=10), model_launches=1, corpus_build_seconds=1800)
        settled, _ = ip.settle(draft, now=NOW, corpus=[], docket={"events": []})
        self.assertEqual((settled["used"]["wall_seconds"], settled["corpus_build_seconds"]), (600, 1800))
        self.assertFalse(settled["allocation_exceeded"])
        self.assertIsNone(ip.finalize_receipt(receipt("2026-09-25"))["corpus_build_seconds"])
        for bad in (-1, "10", True, float("nan")):
            with self.assertRaisesRegex(ValueError, "invalid_corpus_build_seconds"):
                ip.finalize_receipt(receipt("2026-09-25", corpus_build_seconds=bad))

    def test_cli_round_trip_feeds_initiative_health(self):
        import initiative_health as health
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            intel = tmp / "state" / "intelligence"
            (tmp / "state").mkdir()

            def run(*args):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = ip.main(list(args) + ["--now", iso(NOW)])
                return rc, out.getvalue()

            rc, text = run("plan", "--dir", str(intel))
            self.assertEqual(rc, 0)
            (tmp / "plan.json").write_text(text)
            (tmp / "out.json").write_text("Here you go:\n```json\n" + json.dumps({
                "questions": ["a", "b", "c"], "used": {"source_opens": 2},
                "coverage": {"internal": "complete", "external": "partial", "gaps": ["paywall"]},
                "candidates": [candidate()]}) + "\n```\n")
            (tmp / "ch0.json").write_text('{"verdict": "survives", "notes": "fine", "counterevidence": []}')
            rc, text = run("draft", "--plan", str(tmp / "plan.json"), "--output", str(tmp / "out.json"),
                           "--challenge", "0:" + str(tmp / "ch0.json"), "--generator", "gpt-5.6-sol",
                           "--challenger", "claude-fable-5-1", "--started-at", iso(NOW), "--model-launches", "2")
            self.assertEqual(rc, 0)
            (tmp / "draft.json").write_text(text)
            (tmp / "docket.json").write_text(json.dumps({"schema": "iris-initiative-docket/v1", "generation": 0,
                                                         "events": []}))
            rc, text = run("finalize", "--receipt", str(tmp / "draft.json"), "--dir", str(intel),
                           "--docket", str(tmp / "docket.json"))
            self.assertEqual(rc, 0, text)
            result = json.loads(text)
            self.assertEqual((result["outcome"], len(result["outbox"])), ("opportunities_prepared", 1))
            proposal = json.loads(Path(result["outbox"][0]).read_text())
            self.assertTrue(docket_accepts(proposal, NOW))
            oid = ip.opportunity_id("batch-review-exports")
            proof = intel / "proofs" / (oid + ".md")
            self.assertEqual(result["proofs"], [str(proof)])
            self.assertEqual(proof.read_text(), candidate()["proof_body"])
            self.assertEqual(stat.S_IMODE(proof.stat().st_mode), 0o600)
            self.assertEqual(proposal["source"]["reference"]["id"], oid)
            self.assertEqual(proposal["packet"]["question"], candidate()["decision_question"])
            ledger = ip.load_ledger(intel)
            self.assertEqual(ledger, {proposal["source"]["work_revision"]: ip.evidence_tokens(candidate())})
            self.assertEqual(stat.S_IMODE((intel / "evidence" / (oid + ".json")).stat().st_mode), 0o600)
            rc, text = run("finalize", "--receipt", str(tmp / "draft.json"), "--dir", str(intel))
            self.assertEqual((rc, json.loads(text)["refused"]), (1, "receipt_exists"))
            seen = health.latest_dated_file(intel, "*.json")
            self.assertEqual((seen["latest_date"], seen["count"]), ("2026-09-25", 1))


class CorpusTest(unittest.TestCase):
    """build_corpus reads every source read-only and records each missing one as a gap."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        (self.tmp / "plans.json").write_text(json.dumps({"lanes": [
            {"name": "ad-system", "waiting_on_anthony": ["Rule the study scrape ceiling."]},
            {"name": "quiet", "waiting_on_anthony": []}]}))
        radar = self.tmp / "radar"
        radar.mkdir()
        (radar / "TOPICS.md").write_text("| ID | Topic | Scope | Cadence |\n| --- | --- | --- | --- |\n"
                                         "| M01 | Conversion rate optimization | funnels, A/B tests | Daily |\n")
        (radar / "DECISIONS.md").write_text("| ID / dedup key | Disposition | Reason | Approval |\n|---|---|---|---|\n"
                                            "| D-001 — Mission Control | Excluded | Dormant | None |\n\n"
                                            "**R-ENG-001 — deepened and challenged.** Defer architecture change.\n")
        c = candidate()
        (self.tmp / "docket.json").write_text(json.dumps({"events": [
            docket_event(1, "propose", {"source": source_for(c), "packet": {
                "question": "Should review exports be batched?", "recommendation": "Batch on one project."}}),
            docket_event(2, "respond", {"disposition": "declined", "source": source_for(c)})]}))
        self.intel = self.tmp / "intel"

    def serve(self, body):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/data/board.json" % server.server_address[1]

    def config(self, **changes):
        base = {"board_url": None, "plans_snapshot": str(self.tmp / "plans.json"),
                "radar_dir": str(self.tmp / "radar"), "docket": str(self.tmp / "docket.json"),
                "intelligence_dir": str(self.intel)}
        base.update(changes)
        return base

    def test_all_sources_read_into_entries(self):
        url = self.serve({"items": [{"id": "work-1", "project": "PG ad-script pass",
                                     "next_action": "Line-edit the prepared scripts."}, "junk"]})
        corpus = ip.build_corpus(self.config(board_url=url))
        self.assertEqual(corpus["gaps"], [])
        ids = {e["id"]: e for e in corpus["entries"]}
        self.assertEqual(ids["board:work-1"]["kind"], "backlog")
        self.assertIn("Line-edit", ids["board:work-1"]["text"])
        self.assertEqual(ids["plans:ad-system:waiting-0"]["text"], "Rule the study scrape ceiling.")
        self.assertIn("radar-topic:M01", ids)
        self.assertIn("radar-decision:D-001", ids)
        self.assertIn("radar-decision:R-ENG-001", ids)
        self.assertNotIn("radar-topic:ID", ids)
        oid = ip.opportunity_id("batch-review-exports")
        self.assertEqual((ids[oid]["kind"], ids[oid]["text"]), ("docket", "Should review exports be batched? "
                                                                          "Batch on one project."))
        self.assertTrue(all(set(e) == {"id", "kind", "text"} and e["kind"] in ip.CORPUS_KINDS
                            for e in corpus["entries"]))

    def test_missing_sources_become_gaps_never_errors(self):
        corpus = ip.build_corpus(self.config(board_url="http://127.0.0.1:1/data/board.json",
                                             plans_snapshot=str(self.tmp / "nope.json"),
                                             radar_dir=str(self.tmp / "nothing"), docket=None))
        self.assertEqual([g.split(":")[0] for g in corpus["gaps"]], ["board", "plans_snapshot", "radar", "docket"])
        self.assertIn("plans_snapshot: missing", corpus["gaps"])
        self.assertIn("docket: not configured", corpus["gaps"])
        remote = ip.build_corpus(self.config(board_url="http://example.org/data/board.json"))
        self.assertEqual(remote["gaps"], ["board: unavailable (ValueError)"])

    def test_cli_writes_private_corpus_and_finalize_uses_it(self):
        out = self.intel / "raw" / "corpus-2026-09-25.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = ip.main(["corpus", "--dir", str(self.intel), "--out", str(out), "--board-url", "",
                          "--plans", str(self.tmp / "plans.json"), "--radar-dir", str(self.tmp / "radar"),
                          "--now", iso(NOW)])
        self.assertEqual(rc, 0, stdout.getvalue())
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        value = json.loads(out.read_text())
        self.assertEqual(value["gaps"], ["board: not configured", "docket: not configured"])
        entries, gaps = ip.load_corpus_file(str(out))
        self.assertEqual(len(gaps), 2)
        self.assertTrue(entries)
        # A near-duplicate of a board item is rejected as not novel.
        near = candidate(dedupe_key="ssp-edit", question="Line-edit the prepared SSP ad scripts?",
                         recommendation="Line-edit the prepared scripts in the PG ad-script pass.")
        corpus = [{"id": "board:work-1", "kind": "backlog",
                   "text": "PG ad-script pass Line-edit the prepared SSP ad scripts."}]
        self.assertFalse(ip.novelty_check(near, corpus)["novel"])


@unittest.skipUnless(shutil.which("bash"), "bash required")
class StageFourStubTest(unittest.TestCase):
    """Runs the real runner in stub mode inside a temp copy; no model, gh or launchd call."""

    def run_scan(self, **env):
        tmp = Path(self.tmp)
        full = {"HOME": str(tmp / "home"), "DAILY_SCAN_STUB": "1", "PATH": os.environ.get("PATH", "")}
        full.update(env)
        return subprocess.run([str(tmp / "repo" / "tools" / "daily-scan" / "run-daily-scan.sh")], cwd=str(tmp),
                              env=full, capture_output=True, text=True, timeout=120)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tmp = Path(self.tmp)
        (tmp / "repo" / "tools").mkdir(parents=True)
        shutil.copytree(REPO / "tools" / "daily-scan", tmp / "repo" / "tools" / "daily-scan")
        (tmp / "repo" / "tools" / "convergence").mkdir()
        shutil.copy(HERE / "intelligence_pass.py", tmp / "repo" / "tools" / "convergence")
        fake = tmp / "home" / ".local" / "bin"
        fake.mkdir(parents=True)
        for name in ("codex", "claude", "gh", "launchctl"):
            (fake / name).write_text('#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' % (name, tmp / "calls"))
            (fake / name).chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_watchdog_default_fires_before_the_wall_allocation(self):
        script = (REPO / "tools" / "daily-scan" / "run-daily-scan.sh").read_text()
        default = int(re.search(r'INTEL_TIMEOUT="\$\{DAILY_SCAN_INTEL_TIMEOUT:-(\d+)\}"', script).group(1))
        self.assertEqual(default, 1440)
        self.assertLess(default, ip.POLICY["wall_minutes"] * 60)
        # When it fires, the host (not the model) states why the allocation was exceeded.
        self.assertIn('intel_over="model calls hit the ${INTEL_TIMEOUT}s watchdog"', script)
        self.assertIn('draft_args+=(--allocation-exceeded-reason "${intel_over}")', script)

    def test_unset_flag_only_logs_skipped(self):
        proc = self.run_scan()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = Path(self.tmp) / "repo" / "state"
        self.assertIn("stage4 SKIPPED (not enabled)", proc.stdout)
        self.assertNotIn("stage4 START", proc.stdout)
        self.assertEqual(sorted(p.name for p in state.iterdir()), ["daily-scan", "logs"])
        self.assertEqual([p.name for p in (state / "daily-scan").iterdir() if "stage4" in p.name or "intel" in p.name], [])
        self.assertEqual([p.name for p in (state / "daily-scan" / "raw").iterdir() if "intel" in p.name], [])
        calls = (Path(self.tmp) / "calls").read_text().split("\n") if (Path(self.tmp) / "calls").exists() else []
        self.assertFalse([c for c in calls if c.startswith(("codex", "claude", "gh"))])

    def test_enabled_stub_writes_receipt_and_outbox_under_stub_dir(self):
        proc = self.run_scan(DAILY_SCAN_INTELLIGENCE="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stage4 OK -- outcome opportunities_prepared", proc.stdout)
        state = Path(self.tmp) / "repo" / "state"
        self.assertFalse((state / "intelligence").exists())
        receipts = sorted((state / "intelligence-stub").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        value = json.loads(receipts[0].read_text())
        self.assertEqual((value["used"]["model_launches"], value["candidates"][0]["status"]),
                         (2, "presented_to_outbox"))
        outbox = list((state / "intelligence-stub" / "outbox").glob("opportunity-*.json"))
        self.assertEqual(len(outbox), 1)
        proposal = json.loads(outbox[0].read_text())
        oid = proposal["source"]["work_item_id"]
        self.assertTrue(proposal["packet"]["question"].startswith("STUB: should"))
        self.assertTrue((state / "intelligence-stub" / "proofs" / (oid + ".md")).is_file())
        self.assertEqual(proposal["source"]["reference"]["id"], oid)
        corpus = json.loads((state / "intelligence-stub" / "raw").glob("corpus-*.json").__next__().read_text())
        self.assertIn("board: not configured", corpus["gaps"])
        self.assertIn("plans_snapshot: missing", corpus["gaps"])
        self.assertEqual(value["coverage"]["internal"], "partial")
        self.assertTrue(any(g.startswith("novelty_corpus_gap: board") for g in value["coverage"]["gaps"]))
        self.assertEqual((value["mode"], value["gap_dates"], value["allocation_exceeded"]), ("scheduled", [], False))
        calls = (Path(self.tmp) / "calls").read_text() if (Path(self.tmp) / "calls").exists() else ""
        self.assertNotIn("codex", calls)
        self.assertNotIn("claude", calls)
        again = self.run_scan(DAILY_SCAN_INTELLIGENCE="1")
        self.assertIn("stage4 SKIPPED -- plan: already_ran_today", again.stdout)

    def python_wrapper(self, body):
        """A FULLY_AWARE_PYTHON that runs ``body`` (sh) before exec'ing the real python."""
        path = Path(self.tmp) / "python-wrapper"
        path.write_text('#!/bin/sh\nREAL="%s"\n%s\nexec "$REAL" "$@"\n' % (sys.executable, body))
        path.chmod(0o755)
        return str(path)

    def test_corpus_build_time_is_not_counted_as_pass_wall_time(self):
        wrapper = self.python_wrapper('[ "$2" = "corpus" ] && sleep 3')
        proc = self.run_scan(DAILY_SCAN_INTELLIGENCE="1", FULLY_AWARE_PYTHON=wrapper)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stage4 OK", proc.stdout)
        value = json.loads(next((Path(self.tmp) / "repo" / "state" / "intelligence-stub").glob("*.json")).read_text())
        self.assertGreaterEqual(value["corpus_build_seconds"], 3)
        self.assertLess(value["used"]["wall_seconds"], 3)

    def test_refused_finalize_records_a_failed_receipt_and_blocks_a_second_pass(self):
        # The draft step reports a pass far over its wall allocation with no reason.
        wrapper = self.python_wrapper(
            'if [ "$2" = "draft" ]; then "$REAL" "$@" | "$REAL" -c \'import json,sys\n'
            'd = json.load(sys.stdin); d["used"]["wall_seconds"] = 99999; d["allocation_exceeded_reason"] = None\n'
            'print(json.dumps(d))\'; exit $?; fi')
        proc = self.run_scan(DAILY_SCAN_INTELLIGENCE="1", FULLY_AWARE_PYTHON=wrapper)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("finalize refused (allocation_exceeded_without_reason:wall_seconds)", proc.stdout)
        self.assertIn("recorded as a failed pass receipt", proc.stdout)
        self.assertNotIn("stage4 OK", proc.stdout)
        state = Path(self.tmp) / "repo" / "state"
        receipts = sorted((state / "intelligence-stub").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        value = json.loads(receipts[0].read_text())
        self.assertEqual((value["outcome"], value["refusal_reasons"], value["allocation_exceeded"]),
                         ("failed", ["allocation_exceeded_without_reason:wall_seconds"], True))
        self.assertEqual([(r["dedupe_key"], r["status"]) for r in value["candidates"]],
                         [("stub-intelligence-rehearsal", "held")])
        self.assertFalse((state / "intelligence-stub" / "outbox").exists())
        self.assertFalse((state / "intelligence-stub" / "proofs").exists())
        self.assertTrue(list((state / "daily-scan").glob("*stage4.FAILED")))
        again = self.run_scan(DAILY_SCAN_INTELLIGENCE="1")
        self.assertIn("stage4 SKIPPED -- plan: already_ran_today", again.stdout)


if __name__ == "__main__":
    unittest.main()
