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
import tempfile
import unittest
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
        self.assertEqual((tomorrow["run"], tomorrow["mode"], tomorrow["covers_date"]),
                         (True, "recovery", "2026-09-25"))
        self.assertEqual(tomorrow["reason"], "recovering_preempted_day")

    def test_one_recovery_pass_per_gap_and_one_pass_per_day(self):
        history = [receipt("2026-09-21")]  # 22, 23 and 24 missed
        plan = ip.plan_pass(history, NOW, backlog_count=0)
        self.assertEqual((plan["mode"], plan["covers_date"], plan["gap_dates"]),
                         ("recovery", "2026-09-24", ["2026-09-22", "2026-09-23", "2026-09-24"]))
        history.append(receipt("2026-09-25", mode="recovery", covers="2026-09-24"))
        same_day = ip.plan_pass(history, NOW + dt.timedelta(hours=2), backlog_count=0)
        self.assertEqual((same_day["run"], same_day["reason"]), (False, "already_ran_today"))
        next_day = ip.plan_pass(history, NOW + dt.timedelta(days=1), backlog_count=0)
        self.assertEqual((next_day["run"], next_day["mode"]), (True, "scheduled"))
        with self.assertRaisesRegex(ValueError, "recovery_must_cover_earlier_day"):
            ip.finalize_receipt(receipt("2026-09-25", mode="recovery", covers="2026-09-25"))

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
        self.assertNotEqual(ip.evidence_revision(candidate(counterevidence=["new"])), ip.evidence_revision(candidate()))


class SuppressionTest(unittest.TestCase):
    def events(self, c, disposition="declined"):
        key = "initiative-" + "0" * 32
        return {"schema": "iris-initiative-docket/v1", "generation": 2, "events": [
            docket_event(1, "propose", {"event_id": "e1", "action_class": "opportunity_proposal",
                                        "source": source_for(c), "packet": {}, "next_check_at": "x", "origin": {}}),
            docket_event(2, "respond", {"event_id": "e2", "action_key": key, "proposal_revision": "0" * 64,
                                        "response_ref": {}, "disposition": disposition, "option_id": None,
                                        "source": source_for(c)})]}

    def test_same_evidence_decline_suppressed_changed_evidence_eligible(self):
        c = candidate()
        docket = self.events(c)
        self.assertEqual(ip.suppression(c, docket, NOW)["status"], "suppressed")
        reworded = candidate(recommendation="Entirely different wording of the same idea.")
        self.assertEqual(ip.suppression(reworded, docket, NOW)["status"], "suppressed")
        changed = candidate(counterevidence=["A new contrary study appeared."])
        self.assertEqual(ip.suppression(changed, docket, NOW)["status"], "eligible_changed_evidence")
        self.assertEqual(ip.suppression(candidate(dedupe_key="other-idea"), docket, NOW)["status"], "eligible")

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
            self.assertTrue(docket_accepts(json.loads(Path(result["outbox"][0]).read_text()), NOW))
            rc, text = run("finalize", "--receipt", str(tmp / "draft.json"), "--dir", str(intel))
            self.assertEqual((rc, json.loads(text)["refused"]), (1, "receipt_exists"))
            seen = health.latest_dated_file(intel, "*.json")
            self.assertEqual((seen["latest_date"], seen["count"]), ("2026-09-25", 1))


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
        self.assertEqual(len(list((state / "intelligence-stub" / "outbox").glob("opportunity-*.json"))), 1)
        calls = (Path(self.tmp) / "calls").read_text() if (Path(self.tmp) / "calls").exists() else ""
        self.assertNotIn("codex", calls)
        self.assertNotIn("claude", calls)
        again = self.run_scan(DAILY_SCAN_INTELLIGENCE="1")
        self.assertIn("stage4 SKIPPED -- plan: already_ran_today", again.stdout)


if __name__ == "__main__":
    unittest.main()
