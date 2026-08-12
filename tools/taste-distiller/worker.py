#!/usr/bin/env python3
"""Macro-seat taste-distiller worker (spec §2.2 stage 2, detached).

Drains <operator_root>/spool/distill-queue.ndjson. Per quiet session:
stream the full transcript JSONL (drop isSidechain, keep user/assistant
turns, per-session char cap), run the TASTE MARKER anchor pass, make ONE
headless Haiku call (`claude -p --model haiku`, ruling 3 2026-07-23), and
emit one IngestCandidate per specimen via `imprint ingest scan`. NEVER
keep/kill — quarantine only; Anthony's keep/kill is the only promotion
path.

Idempotency: imprint's (source_kind, source_locator, sha256) dedupe plus a
worker-side ledger of processed session_ids at spool/distill-ledger.json.
A session is distilled once: only after its transcript has been quiet for
QUIET_SECONDS (Stop fires every turn; quiet ≈ session over), and never
again once ledgered (v1 accepts losing turns added hours later — re-run a
session deliberately with --force). Worker death is invisible to sessions:
the hook never waits on this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import assign_scope, ledger_path, load_registry, spool_path

QUIET_SECONDS = 1800
MAX_EXCERPT_CHARS = 160_000     # ~40k tokens; head 25% / tail 75% when over
TURN_CHAR_CAP = 4_000
CLAUDE_TIMEOUT = 300
RETRY_LIMIT = 3
SPECIMEN_KINDS = {"correction", "ruling", "preference", "approved_pattern"}
DISTILLER_ID = "macroseat-taste-distiller/1.0"

PROMPT = """You are the macro-seat taste distiller. Below is a numbered excerpt of one \
Claude Code session transcript (lines tagged L<n>|<role>). Extract 0..N taste \
specimens — moments where the OPERATOR (the human) exercised durable judgment:

- correction: the operator corrected the assistant's approach, output, or framing
- ruling: the operator made an explicit decision between alternatives
- preference: the operator stated a durable preference about how work should be done
- approved_pattern: the operator confirmed an approach as the right one

Rules:
- Only operator judgment counts. Assistant suggestions, plans, and summaries are not specimens.
- Skip routine instructions ("run the tests", "fix the typo") — capture only judgment that should shape FUTURE work.
- Quote the operator faithfully; include the surrounding WHY when one was stated or is clearly implied.
- line_start/line_end are the L<n> numbers bounding the moment in THIS excerpt's numbering.
- scope_hint: a client/collaborator name if the judgment is about one relationship, else "global", else "unknown".
{marker_clause}
Return STRICT JSON only, no prose, no code fences:
{{"specimens": [{{"kind": "correction|ruling|preference|approved_pattern", "content": "faithful quote/summary of the judgment", "why": "the stated or implied reason", "line_start": 0, "line_end": 0, "scope_hint": "global"}}]}}
If there are no specimens: {{"specimens": []}}

TRANSCRIPT EXCERPT:
{excerpt}
"""

MARKER_CLAUSE = """- A TASTE MARKER block from this session's handoff is provided below. EVERY line \
of it MUST be represented by a specimen (it is a guaranteed anchor; use the \
transcript to recover the fuller quote and why):
{marker}
"""


# ---------------------------------------------------------------- transcript

def read_transcript(path: Path) -> list[tuple[int, str, str]]:
    """[(jsonl_line_number, role, text)] — isSidechain dropped, tool noise dropped."""
    turns: list[tuple[int, str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain"):
                continue
            role = obj.get("type")
            if role not in ("user", "assistant"):
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
            text = "\n".join(part.strip() for part in parts if part and part.strip())
            if text:
                turns.append((lineno, role, text))
    return turns


def extract_marker(path: Path) -> list[str]:
    """TASTE MARKER anchor pass (§2.3): find marker bullets anywhere in the
    raw transcript (handoff Writes appear inside tool_use inputs)."""
    bullets: list[str] = []
    pattern = re.compile(r"- (RULED|CORRECTED|APPROVED-PATTERN):\s*(.+)")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "TASTE MARKER" not in raw:
        return []
    # Marker lines arrive JSON-escaped inside transcript lines; scan un-escaped.
    for chunk in raw.splitlines():
        if "TASTE MARKER" not in chunk and not pattern.search(chunk):
            continue
        try:
            unescaped = json.loads(f'"{chunk}"') if "\\n" in chunk else chunk
        except ValueError:
            unescaped = chunk
        for match in pattern.finditer(unescaped.replace("\\n", "\n")):
            line = f"- {match.group(1)}: {match.group(2).strip()}"
            if line not in bullets:
                bullets.append(line)
    return bullets[:10]


def build_excerpt(turns: list[tuple[int, str, str]]) -> str:
    lines = []
    for lineno, role, text in turns:
        if len(text) > TURN_CHAR_CAP:
            text = text[: TURN_CHAR_CAP // 2] + "\n[...turn truncated...]\n" + text[-TURN_CHAR_CAP // 2:]
        lines.append(f"L{lineno}|{role}: {text}")
    excerpt = "\n".join(lines)
    if len(excerpt) <= MAX_EXCERPT_CHARS:
        return excerpt
    head = int(MAX_EXCERPT_CHARS * 0.25)
    tail = MAX_EXCERPT_CHARS - head
    return excerpt[:head] + "\n[... middle of session elided by char cap ...]\n" + excerpt[-tail:]


# ---------------------------------------------------------------- distill

def call_claude(prompt: str) -> str:
    process = subprocess.run(
        ["claude", "-p", "--model", "haiku", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
    )
    if process.returncode != 0:
        raise RuntimeError(f"claude exit {process.returncode}: {process.stderr[:500]}")
    outer = json.loads(process.stdout)
    result = outer.get("result") if isinstance(outer, dict) else None
    if not isinstance(result, str):
        raise RuntimeError("claude output missing result string")
    return result


def parse_specimens(result: str, max_line: int) -> list[dict]:
    text = result.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    specimens = []
    for raw in data.get("specimens") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        content = str(raw.get("content") or "").strip()
        if kind not in SPECIMEN_KINDS or not content:
            continue
        def clamp(value):
            try:
                return max(1, min(int(value), max_line))
            except (TypeError, ValueError):
                return 1
        start, end = clamp(raw.get("line_start")), clamp(raw.get("line_end"))
        specimens.append({
            "kind": kind,
            "content": content,
            "why": str(raw.get("why") or "").strip(),
            "line_start": min(start, end),
            "line_end": max(start, end),
            "scope_hint": str(raw.get("scope_hint") or "unknown").strip(),
        })
    return specimens


def build_candidates(entry: dict, specimens: list[dict], marker: list[str],
                     registry: list[dict]) -> list[dict]:
    candidates = []
    marker_text = "\n".join(marker)
    for specimen in specimens:
        scoping = assign_scope(
            entry.get("project_dir") or "",
            f"{specimen['content']}\n{specimen['why']}\n{specimen['scope_hint']}",
            registry,
        )
        taste = {
            "kind": specimen["kind"],
            "why": specimen["why"],
            "scope": scoping["scope"],
            "marker_derived": bool(marker_text) and specimen["content"] in marker_text,
            "distiller": DISTILLER_ID,
            "session_id": entry["session_id"],
            "project_dir": entry.get("project_dir") or "",
            "line_start": specimen["line_start"],
            "line_end": specimen["line_end"],
        }
        if scoping["flagged"]:
            taste["scope_flag"] = "ambiguous"
            taste["scope_candidates"] = scoping["candidates"]
        candidates.append({
            "source_kind": "session_transcript",
            "source_locator": (f"{entry['transcript_path']}"
                               f"#L{specimen['line_start']}-L{specimen['line_end']}"),
            "content": f"[{specimen['kind']}] {specimen['content']}"
                       + (f"\nWHY: {specimen['why']}" if specimen["why"] else ""),
            "metadata": {
                "session_id": entry["session_id"],
                "project_dir": entry.get("project_dir") or "",
                "tenant_scope": scoping["scope"],
                "marker_derived": taste["marker_derived"],
                "imprint_scope": {"session_id": entry["session_id"]},
            },
            "extensions": {"taste.v1": taste},
        })
    return candidates


def ingest_scan(candidates: list[dict]) -> dict:
    if not candidates:
        return {"items": []}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(candidates, handle, ensure_ascii=False)
        temp = handle.name
    try:
        process = subprocess.run(
            ["imprint", "ingest", "scan", "--input", temp],
            capture_output=True, text=True, timeout=120,
        )
        if process.returncode != 0:
            raise RuntimeError(f"ingest scan exit {process.returncode}: {process.stderr[:500]}")
        return json.loads(process.stdout)
    finally:
        os.unlink(temp)


def distill_session(entry: dict, registry: list[dict], *, dry_run: bool = False,
                    claude_call=call_claude) -> dict:
    path = Path(entry["transcript_path"])
    turns = read_transcript(path)
    if not turns:
        return {"status": "empty", "specimens": 0}
    marker = extract_marker(path)
    marker_clause = MARKER_CLAUSE.format(marker="\n".join(marker)) if marker else ""
    prompt = PROMPT.format(marker_clause=marker_clause, excerpt=build_excerpt(turns))
    specimens = parse_specimens(claude_call(prompt), max_line=turns[-1][0])
    candidates = build_candidates(entry, specimens, marker, registry)
    if dry_run:
        print(json.dumps(candidates, indent=2, ensure_ascii=False))
        return {"status": "dry_run", "specimens": len(candidates)}
    ingest_scan(candidates)
    return {"status": "processed", "specimens": len(candidates),
            "marker_lines": len(marker)}


# ---------------------------------------------------------------- spool loop

def load_ledger() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ledger(ledger: dict) -> None:
    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, target)


def read_spool() -> list[dict]:
    entries: dict[str, dict] = {}
    try:
        raw = spool_path().read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("session_id") and obj.get("transcript_path"):
            prior = entries.get(obj["session_id"])
            if prior is None or obj.get("ts", 0) >= prior.get("ts", 0):
                entries[obj["session_id"]] = obj
    return list(entries.values())


def rewrite_spool(entries: list[dict]) -> None:
    target = spool_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temp, target)


def acquire_lock() -> Path | None:
    lock = spool_path().parent / ".distill.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))
        return lock
    except FileExistsError:
        try:
            pid = int((lock / "pid").read_text())
            os.kill(pid, 0)
            return None                       # live owner; back off
        except (OSError, ValueError):
            pass                              # stale owner: clear, next run works
        try:
            (lock / "pid").unlink(missing_ok=True)
            lock.rmdir()
        except OSError:
            pass
        return None


def release_lock(lock: Path) -> None:
    try:
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        pass


def run(args) -> int:
    lock = acquire_lock()
    if lock is None:
        print("distill: another worker owns the lock (or a stale lock was cleared); exiting")
        return 0
    try:
        ledger = load_ledger()
        registry = load_registry()
        now = time.time()
        keep: list[dict] = []
        processed = 0
        for entry in sorted(read_spool(), key=lambda e: e.get("ts", 0)):
            sid = entry["session_id"]
            if args.session and sid != args.session:
                keep.append(entry)
                continue
            record = ledger.get(sid)
            targeted_force = args.force and args.session == sid
            if record and not targeted_force and (record.get("status") != "error"
                                                  or record.get("attempts", 0) >= RETRY_LIMIT):
                continue                       # done (or given up): drop from spool
            path = Path(entry["transcript_path"])
            if not path.exists():
                if now - entry.get("ts", now) > 7 * 86400:
                    ledger[sid] = {"status": "missing", "ts": now}
                else:
                    keep.append(entry)
                continue
            quiet = args.quiet_seconds if args.quiet_seconds is not None else QUIET_SECONDS
            if not args.force and now - path.stat().st_mtime < quiet:
                keep.append(entry)             # session may still be live
                continue
            if processed >= args.max_sessions:
                keep.append(entry)
                continue
            try:
                result = distill_session(entry, registry, dry_run=args.dry_run)
            except Exception as error:          # noqa: BLE001 — one bad session never stops the drain
                attempts = (record or {}).get("attempts", 0) + 1
                ledger[sid] = {"status": "error", "error": str(error)[:500],
                               "attempts": attempts, "ts": now}
                if attempts < RETRY_LIMIT:
                    keep.append(entry)
                print(f"distill: ERROR {sid}: {error}", file=sys.stderr)
            else:
                processed += 1
                if args.dry_run:
                    keep.append(entry)         # dry runs consume nothing
                else:
                    ledger[sid] = {"status": result["status"], "ts": now,
                                   "specimens": result.get("specimens", 0),
                                   "transcript_bytes": path.stat().st_size}
                print(f"distill: {sid}: {result}")
        if not args.dry_run:
            rewrite_spool(keep)
            save_ledger(ledger)
        print(f"distill: done — {processed} session(s) distilled, {len(keep)} pending")
        return 0
    finally:
        release_lock(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-sessions", type=int, default=40,
                        help="cap per run; launchd fires every 15 min so the rest waits")
    parser.add_argument("--quiet-seconds", type=int, default=None,
                        help=f"idle threshold before a session is distillable (default {QUIET_SECONDS})")
    parser.add_argument("--session", help="only process this session_id")
    parser.add_argument("--force", action="store_true",
                        help="ignore the quiet threshold (use with --session)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print candidates; no ingest, no ledger/spool writes")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
