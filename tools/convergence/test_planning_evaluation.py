from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import planning_evaluation as evaluation
import planning_receipts as receipts


class PlanningEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        now = dt.datetime.now(dt.timezone.utc)
        self.design = self.root / "design.json"
        self.design.write_bytes(
            receipts.encode(
                {
                    "schema": "convergence-evaluation-design/v1",
                    "frozen_at": (now - dt.timedelta(days=2)).isoformat(),
                    "held_out": {"expected_cases": 3},
                }
            )
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ref(self, path: Path) -> dict[str, str]:
        return {"path": str(path), "sha256": receipts.sha(path.read_bytes())}

    def payload(self, cases: list[Path | None]) -> dict[str, object]:
        return {
            "schema": evaluation.INPUT_SCHEMA,
            "design_ref": self.ref(self.design),
            "case_dirs": [None if case is None else str(case) for case in cases],
        }

    def make_case(
        self,
        *,
        encounter_id: str | None = None,
        board_index: int | None = None,
        proposal_ids: list[str] | None = None,
    ) -> tuple[Path, str]:
        self.counter += 1
        index = self.counter if board_index is None else board_index
        case = self.root / f"case-{self.counter}"
        board = self.root / f"board-{index}.json"
        board.write_bytes(
            receipts.encode(
                {
                    "base": {
                        "items": [
                            {
                                "id": "alpha",
                                "effective_status": "open",
                                "due_at": "2099-01-01",
                                "project": f"A-{index}",
                            },
                            {
                                "id": "beta",
                                "effective_status": "open",
                                "due_at": "2099-01-01T01:00:00Z",
                                "project": f"B-{index}",
                            },
                            {
                                "id": "gamma",
                                "effective_status": "open",
                                "due_at": "2099-01-01T02:00:00Z",
                                "project": f"G-{index}",
                            },
                            {
                                "id": "zeta",
                                "effective_status": "open",
                                "due_at": "2099-01-02",
                                "project": f"Z-{index}",
                            },
                        ]
                    }
                }
            )
        )
        now = dt.datetime.now(dt.timezone.utc)
        source = {
            "sweep_run_id": f"sweep-{self.counter}",
            "encounter_id": encounter_id or f"encounter-{self.counter}",
            "encountered_at": (now - dt.timedelta(seconds=1)).isoformat(),
            "frozen_design": self.ref(self.design),
            "source_ref": self.ref(board),
            "source_cutoff": (now - dt.timedelta(seconds=2)).isoformat(),
            "board_current": True,
            "coverage_limits": ["Synthetic calendar coverage is partial."],
            "development_case": False,
            "already_ranked_or_labelled": False,
        }
        tail = receipts.append(case, "source", source, None)["tail_sha256"]
        tail = receipts.append(case, "baseline", {}, tail)["tail_sha256"]
        proposal_ids = proposal_ids or ["zeta", "alpha"]
        tail = receipts.append(
            case,
            "proposal",
            {
                "ordered_ids": proposal_ids,
                "reasons": ["Synthetic comparison row." for _ in proposal_ids],
                "coverage_limits": ["Synthetic calendar coverage is partial."],
                "human_labels_known": False,
            },
            tail,
        )["tail_sha256"]
        return case, tail

    def append_label(
        self,
        case: Path,
        tail: str,
        *,
        human_top_three_ids: list[str] | None,
        usefulness: str | None,
        correction_burden: int | float | None,
        supervision: int | float | None,
        hard_failures: list[str] | None,
        unknown: bool = False,
        turn_suffix: str = "answer",
    ) -> str:
        chain, _ = receipts.load(case)
        proposal_at = receipts.instant(chain[2]["recorded_at"])
        recorded_at = dt.datetime.now(dt.timezone.utc)
        answer = None
        if not unknown:
            answer = {
                "task_id": "12345678-1234-1234-1234-123456789abc",
                "turn_id": f"turn-{turn_suffix}",
                "message_id": None,
                "reference_limit": "Individual message ID unavailable.",
                "observed_at": (proposal_at + dt.timedelta(microseconds=1)).isoformat(),
            }
        data = {
            "answer_reference": answer,
            "human_top_three_ids": human_top_three_ids,
            "usefulness": usefulness,
            "correction_burden": correction_burden,
            "human_supervision_minutes": supervision,
            "hard_failures": hard_failures,
            "raw_answer_minimal": None if unknown else "Synthetic owner answer.",
        }
        with mock.patch.object(receipts, "clock_now", return_value=recorded_at):
            return receipts.append(case, "label", data, tail)["tail_sha256"]

    def complete_case(
        self,
        *,
        human: list[str],
        proposal_ids: list[str] | None = None,
        hard_failures: list[str] | None = None,
        usefulness: str = "useful",
        correction: int | float = 1,
        supervision: int | float = 2,
    ) -> Path:
        case, tail = self.make_case(proposal_ids=proposal_ids)
        self.append_label(
            case,
            tail,
            human_top_three_ids=human,
            usefulness=usefulness,
            correction_burden=correction,
            supervision=supervision,
            hard_failures=hard_failures if hard_failures is not None else [],
        )
        return case

    def make_missed_case(self) -> Path:
        case = self.root / "missed-case"
        receipts.append(
            case,
            "missed",
            {
                "reason": "No prospective capture before ranking.",
                "encountered_at": None,
                "evidence_reference": "Synthetic sweep receipt.",
            },
            None,
        )
        return case

    def make_outcome_case(self) -> Path:
        case = self.root / "wrong-chain"
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        approval = {
            "quote": "Approve this block",
            "task_id": "12345678-1234-1234-1234-123456789abc",
            "turn_id": "turn-approval",
        }
        proposal = {
            "schema": "iris-schedule-pilot-proposal/v1",
            "status": "scheduled_outcome_pending",
            "proposal_id": "approved-block",
            "event_id": "event-one",
            "start": now.isoformat(),
            "focus_end": (now + dt.timedelta(minutes=45)).isoformat(),
            "end": (now + dt.timedelta(minutes=60)).isoformat(),
            "approval": approval,
            "estimated_focus_minutes": 45,
        }
        calendar = {
            "schema": "iris-calendar-scheduled-receipt/v1",
            **{key: proposal[key] for key in ("proposal_id", "event_id", "start", "focus_end", "end", "approval")},
        }
        proposal_file = self.root / "proposal.json"
        calendar_file = self.root / "calendar.json"
        proposal_file.write_bytes(receipts.encode(proposal))
        calendar_file.write_bytes(receipts.encode(calendar))
        receipts.append(
            case,
            "outcome-source",
            {"proposal_ref": self.ref(proposal_file), "calendar_ref": self.ref(calendar_file)},
            None,
        )
        return case

    def test_empty_cohort_retains_three_unknown_slots(self) -> None:
        report = evaluation.evaluate(self.payload([None, None, None]))
        self.assertEqual(3, report["expected3"])
        self.assertEqual({"expected3": 3, "recorded": 0, "missed": 0, "unlabelled": 0, "complete": 0, "unknown": 3}, report["counts"])
        self.assertEqual(["unknown", "unknown", "unknown"], [slot["state"] for slot in report["slots"]])
        self.assertIsNone(report["agreement"]["aggregate_delta"])
        self.assertFalse(report["qualification"]["eligible_for_owner_review"])

    def test_positive_negative_and_tie_are_strict(self) -> None:
        positive = self.complete_case(human=["zeta"])
        negative = self.complete_case(human=["alpha"], proposal_ids=["zeta"])
        tie = self.complete_case(human=["alpha"], proposal_ids=["alpha", "zeta"])

        positive_report = evaluation.evaluate(self.payload([positive, None, None]))
        self.assertEqual(1, positive_report["agreement"]["aggregate_delta"])
        self.assertFalse(positive_report["qualification"]["eligible_for_owner_review"])

        negative_report = evaluation.evaluate(self.payload([negative, None, None]))
        self.assertEqual(-1, negative_report["agreement"]["aggregate_delta"])

        tie_report = evaluation.evaluate(self.payload([tie, None, None]))
        self.assertEqual(0, tie_report["agreement"]["aggregate_delta"])
        self.assertIn("NO_STRICT_AGGREGATE_IMPROVEMENT", tie_report["qualification"]["reason_codes"])

    def test_all_three_positive_cases_are_only_eligible_for_owner_review(self) -> None:
        cases = [self.complete_case(human=["zeta"]) for _ in range(3)]
        report = evaluation.evaluate(self.payload(cases))
        self.assertEqual(3, report["complete"])
        self.assertEqual(3, report["agreement"]["paired_denominator"])
        self.assertEqual(3, report["agreement"]["aggregate_delta"])
        qualification = report["qualification"]
        self.assertTrue(qualification["eligible_for_owner_review"])
        self.assertFalse(qualification["automatic_win"])
        self.assertFalse(qualification["rollout_authorized"])
        self.assertFalse(qualification["cohort_completeness_verified"])
        self.assertFalse(qualification["real_model_superiority_claim"])

    def test_partial_and_unknown_labels_remain_unlabelled(self) -> None:
        partial, tail = self.make_case()
        self.append_label(
            partial,
            tail,
            human_top_three_ids=["zeta"],
            usefulness=None,
            correction_burden=None,
            supervision=None,
            hard_failures=None,
        )
        unknown, tail = self.make_case()
        self.append_label(
            unknown,
            tail,
            human_top_three_ids=None,
            usefulness=None,
            correction_burden=None,
            supervision=None,
            hard_failures=None,
            unknown=True,
        )
        report = evaluation.evaluate(self.payload([partial, unknown, None]))
        self.assertEqual(["unlabelled", "unlabelled", "unknown"], [slot["state"] for slot in report["slots"]])
        self.assertEqual(2, report["hard_failures"]["unknown_label_count"])
        self.assertEqual(3, report["hard_failures"]["unknown_count"])
        self.assertEqual(1, report["agreement"]["paired_denominator"])
        self.assertEqual(0, report["correction_burden"]["known_count"])

    def test_missed_slot_is_retained_and_never_qualifies(self) -> None:
        missed = self.make_missed_case()
        complete = self.complete_case(human=["zeta"])
        report = evaluation.evaluate(self.payload([complete, missed, None]))
        self.assertEqual(["complete", "missed", "unknown"], [slot["state"] for slot in report["slots"]])
        self.assertEqual(1, report["missed"])
        self.assertEqual(1, report["recorded"] - report["missed"])
        self.assertIn("MISSING_OR_MISSED_SLOT", report["qualification"]["reason_codes"])

    def test_hard_failure_blocks_owner_review_and_is_counted(self) -> None:
        cases = [self.complete_case(human=["zeta"], hard_failures=["unsupported obligation"]) for _ in range(3)]
        report = evaluation.evaluate(self.payload(cases))
        self.assertEqual(3, report["hard_failures"]["known_count"])
        self.assertEqual(3, report["hard_failures"]["cases_with_failures"])
        self.assertIn("HARD_FAILURE_PRESENT", report["qualification"]["reason_codes"])
        self.assertFalse(report["qualification"]["eligible_for_owner_review"])

    def test_latest_genuine_label_wins_and_empty_human_list_is_known(self) -> None:
        case, tail = self.make_case()
        self.append_label(
            case,
            tail,
            human_top_three_ids=None,
            usefulness=None,
            correction_burden=None,
            supervision=None,
            hard_failures=None,
            unknown=True,
            turn_suffix="unknown",
        )
        chain, new_tail = receipts.load(case)
        self.assertEqual("label", chain[-1]["kind"])
        self.append_label(
            case,
            new_tail,
            human_top_three_ids=[],
            usefulness="not_useful",
            correction_burden=0,
            supervision=0,
            hard_failures=[],
            turn_suffix="genuine",
        )
        report = evaluation.evaluate(self.payload([case, None, None]))
        self.assertEqual("complete", report["slots"][0]["state"])
        self.assertEqual(1, report["agreement"]["paired_denominator"])
        self.assertEqual(0, report["agreement"]["proposal"])
        self.assertEqual(1, report["usefulness"]["not_useful"])
        self.assertEqual(1, report["correction_burden"]["known_count"])
        self.assertEqual(0, report["correction_burden"]["total"])
        self.assertIn("NOT_ALL_USEFUL", report["qualification"]["reason_codes"])

    def test_duplicate_encounter_and_source_snapshot_are_rejected(self) -> None:
        one, _ = self.make_case(encounter_id="same-encounter", board_index=1)
        two, _ = self.make_case(encounter_id="same-encounter", board_index=2)
        with self.assertRaisesRegex(evaluation.EvaluationError, "DUPLICATE_ENCOUNTER"):
            evaluation.evaluate(self.payload([one, two, None]))

        shared_one, _ = self.make_case(encounter_id="encounter-one", board_index=7)
        shared_two, _ = self.make_case(encounter_id="encounter-two", board_index=7)
        with self.assertRaisesRegex(evaluation.EvaluationError, "DUPLICATE_SNAPSHOT"):
            evaluation.evaluate(self.payload([shared_one, shared_two, None]))

    def test_design_hash_and_source_design_mismatch_are_rejected(self) -> None:
        case, _ = self.make_case()
        wrong_hash = self.payload([case, None, None])
        wrong_hash["design_ref"] = {"path": str(self.design), "sha256": "0" * 64}
        with self.assertRaisesRegex(evaluation.EvaluationError, "DESIGN_HASH_MISMATCH"):
            evaluation.evaluate(wrong_hash)

        other = self.root / "other-design.json"
        other.write_bytes(receipts.encode({"schema": "convergence-evaluation-design/v1", "frozen_at": "2026-01-01T00:00:00+00:00"}))
        mismatch = self.payload([case, None, None])
        mismatch["design_ref"] = self.ref(other)
        with self.assertRaisesRegex(evaluation.EvaluationError, "DESIGN_MISMATCH"):
            evaluation.evaluate(mismatch)

    def test_wrong_chain_and_tamper_are_rejected_without_path_leaks(self) -> None:
        wrong_chain = self.make_outcome_case()
        with self.assertRaisesRegex(evaluation.EvaluationError, "WRONG_CHAIN") as wrong:
            evaluation.evaluate(self.payload([wrong_chain, None, None]))
        self.assertNotIn(str(wrong_chain), str(wrong.exception))

        tampered = self.complete_case(human=["zeta"])
        receipt = tampered / "0002-baseline.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaisesRegex(evaluation.EvaluationError, "INVALID_CASE_EVIDENCE") as error:
            evaluation.evaluate(self.payload([tampered, None, None]))
        self.assertNotIn(str(tampered), str(error.exception))

    def test_evaluation_makes_no_state_writes(self) -> None:
        case = self.complete_case(human=["zeta"])
        before = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        evaluation.evaluate(self.payload([case, None, None]))
        after = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_cyclic_api_input_and_excessive_receipts_refuse(self) -> None:
        payload = self.payload([None, None, None])
        payload["cycle"] = payload
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(payload)
        case, _ = self.make_case()
        for index in range(1, 18):
            (case / f"{index:04d}-extra.json").write_text("{}")
        with self.assertRaisesRegex(evaluation.EvaluationError, "TOO_MANY_RECEIPTS"):
            evaluation.evaluate(self.payload([case, None, None]))

    def test_cli_returns_bounded_json_without_private_paths(self) -> None:
        input_file = self.root / "request.json"
        input_file.write_bytes(receipts.encode(self.payload([None, None, None])))
        command = [sys.executable, str(Path(evaluation.__file__)), "--input", str(input_file)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(evaluation.REPORT_SCHEMA, output["schema"])
        self.assertNotIn(str(input_file), completed.stdout)


if __name__ == "__main__":
    unittest.main()
