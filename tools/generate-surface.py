#!/usr/bin/env python3
"""generate-surface.py -- D30-class read-only generator for surface/v1 JSON.

Emits a machine-generated, provenance-tagged state surface for one canonical
environment (Macro Seat spec v1, Artifact 1 / SS1.2). The surface is the atomic
input the Artifact-3 boot-pack assembler folds per repo: identity, cold-load
facts, live state (open PRs, branches, verification), a decision feed, next
lanes, do-not-read surfaces, pitfalls, and a normalized next-session view.

Class D30 (read-only, stateless, non-enforcing, report-only, MANUALLY invoked --
never wired into CI/hooks/gates). Stdlib only, Python 3.9+. The ONLY file it
writes is the surface JSON (its --out path); all other output is status text on
stderr. It never fetches, mutates, commits, or pushes anything in any repo.

Provenance (spec SS1.1/1.2): every leaf entry carries ``source`` + ``as_of``, or
a ``{"degraded": true, "reason": ...}`` marker in place -- probes are NEVER
silently omitted. The assembler renders degraded markers as WARNINGs.

Determinism (spec SS1.1): fixed top-level key order, LC_ALL=C child collation,
stable list ordering (config order preserved; PRs sorted by number; branches
sorted by name), a single trailing newline, and no wall-clock in any DIFFED
content. ``generated_at`` and live-probe ``as_of`` fields hold wall-clock and are
CARVED before diff comparison (spec: "excluded from any diff comparison"); every
repo-fact ``as_of`` is the deterministic HEAD committer date (%cI). Two runs on an
unchanged repo are byte-identical after carving those volatiles.

Probe vocabulary (spec SS1.2 whitelist -- no arbitrary shell):
  git             rev-parse / log / status / rev-list HEAD..origin/main --count
                  (currency read from EXISTING refs; the generator never fetches).
  gh              pr list (degrades gracefully if gh is missing/unauthenticated).
  file-exists     existence test of a repo-relative path.
  script-exit-code  runs ONLY scripts explicitly listed in the config's
                  verification[] block, with a per-probe timeout.

Config resolution (spec SS1.2, first hit wins):
  1. <repo>/.macro/surface-config.json          (repo-local, if present)
  2. tools/configs/<environment>.json (central, shipped)

Output (spec ruling SS6.2): <repo>/.macro/surface.json, local-only and
gitignored. The generator REFUSES to write a path inside the target repo unless
that path is gitignored there -- pass --stdout (or an --out outside the repo) to
bypass. surface.json is generator output: never hand-edit it, re-run instead.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

# Import the M1 next-session parser from the same directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import next_session as ns  # noqa: E402

SCHEMA = "surface/v1"
CONFIG_SCHEMA = "surface-config/v1"
CONFIGS_DIRNAME = "configs"

# Canonical degrade reason enums (closed set, deterministic).
GH_MISSING = "gh_missing"
GH_AUTH_FAILED = "gh_auth_failed"


class GenError(Exception):
    """A hard generation failure (nonzero exit; not a degrade)."""


# --------------------------------------------------------------------------- #
# read-only shell helpers
# --------------------------------------------------------------------------- #
def _c_env():
    """Environment with LC_ALL=C so any child sort/collation is deterministic."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def run(cmd, cwd=None, timeout=None):
    """Run a command read-only; return (returncode, stdout, stderr).

    A timeout or a missing executable is reported as a nonzero rc with the
    reason on stderr -- callers turn that into an honest degrade, never a crash.
    """
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", env=_c_env(), timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except (FileNotFoundError, OSError) as exc:
        return 127, "", "%s: %s" % (type(exc).__name__, exc)


def _degraded(reason):
    return {"degraded": True, "reason": reason}


def _is_degraded(v):
    return isinstance(v, dict) and v.get("degraded") is True


# --------------------------------------------------------------------------- #
# git probes (read existing refs; NEVER fetch)
# --------------------------------------------------------------------------- #
def git_head_sha(repo):
    rc, out, _ = run(["git", "-C", repo, "rev-parse", "HEAD"])
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def git_head_time(repo):
    """HEAD committer date (%cI) -- the deterministic source_commit_time."""
    rc, out, _ = run(["git", "-C", repo, "log", "-1", "--format=%cI", "HEAD"])
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def git_default_branch(repo, declared):
    """Prefer the config's declared default branch; fall back to origin/HEAD."""
    if declared:
        return declared
    rc, out, _ = run(
        ["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    return ""


def git_behind_origin_main(repo):
    """Count commits HEAD is behind origin/main using EXISTING refs (no fetch).

    Returns an int, or a degraded marker if the origin/main ref is absent.
    """
    ref = "refs/remotes/origin/main"
    rc, out, err = run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref])
    if rc != 0 or not out.strip():
        return _degraded("no %s ref (repo not fetched, or non-main default)" % ref)
    rc, out, err = run(
        ["git", "-C", repo, "rev-list", "--count", "HEAD..%s" % ref])
    if rc != 0 or not out.strip().isdigit():
        return _degraded("git rev-list HEAD..%s failed: %s" % (ref, err.strip()))
    return int(out.strip())


def git_active_branches(repo, as_of):
    """Local branch names, sorted (LC_ALL=C). Live repo fact -- volatile as_of."""
    rc, out, _ = run([
        "git", "-C", repo, "for-each-ref", "--format=%(refname:short)",
        "refs/heads"])
    if rc != 0:
        return _degraded("git for-each-ref refs/heads failed")
    names = sorted(n for n in out.splitlines() if n.strip())
    return [{"name": n, "source": "git", "as_of": as_of} for n in names]


# --------------------------------------------------------------------------- #
# gh probe (open PRs)
# --------------------------------------------------------------------------- #
def gh_open_prs(gh_repo, as_of):
    """Return a sorted list of PR entries, or a degraded marker.

    Degrades (never raises) when gh is missing, unauthenticated, or errors.
    """
    if not gh_repo:
        return _degraded("no gh_repo configured")
    if shutil.which("gh") is None:
        return _degraded(GH_MISSING)
    rc, out, _ = run([
        "gh", "pr", "list", "--repo", gh_repo, "--state", "open",
        "--json", "number,title,headRefName,baseRefName", "--limit", "200"])
    if rc != 0:
        auth_rc, _, _ = run(["gh", "auth", "status"])
        if auth_rc != 0:
            return _degraded(GH_AUTH_FAILED)
        return _degraded("gh_pr_list_failed_exit_%d" % rc)
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return _degraded("gh_pr_list_unparseable_json")
    rows = sorted(data, key=lambda p: p.get("number", 0))
    return [
        {
            "number": p.get("number"),
            "title": p.get("title", ""),
            "head": p.get("headRefName", ""),
            "base": p.get("baseRefName", ""),
            "source": "gh",
            "as_of": as_of,
        }
        for p in rows
    ]


# --------------------------------------------------------------------------- #
# file-exists / script-exit-code probes
# --------------------------------------------------------------------------- #
def probe_file_exists(repo, rel_path):
    full = os.path.join(repo, rel_path)
    return os.path.exists(full)


def probe_script_exit_code(repo, script_rel, args, timeout):
    """Run a whitelisted script (from config verification[]) with a timeout.

    Python scripts run under the current interpreter; anything else runs
    directly. Returns (exit_code, degraded_or_none).
    """
    full = os.path.join(repo, script_rel)
    if not os.path.isfile(full):
        return None, _degraded("script not found: %s" % script_rel)
    if script_rel.endswith(".py"):
        cmd = [sys.executable, full]
    else:
        cmd = [full]
    cmd = cmd + [str(a) for a in (args or [])]
    rc, _out, err = run(cmd, cwd=repo, timeout=timeout)
    if rc in (124, 127):
        return None, _degraded("script probe failed (%s): %s" % (rc, err.strip()))
    return rc, None


# --------------------------------------------------------------------------- #
# cold-load probe dispatch
# --------------------------------------------------------------------------- #
def resolve_cold_probe(repo, probe, ctx):
    """Resolve a single cold-load probe to (value, source, as_of) or degraded.

    ctx carries {head_sha, commit_time, now, open_prs}. Returns a dict that is
    either {"value", "source", "as_of"} or a degraded marker.
    """
    kind = probe.get("kind")
    if kind == "git-head":
        v = ctx["head_sha"]
        if v is None:
            return _degraded("git rev-parse HEAD failed")
        return {"value": v, "source": "git", "as_of": ctx["commit_time"]}
    if kind == "git-head-time":
        v = ctx["commit_time"]
        if v is None:
            return _degraded("git log HEAD %cI failed")
        return {"value": v, "source": "git", "as_of": ctx["commit_time"]}
    if kind == "git-behind":
        v = git_behind_origin_main(repo)
        if _is_degraded(v):
            return v
        return {"value": v, "source": "git", "as_of": ctx["commit_time"]}
    if kind == "gh-pr-count":
        prs = ctx["open_prs"]
        if _is_degraded(prs):
            return prs
        return {"value": len(prs), "source": "gh", "as_of": ctx["now"]}
    if kind == "file-exists":
        path = probe.get("path", "")
        if not path:
            return _degraded("file-exists probe missing 'path'")
        return {"value": probe_file_exists(repo, path), "source": "file-exists",
                "as_of": ctx["commit_time"]}
    if kind == "script-exit-code":
        script = probe.get("script", "")
        rc, deg = probe_script_exit_code(
            repo, script, probe.get("args"), probe.get("timeout", 60))
        if deg is not None:
            return deg
        return {"value": rc, "source": "script-exit-code", "as_of": ctx["now"]}
    return _degraded("unknown probe kind: %r" % kind)


def build_cold_load(repo, cfg, ctx):
    """Build the cold_load[] list from config entries (config order preserved).

    Each config entry is either a STATIC fact
        {"claim": "...", "source": "...", "as_of": "..."}
    or a PROBE-DRIVEN fact
        {"claim_template": "...{value}...", "probe": {...},
         "source": <override?>, "as_of": <override?>}
    Probe failures land as a degraded entry in place -- never dropped.
    """
    out = []
    for entry in cfg.get("cold_load", []):
        if "probe" in entry:
            res = resolve_cold_probe(repo, entry["probe"], ctx)
            if _is_degraded(res):
                out.append({
                    "claim": entry.get("claim_template", "").replace(
                        "{value}", "<unavailable>") or entry.get("claim", ""),
                    "degraded": True,
                    "reason": res["reason"],
                })
                continue
            template = entry.get("claim_template", "{value}")
            claim = template.replace("{value}", _fmt(res["value"]))
            out.append({
                "claim": claim,
                "source": entry.get("source", res["source"]),
                "as_of": entry.get("as_of", res["as_of"]),
            })
        else:
            out.append({
                "claim": entry.get("claim", ""),
                "source": entry.get("source", "config"),
                "as_of": entry.get("as_of", ctx["commit_time"] or ""),
            })
    return out


def _fmt(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


# --------------------------------------------------------------------------- #
# state.verification (whitelisted script-exit-code probes)
# --------------------------------------------------------------------------- #
def build_verification(repo, cfg, ctx):
    out = []
    for v in cfg.get("verification", []):
        name = v.get("name", v.get("script", "verification"))
        probe = v.get("probe", {})
        if probe.get("kind") != "script-exit-code":
            out.append({"name": name, "degraded": True,
                        "reason": "verification probe must be script-exit-code"})
            continue
        rc, deg = probe_script_exit_code(
            repo, probe.get("script", ""), probe.get("args"),
            probe.get("timeout", 120))
        if deg is not None:
            out.append({"name": name, "degraded": True, "reason": deg["reason"]})
            continue
        out.append({
            "name": name,
            "checker_exit": rc,
            "notes": v.get("notes", ""),
            "source": "script-exit-code",
            "as_of": ctx["now"],
        })
    return out


# --------------------------------------------------------------------------- #
# static config lists (decisions / next_lanes / do_not_read / pitfalls)
# --------------------------------------------------------------------------- #
def build_decisions(cfg, ctx):
    out = []
    for d in cfg.get("decisions", []):
        out.append({
            "id": d.get("id", ""),
            "summary": d.get("summary", ""),
            "kind": d.get("kind", "ruling"),
            "waiting_since": d.get("waiting_since", ""),
            "source": d.get("source", "config"),
            "as_of": d.get("as_of", ctx["commit_time"] or ""),
        })
    return out


def build_tagged_list(cfg, key, ctx):
    """do_not_read / next_lanes / pitfalls: each entry {claim/path, source, as_of}."""
    out = []
    for e in cfg.get(key, []):
        if isinstance(e, str):
            out.append({"entry": e, "source": "config",
                        "as_of": ctx["commit_time"] or ""})
        else:
            out.append({
                "entry": e.get("entry", ""),
                "source": e.get("source", "config"),
                "as_of": e.get("as_of", ctx["commit_time"] or ""),
            })
    return out


# --------------------------------------------------------------------------- #
# next_session (via the M1 parser)
# --------------------------------------------------------------------------- #
def build_next_session(repo, ctx):
    """Normalized view of the repo's most recent root-level NEXT_SESSION.json.

    Uses the M1 next_session parser. Degraded (never raises) when no root-level
    file exists or the parser cannot recover a record.
    """
    root_file = os.path.join(repo, "NEXT_SESSION.json")
    if not os.path.isfile(root_file):
        return _degraded("no root-level NEXT_SESSION.json")
    try:
        rec = ns.normalize_file(root_file)
    except Exception as exc:  # last-resort guard; parser is designed not to raise
        return _degraded("next_session parser error: %s" % exc)
    if rec.get("unparseable"):
        return _degraded("unparseable NEXT_SESSION.json: %s"
                         % rec.get("error", "unknown"))
    return {
        "source": "next_session.py:%s" % rec.get("detected_schema", "?"),
        "as_of": ctx["now"],
        "normalized": rec.get("normalized", {}),
    }


# --------------------------------------------------------------------------- #
# surface assembly (fixed key order)
# --------------------------------------------------------------------------- #
def build_surface(repo, cfg):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    head_sha = git_head_sha(repo)
    commit_time = git_head_time(repo)
    gh_repo = cfg.get("gh_repo", "")
    open_prs = gh_open_prs(gh_repo, now)

    ctx = {"head_sha": head_sha, "commit_time": commit_time, "now": now,
           "open_prs": open_prs}

    identity = {
        "repo_path": cfg.get("repo_path", repo),
        "canonical": bool(cfg.get("canonical", True)),
        "role": cfg.get("role", ""),
        "default_branch": git_default_branch(repo, cfg.get("default_branch", "")),
        "head_sha": head_sha if head_sha is not None
        else _degraded("git rev-parse HEAD failed"),
        "behind_origin_main": git_behind_origin_main(repo),
        "source": "git",
        "as_of": commit_time if commit_time is not None else now,
    }

    surface = {
        "schema": SCHEMA,
        "environment": cfg.get("environment", os.path.basename(repo)),
        "source_commit_time": commit_time if commit_time is not None
        else _degraded("git log HEAD %cI failed"),
        "generated_at": now,
        "identity": identity,
        "cold_load": build_cold_load(repo, cfg, ctx),
        "state": {
            "open_prs": open_prs,
            "active_branches": git_active_branches(repo, now),
            "verification": build_verification(repo, cfg, ctx),
        },
        "decisions": build_decisions(cfg, ctx),
        "next_lanes": build_tagged_list(cfg, "next_lanes", ctx),
        "do_not_read": build_tagged_list(cfg, "do_not_read", ctx),
        "pitfalls": build_tagged_list(cfg, "pitfalls", ctx),
        "next_session": build_next_session(repo, ctx),
    }
    degraded = _surface_has_degrade(surface)
    return surface, degraded


def _surface_has_degrade(obj):
    """True if any degraded marker appears anywhere in the surface."""
    if isinstance(obj, dict):
        if obj.get("degraded") is True:
            return True
        return any(_surface_has_degrade(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_surface_has_degrade(v) for v in obj)
    return False


def render(surface):
    """Deterministic JSON: fixed insertion order, 2-space indent, one newline."""
    return json.dumps(surface, ensure_ascii=False, indent=2,
                      sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #
def _central_configs_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        CONFIGS_DIRNAME)


def resolve_config(args):
    """Resolve (config_dict, config_path, repo_path) per spec SS1.2 order."""
    # 1. explicit --config
    if args.config:
        cfg_path = os.path.abspath(args.config)
        cfg = _load_config(cfg_path)
        repo = os.path.abspath(args.repo or cfg.get("repo_path") or ".")
        return cfg, cfg_path, repo

    repo = os.path.abspath(args.repo) if args.repo else None

    # 2. repo-local <repo>/.macro/surface-config.json
    if repo:
        local = os.path.join(repo, ".macro", "surface-config.json")
        if os.path.isfile(local):
            cfg = _load_config(local)
            return cfg, local, os.path.abspath(cfg.get("repo_path", repo))

    # 3. central configs/<environment>.json
    env = args.environment or (os.path.basename(repo) if repo else None)
    if not env:
        raise GenError(
            "cannot resolve config: pass --config, or --repo with a repo-local "
            ".macro/surface-config.json, or --environment for a central config")
    central = os.path.join(_central_configs_dir(), env + ".json")
    if not os.path.isfile(central):
        raise GenError("no config for environment %r at %s" % (env, central))
    cfg = _load_config(central)
    repo = os.path.abspath(args.repo or cfg.get("repo_path") or ".")
    return cfg, central, repo


def _load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GenError("cannot load config %s: %s" % (path, exc))
    if not isinstance(cfg, dict):
        raise GenError("config %s is not a JSON object" % path)
    if cfg.get("schema") not in (CONFIG_SCHEMA, None):
        raise GenError("config %s has unexpected schema %r"
                       % (path, cfg.get("schema")))
    return cfg


# --------------------------------------------------------------------------- #
# gitignore guard
# --------------------------------------------------------------------------- #
def _is_inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


def assert_gitignored(repo, out_path):
    """Refuse an in-repo output path unless git ignores it (spec ruling SS6.2)."""
    if not _is_inside(out_path, repo):
        return  # writing outside the target repo -- guard does not apply
    rc, _out, _err = run(["git", "-C", repo, "check-ignore", "--quiet", out_path])
    if rc != 0:
        raise GenError(
            "refusing to write %s: surface.json must be gitignored in the target "
            "repo (spec ruling SS6.2: local-only, gitignored). Add it to "
            "%s/.gitignore, or pass --stdout / --out <path outside repo>."
            % (out_path, repo))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="D30 read-only generator for surface/v1 JSON.")
    ap.add_argument("--repo", default=None,
                    help="target repo root (default: from config repo_path)")
    ap.add_argument("--environment", default=None,
                    help="environment id for central config lookup "
                         "(configs/<environment>.json)")
    ap.add_argument("--config", default=None,
                    help="explicit config path (overrides resolution order)")
    ap.add_argument("--out", default=None,
                    help="output path (default: <repo>/.macro/surface.json)")
    ap.add_argument("--stdout", action="store_true",
                    help="write the surface to stdout instead of a file "
                         "(bypasses the gitignored-path guard)")
    args = ap.parse_args(argv)

    try:
        cfg, cfg_path, repo = resolve_config(args)
        if not os.path.isdir(repo):
            raise GenError("repo path is not a directory: %s" % repo)
        surface, degraded = build_surface(repo, cfg)
        content = render(surface)

        if args.stdout:
            sys.stdout.write(content)
            _report(sys.stderr, cfg_path, repo, degraded, dest="stdout")
            return 0

        out = os.path.abspath(args.out) if args.out \
            else os.path.join(repo, ".macro", "surface.json")
        assert_gitignored(repo, out)
        out_dir = os.path.dirname(out)
        if out_dir and not os.path.isdir(out_dir):
            raise GenError(
                "output parent directory does not exist: %s (create <repo>/.macro "
                "first; the generator never creates directories)" % out_dir)
    except GenError as exc:
        sys.stderr.write("generate-surface: GENERATION FAILURE: %s\n" % exc)
        return 2

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(content)
    _report(sys.stderr, cfg_path, repo, degraded, dest=out)
    return 0


def _report(err, cfg_path, repo, degraded, dest):
    tag = "DEGRADED (probe(s) unavailable; markers inline)" if degraded else "clean"
    err.write("generate-surface: %s surface for %s\n" % (tag, repo))
    err.write("  config: %s\n" % cfg_path)
    err.write("  output: %s\n" % dest)


if __name__ == "__main__":
    raise SystemExit(main())
