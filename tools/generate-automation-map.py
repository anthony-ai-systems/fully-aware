#!/usr/bin/env python3
"""generate-automation-map.py -- D30-class read-only generator for the live
automation map.

Renders one self-contained HTML map (plus a machine-readable JSON sidecar) of
every live automation surface on this machine: launchd LaunchAgents, Codex
pulse automations, Claude Code hooks, the fully-aware surfaced environments,
and the installed skill inventory. The map answers "what runs on this system,
when, driven by what, gated by whom" at a glance -- the plain-language system
map the boot pack can't be (prose) and Mission Control isn't yet (unusable).

Class D30 (read-only, stateless, non-enforcing, report-only, MANUALLY invoked
-- never wired into CI/hooks/gates). Stdlib only, Python 3.9+. The ONLY files
it writes are its two --out paths (default: state/automation-map.html and
state/automation-map.json, both under the gitignored state/). It never
mutates, commits, or pushes anything in any repo.

Provenance: every node carries ``source`` (the path or command probed) and a
``status``; a probe that fails yields a node or field with
``{"degraded": true, "reason": ...}`` -- probes are NEVER silently omitted.

Determinism: nodes sorted by (lane, id); ``generated_at`` is the only
wall-clock field and is CARVED before any diff comparison. Two runs on an
unchanged system are byte-identical after carving it.

Color semantics (legend is embedded in the HTML):
  purple  = an AI agent executes the step (Claude hook agents, Codex automations)
  teal    = a plain script/daemon executes it (no model in the loop)
  green   = an artifact / store / external surface (data, not an actor)
  amber left edge = HUMAN-GATED: the node only proposes; acting on its output
          (merge, arm, adjudicate) is Anthony's. "Merge is Anthony's" made visible.
  red dot = probe says the thing is failing (nonzero last exit, missing artifact)

Flow edges are HAND-MAINTAINED topology (same doctrine as
tools/configs/seed-manifest.json): stable, known data flows only, declared in
FLOWS below. An edge renders only when both endpoints were actually observed
this run -- the topology can claim a flow, but a dead endpoint drops it.

Usage:
  python3 tools/generate-automation-map.py                # writes into state/
  python3 tools/generate-automation-map.py --stdout       # JSON to stdout
  python3 tools/generate-automation-map.py --home /tmp/x  # test harness root
"""

from __future__ import annotations

import argparse
import datetime
import glob
import html
import json
import os
import plistlib
import re
import subprocess
import sys

SCHEMA = "automation-map/v1"

# --------------------------------------------------------------------------- #
# hand-maintained topology (seed-manifest doctrine: edited by hand, on purpose)
# --------------------------------------------------------------------------- #

# Known human-gated nodes: their output waits on Anthony (merge / arm / rule).
HUMAN_GATED = {
    "la:com.anthonyflores.fully-aware.daily-scan",   # scan proposes; review is Anthony's
    "cx:github-coach-midday-loss-prevention-scan",   # reconciliation report -> Anthony acts
    "cx:github-coach-nightly-fleet-reconciliation",
    "cx:launchpad-autofeed",                         # dry-run until Anthony flips live
    "cx:daily-gmail-action-inbox-sweep",             # action inbox -> Anthony clears
}

# Declared artifact / store nodes (green): existence-probed each run.
ARTIFACTS = [
    ("art:boot-pack", "BOOT-PACK.md", "morning boot pack",
     "~/code/fully-aware/state/BOOT-PACK.md"),
    ("art:boot-digest", "BOOT-DIGEST.md", "slim session digest",
     "~/code/fully-aware/state/BOOT-DIGEST.md"),
    ("art:surfaces", "surfaces cache", "per-repo surface/v1 JSON",
     "~/code/fully-aware/state/surfaces"),
    ("art:imprint-store", "imprint store export", "captured judgment (bulk channel)",
     "~/code/fully-aware/state/imprint-store.md"),
    ("art:iris-events", "agent-sessions events", "IRIS events.jsonl (session pulses)",
     "~/Library/Application Support/IRIS/agent-sessions/events.jsonl"),
    ("art:client-work-board", "client-work board", "IRIS board data (serve_board.py :4180)",
     "~/code/anthony-wiki-vault/IRIS/client-work-board"),
    ("art:daily-scan", "daily-scan output", "Codex scan pipeline results",
     "~/code/fully-aware/state/daily-scan"),
]

# (src, dst) stable data flows. Rendered only if both nodes observed this run.
FLOWS = [
    ("hook:agent_session_pulse.py", "art:iris-events"),
    ("art:iris-events", "cx:iris-client-work-pulse"),
    ("cx:iris-client-work-pulse", "art:client-work-board"),
    ("la:com.anthonyflores.iris-client-work-board", "art:client-work-board"),
    ("hook:stop_capture.py", "art:imprint-store"),
    ("hook:user_prompt_submit.py", "art:imprint-store"),
    ("art:imprint-store", "la:com.anthonyflores.fully-aware.boot-pack"),
    ("env:*", "art:surfaces"),
    ("art:surfaces", "la:com.anthonyflores.fully-aware.boot-pack"),
    ("la:com.anthonyflores.fully-aware.boot-pack", "art:boot-pack"),
    ("art:boot-pack", "art:boot-digest"),
    ("art:boot-digest", "hook:session-digest-hook.sh"),
    ("la:com.anthonyflores.fully-aware.daily-scan", "art:daily-scan"),
]

LANES = [
    ("hooks", "Session hooks (Claude Code)"),
    ("launchd", "Scheduled — launchd"),
    ("codex", "Codex automations"),
    ("artifacts", "Artifacts & stores"),
    ("envs", "Surfaced environments"),
]


class GenError(Exception):
    """A hard generation failure (nonzero exit; not a degrade)."""


def _expand(path, home):
    """Expand ~ against an explicit home root (test harness support)."""
    if path.startswith("~"):
        return os.path.join(home, path[2:]) if path != "~" else home
    return path


# --------------------------------------------------------------------------- #
# probes (each returns a list of node dicts; failures degrade, never raise)
# --------------------------------------------------------------------------- #

def _launchctl_state(runner):
    """label -> (pid_or_None, last_exit_or_None); degraded dict on failure."""
    try:
        out = runner(["launchctl", "list"])
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the map
        return {"degraded": True, "reason": "launchctl_failed: %s" % exc}
    state = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = parts
        state[label] = (None if pid == "-" else pid,
                        None if status == "-" else status)
    return state


def _fmt_schedule(pl):
    cal = pl.get("StartCalendarInterval")
    if cal is not None:
        entries = cal if isinstance(cal, list) else [cal]
        times = ["%02d:%02d" % (e.get("Hour", 0), e.get("Minute", 0))
                 for e in entries]
        return "daily " + " + ".join(sorted(times))
    if pl.get("StartInterval"):
        return "every %ss" % pl["StartInterval"]
    if pl.get("KeepAlive"):
        return "keep-alive daemon"
    if pl.get("RunAtLoad"):
        return "at load"
    return "no schedule declared"


# Personal-agent prefixes shown by default; third-party updaters (Adobe,
# Google, ...) are noise for this map. --all-launchagents lifts the filter.
LAUNCHD_PREFIXES = ("com.anthony",)


def _load_plist(path):
    """plistlib, falling back to `plutil -convert xml1` for plists that
    launchd tolerates but expat rejects (e.g. `--` inside XML comments)."""
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:  # noqa: BLE001 - try the OS parser before degrading
        out = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", path],
                             capture_output=True, timeout=10, check=True)
        return plistlib.loads(out.stdout)


def probe_launchagents(home, runner, all_agents=False):
    la_dir = os.path.join(home, "Library", "LaunchAgents")
    loaded = _launchctl_state(runner)
    loaded_degraded = isinstance(loaded, dict) and loaded.get("degraded")
    nodes = []
    for path in sorted(glob.glob(os.path.join(la_dir, "*.plist"))):
        if not all_agents and not os.path.basename(path).startswith(
                LAUNCHD_PREFIXES):
            continue
        try:
            pl = _load_plist(path)
        except Exception as exc:  # noqa: BLE001
            nodes.append({
                "id": "la:" + os.path.basename(path),
                "lane": "launchd", "title": os.path.basename(path),
                "subtitle": "unreadable plist", "executor": "script",
                "status": "unknown", "source": path,
                "degraded": True, "reason": str(exc),
            })
            continue
        label = pl.get("Label", os.path.basename(path)[:-6])
        args = pl.get("ProgramArguments") or [pl.get("Program", "?")]
        program = " ".join(str(a) for a in args)
        status, status_note = "unknown", "launchctl state unavailable"
        if not loaded_degraded:
            if label in loaded:
                pid, last_exit = loaded[label]
                if pid is not None:
                    status, status_note = "ok", "running (pid %s)" % pid
                elif last_exit in (None, "0"):
                    status, status_note = "ok", "loaded, last exit 0"
                else:
                    status, status_note = "failing", "loaded, last exit %s" % last_exit
            else:
                status, status_note = "unarmed", "not loaded in launchctl"
        node = {
            "id": "la:" + label, "lane": "launchd", "title": label,
            "subtitle": _fmt_schedule(pl), "executor": "script",
            "status": status, "source": path,
            "detail": {"program": program, "status_note": status_note,
                       "stdout": pl.get("StandardOutPath"),
                       "stderr": pl.get("StandardErrorPath")},
        }
        nodes.append(node)
    if not nodes:
        nodes.append({"id": "la:none", "lane": "launchd",
                      "title": "no LaunchAgents found", "subtitle": la_dir,
                      "executor": "script", "status": "unknown",
                      "source": la_dir, "degraded": True,
                      "reason": "empty_or_missing_dir"})
    return nodes


_TOML_KEY = re.compile(r'^\s*(name|model|status|schedule|rrule|enabled)\s*=\s*"?([^"\n]*)"?\s*$')


def probe_codex_automations(home):
    """Light line-based read of ~/.codex/automations/*/automation.toml.

    Deliberately NOT a TOML parser (tomllib is 3.11+; this repo floors at 3.9):
    we only lift a few scalar keys and any FREQ= rrule text, and degrade
    per-file on anything unreadable.
    """
    base = os.path.join(home, ".codex", "automations")
    nodes = []
    for path in sorted(glob.glob(os.path.join(base, "*", "automation.toml"))):
        slug = os.path.basename(os.path.dirname(path))
        info = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _TOML_KEY.match(line)
                    if m and m.group(1) not in info:
                        info[m.group(1)] = m.group(2).strip()
                    elif "FREQ=" in line and "schedule" not in info:
                        info["schedule"] = line.split("=", 1)[-1].strip().strip('"')
        except Exception as exc:  # noqa: BLE001
            nodes.append({"id": "cx:" + slug, "lane": "codex", "title": slug,
                          "subtitle": "unreadable automation.toml",
                          "executor": "ai", "status": "unknown",
                          "source": path, "degraded": True, "reason": str(exc)})
            continue
        sched = info.get("schedule") or info.get("rrule") or "schedule unknown"
        nodes.append({
            "id": "cx:" + slug, "lane": "codex",
            "title": info.get("name", slug), "subtitle": sched,
            "executor": "ai", "status": "ok", "source": path,
            "detail": {"model": info.get("model"),
                       "declared_status": info.get("status") or info.get("enabled")},
        })
    if not nodes:
        nodes.append({"id": "cx:none", "lane": "codex",
                      "title": "no Codex automations found", "subtitle": base,
                      "executor": "ai", "status": "unknown", "source": base,
                      "degraded": True, "reason": "empty_or_missing_dir"})
    return nodes


_SCRIPT_EXTS = (".py", ".sh", ".mjs", ".js", ".rb", ".pl")


def _script_name(cmd):
    """The script a hook command runs: first token with a script extension
    (commands may carry trailing args like `... collect`), else last token."""
    tokens = cmd.split()
    if not tokens:
        return "?"
    for tok in tokens:
        if tok.endswith(_SCRIPT_EXTS):
            return os.path.basename(tok)
    return os.path.basename(tokens[-1])


def probe_claude_hooks(home):
    settings_path = os.path.join(home, ".claude", "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as fh:
            hooks_cfg = json.load(fh).get("hooks", {})
    except Exception as exc:  # noqa: BLE001
        return [{"id": "hook:none", "lane": "hooks",
                 "title": "settings.json unreadable", "subtitle": settings_path,
                 "executor": "ai", "status": "unknown", "source": settings_path,
                 "degraded": True, "reason": str(exc)}]
    by_script = {}
    for event in sorted(hooks_cfg):
        for matcher_block in hooks_cfg[event]:
            for hook in matcher_block.get("hooks", []):
                cmd = hook.get("command", "")
                script = _script_name(cmd)
                entry = by_script.setdefault(script, {"events": [], "command": cmd})
                tag = event + (":" + matcher_block["matcher"]
                               if matcher_block.get("matcher") else "")
                if tag not in entry["events"]:
                    entry["events"].append(tag)
    nodes = []
    for script in sorted(by_script):
        entry = by_script[script]
        # Heuristic: an interpreter-run capture/pulse script is an AI-adjacent
        # sensor; classification only affects color, never data.
        executor = "ai" if script.endswith(".py") else "script"
        nodes.append({
            "id": "hook:" + script, "lane": "hooks", "title": script,
            "subtitle": ", ".join(entry["events"]), "executor": executor,
            "status": "ok", "source": settings_path,
            "detail": {"command": entry["command"]},
        })
    return nodes or [{"id": "hook:none", "lane": "hooks", "title": "no hooks wired",
                      "subtitle": settings_path, "executor": "ai",
                      "status": "unknown", "source": settings_path,
                      "degraded": True, "reason": "no_hooks_key"}]


def probe_artifacts(home):
    nodes = []
    for node_id, title, subtitle, raw_path in ARTIFACTS:
        path = _expand(raw_path, home)
        exists = os.path.exists(path)
        detail = {"path": path}
        if exists and os.path.isfile(path):
            st = os.stat(path)
            detail["size_bytes"] = st.st_size
            detail["mtime"] = datetime.datetime.fromtimestamp(
                st.st_mtime, tz=datetime.timezone.utc).isoformat(timespec="seconds")
        nodes.append({
            "id": node_id, "lane": "artifacts", "title": title,
            "subtitle": subtitle, "executor": "external",
            "status": "ok" if exists else "failing", "source": path,
            "detail": detail,
            **({} if exists else {"degraded": True, "reason": "missing_path"}),
        })
    return nodes


def probe_environments(repo_root):
    cfg_dir = os.path.join(repo_root, "tools", "configs")
    skip = {"ratification-backlog.json", "seed-manifest.json"}
    nodes = []
    for path in sorted(glob.glob(os.path.join(cfg_dir, "*.json"))):
        base = os.path.basename(path)
        if base in skip:
            continue
        env = base[:-5]
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            repo = cfg.get("repo") or cfg.get("path") or ""
        except Exception:  # noqa: BLE001
            repo = ""
        nodes.append({"id": "env:" + env, "lane": "envs", "title": env,
                      "subtitle": repo or "surface config", "executor": "external",
                      "status": "ok", "source": path, "detail": {}})
    return nodes


def probe_skills(home):
    skills_dir = os.path.join(home, ".claude", "skills")
    try:
        names = sorted(d for d in os.listdir(skills_dir)
                       if os.path.isdir(os.path.join(skills_dir, d)))
    except OSError:
        names = []
    return [{"id": "skills:all", "lane": "envs",
             "title": "Claude skills (%d installed)" % len(names),
             "subtitle": "~/.claude/skills", "executor": "ai",
             "status": "ok" if names else "unknown", "source": skills_dir,
             "detail": {"skills": names},
             **({} if names else {"degraded": True, "reason": "empty_or_missing_dir"})}]


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #

def _default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10,
                          check=True).stdout


def build_map(home, repo_root, runner=_default_runner, now=None,
              all_agents=False):
    nodes = (probe_claude_hooks(home) +
             probe_launchagents(home, runner, all_agents=all_agents) +
             probe_codex_automations(home) + probe_artifacts(home) +
             probe_environments(repo_root) + probe_skills(home))
    for n in nodes:
        n["human_gated"] = n["id"] in HUMAN_GATED
    lane_order = {k: i for i, (k, _t) in enumerate(LANES)}
    nodes.sort(key=lambda n: (lane_order.get(n["lane"], 99), n["id"]))
    ids = {n["id"] for n in nodes}
    env_ids = sorted(i for i in ids if i.startswith("env:") and i != "env:*")
    edges = []
    for src, dst in FLOWS:
        srcs = env_ids if src == "env:*" else [src]
        for s in srcs:
            if s in ids and dst in ids:
                edges.append({"from": s, "to": dst})
    generated_at = (now or datetime.datetime.now(datetime.timezone.utc)
                    ).isoformat(timespec="seconds")
    return {"schema": SCHEMA, "generated_at": generated_at,
            "advisory": "ADVISORY STATE, NOT LAW — read-only map of observed "
                        "automation surfaces; probes degrade, never guess.",
            "lanes": [{"key": k, "title": t} for k, t in LANES],
            "nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

_CSS = """
:root { --bg:#0b0e14; --panel:#11151f; --card:#161b28; --edge:#2a3040;
  --text:#dbe2f0; --dim:#7d8799; --ai:#a78bfa; --script:#2dd4bf;
  --external:#4ade80; --gate:#fbbf24; --fail:#f87171; --ok:#4ade80; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--text); min-height:100vh;
  font:14px/1.45 -apple-system,"SF Pro Text","Segoe UI",sans-serif; }
header { padding:22px 28px 10px; }
h1 { font-size:20px; letter-spacing:.2px; }
h1 span { color:var(--ai); }
.meta { color:var(--dim); font-size:12px; margin-top:4px; }
.legend { display:flex; gap:16px; flex-wrap:wrap; padding:8px 28px 14px;
  color:var(--dim); font-size:12px; align-items:center; }
.chip { display:inline-block; width:10px; height:10px; border-radius:3px;
  margin-right:5px; vertical-align:-1px; }
#wrap { position:relative; padding:0 28px 40px; }
#lanes { display:flex; gap:26px; align-items:flex-start; position:relative;
  z-index:2; }
.lane { flex:1 1 0; min-width:180px; }
.lane h2 { font-size:11px; text-transform:uppercase; letter-spacing:.14em;
  color:var(--dim); margin-bottom:12px; font-weight:600; }
.node { background:var(--card); border:1px solid var(--edge); border-radius:9px;
  padding:9px 11px; margin-bottom:10px; cursor:pointer; position:relative;
  border-left:3px solid var(--script); transition:border-color .15s, opacity .15s; }
.node.ai { border-left-color:var(--ai); }
.node.external { border-left-color:var(--external); }
.node.gated { box-shadow:inset 0 0 0 1px rgba(251,191,36,.35); }
.node.dim { opacity:.25; }
.node.lit { border-color:var(--gate); }
.node .t { font-weight:600; font-size:13px; word-break:break-word; }
.node .s { color:var(--dim); font-size:11.5px; margin-top:2px;
  word-break:break-all; }
.dot { position:absolute; top:10px; right:10px; width:8px; height:8px;
  border-radius:50%; background:var(--dim); }
.dot.ok { background:var(--ok); } .dot.failing { background:var(--fail); }
.dot.unarmed { background:var(--gate); }
.badge { display:inline-block; margin-top:5px; font-size:10px; color:var(--gate);
  border:1px solid rgba(251,191,36,.4); border-radius:4px; padding:1px 5px; }
#svg { position:absolute; inset:0; z-index:1; pointer-events:none; }
#svg path { stroke:var(--edge); stroke-width:1.5; fill:none; opacity:.7; }
#svg path.lit { stroke:var(--gate); opacity:1; stroke-width:2; }
#panel { position:fixed; top:0; right:-420px; width:400px; height:100vh;
  background:var(--panel); border-left:1px solid var(--edge); padding:24px;
  overflow-y:auto; transition:right .2s ease; z-index:5; }
#panel.open { right:0; }
#panel h3 { font-size:16px; margin-bottom:2px; }
#panel .close { position:absolute; top:14px; right:16px; color:var(--dim);
  cursor:pointer; font-size:18px; }
#panel dl { margin-top:14px; font-size:12.5px; }
#panel dt { color:var(--dim); text-transform:uppercase; font-size:10px;
  letter-spacing:.1em; margin-top:12px; }
#panel dd { margin-top:2px; word-break:break-all; white-space:pre-wrap; }
#panel .gatenote { margin-top:14px; color:var(--gate); font-size:12px; }
"""

_JS = """
const DATA = JSON.parse(document.getElementById('map-data').textContent);
const byId = {}; DATA.nodes.forEach(n => byId[n.id] = n);
const svg = document.getElementById('svg');
function anchors(el, side) {
  const w = document.getElementById('wrap').getBoundingClientRect();
  const r = el.getBoundingClientRect();
  return [side === 'r' ? r.right - w.left : r.left - w.left,
          r.top - w.top + r.height / 2];
}
function draw() {
  svg.innerHTML = '';
  const wrap = document.getElementById('wrap');
  svg.setAttribute('width', wrap.scrollWidth);
  svg.setAttribute('height', wrap.scrollHeight);
  DATA.edges.forEach(e => {
    // getElementById takes a raw id (not a selector) — no escaping, even
    // though node ids contain ':' and '.'.
    const a = document.getElementById('n-' + e.from);
    const b = document.getElementById('n-' + e.to);
    if (!a || !b) return;
    const rev = a.getBoundingClientRect().left > b.getBoundingClientRect().left;
    const [x1, y1] = anchors(a, rev ? 'l' : 'r');
    const [x2, y2] = anchors(b, rev ? 'r' : 'l');
    const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (rev ? -1 : 1);
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
    p.dataset.from = e.from; p.dataset.to = e.to;
    svg.appendChild(p);
  });
}
function highlight(id) {
  const linked = new Set([id]);
  svg.querySelectorAll('path').forEach(p => {
    const hit = p.dataset.from === id || p.dataset.to === id;
    p.classList.toggle('lit', hit);
    if (hit) { linked.add(p.dataset.from); linked.add(p.dataset.to); }
  });
  document.querySelectorAll('.node').forEach(el => {
    el.classList.toggle('dim', !linked.has(el.dataset.id));
    el.classList.toggle('lit', linked.has(el.dataset.id) && el.dataset.id !== id);
  });
}
function clearHl() {
  svg.querySelectorAll('path').forEach(p => p.classList.remove('lit'));
  document.querySelectorAll('.node').forEach(el =>
    el.classList.remove('dim', 'lit'));
}
const panel = document.getElementById('panel');
function show(n) {
  document.getElementById('p-title').textContent = n.title;
  document.getElementById('p-sub').textContent = n.subtitle || '';
  const dl = document.getElementById('p-dl'); dl.innerHTML = '';
  const rows = Object.assign({status: n.status, source: n.source},
                             n.degraded ? {degraded: n.reason} : {},
                             n.detail || {});
  for (const [k, v] of Object.entries(rows)) {
    if (v === null || v === undefined || v === '') continue;
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = Array.isArray(v) ? v.join(', ') : String(v);
    dl.append(dt, dd);
  }
  document.getElementById('p-gate').textContent = n.human_gated
    ? 'HUMAN-GATED — this node only proposes; acting on its output is Anthony\\'s.'
    : '';
  panel.classList.add('open');
}
document.querySelectorAll('.node').forEach(el => {
  el.addEventListener('mouseenter', () => highlight(el.dataset.id));
  el.addEventListener('mouseleave', clearHl);
  el.addEventListener('click', () => show(byId[el.dataset.id]));
});
document.getElementById('p-close').addEventListener('click',
  () => panel.classList.remove('open'));
window.addEventListener('resize', draw);
draw();
"""


def render_html(data):
    lanes_html = []
    for lane in data["lanes"]:
        cards = []
        for n in data["nodes"]:
            if n["lane"] != lane["key"]:
                continue
            cls = "node %s%s" % (n["executor"],
                                 " gated" if n.get("human_gated") else "")
            badge = '<span class="badge">human-gated</span>' \
                if n.get("human_gated") else ""
            cards.append(
                '<div class="%s" id="n-%s" data-id="%s">'
                '<span class="dot %s"></span>'
                '<div class="t">%s</div><div class="s">%s</div>%s</div>'
                % (cls, html.escape(n["id"], quote=True),
                   html.escape(n["id"], quote=True), n["status"],
                   html.escape(n["title"]), html.escape(n.get("subtitle") or ""),
                   badge))
        lanes_html.append('<div class="lane"><h2>%s</h2>%s</div>'
                          % (html.escape(lane["title"]), "".join(cards)))
    legend = (
        '<span><span class="chip" style="background:var(--ai)"></span>AI executes</span>'
        '<span><span class="chip" style="background:var(--script)"></span>script/daemon</span>'
        '<span><span class="chip" style="background:var(--external)"></span>artifact / store</span>'
        '<span><span class="chip" style="background:var(--gate)"></span>human-gated (proposes only)</span>'
        '<span><span class="chip" style="background:var(--fail)"></span>failing probe</span>')
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Automation Map</title><style>%s</style></head><body>"
            "<header><h1>Automation <span>Map</span></h1>"
            "<div class='meta'>generated %s · %s · advisory, read-only</div></header>"
            "<div class='legend'>%s</div>"
            "<div id='wrap'><svg id='svg'></svg><div id='lanes'>%s</div></div>"
            "<div id='panel'><span class='close' id='p-close'>&times;</span>"
            "<h3 id='p-title'></h3><div class='meta' id='p-sub'></div>"
            "<dl id='p-dl'></dl><div class='gatenote' id='p-gate'></div></div>"
            "<script type='application/json' id='map-data'>%s</script>"
            "<script>%s</script></body></html>"
            % (_CSS, html.escape(data["generated_at"]), SCHEMA, legend,
               "".join(lanes_html),
               json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
               _JS))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=os.path.expanduser("~"),
                    help="root for ~ expansion (test harness)")
    ap.add_argument("--repo-root", default=repo_root)
    ap.add_argument("--out-html",
                    default=os.path.join(repo_root, "state", "automation-map.html"))
    ap.add_argument("--out-json",
                    default=os.path.join(repo_root, "state", "automation-map.json"))
    ap.add_argument("--stdout", action="store_true",
                    help="print JSON to stdout, write nothing")
    ap.add_argument("--all-launchagents", action="store_true",
                    help="include third-party LaunchAgents (default: %s*)"
                         % "/".join(LAUNCHD_PREFIXES))
    args = ap.parse_args(argv)

    data = build_map(args.home, args.repo_root,
                     all_agents=args.all_launchagents)
    if args.stdout:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    os.makedirs(os.path.dirname(args.out_html), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(args.out_html, "w", encoding="utf-8") as fh:
        fh.write(render_html(data))
    degraded = sum(1 for n in data["nodes"] if n.get("degraded"))
    failing = sum(1 for n in data["nodes"] if n["status"] == "failing")
    sys.stderr.write("automation-map: %d nodes, %d edges, %d degraded, "
                     "%d failing -> %s\n" % (len(data["nodes"]),
                                             len(data["edges"]), degraded,
                                             failing, args.out_html))
    return 0


if __name__ == "__main__":
    sys.exit(main())
