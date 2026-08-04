#!/usr/bin/env python3
"""Tests for generate-surface.py -- stdlib unittest, no third-party deps.

All fixtures are SYNTHETIC. No real client names, no real repo content. Each test
builds a throwaway git repo in a tempdir. Run with:

    python3 -m unittest test_generate_surface -v

The generator filename has a hyphen, so it is loaded via importlib.
"""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_surface", os.path.join(_HERE, "generate-surface.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load_generator()


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_repo(tmp):
    """A minimal git repo with one commit; returns its path."""
    repo = os.path.join(tmp, "synthrepo")
    os.makedirs(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    with open(os.path.join(repo, "README"), "w") as fh:
        fh.write("synthetic\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    return repo


def _base_config(repo, **over):
    cfg = {
        "schema": "surface-config/v1",
        "environment": "synthrepo",
        "repo_path": repo,
        "canonical": True,
        "role": "synthetic test environment",
        "default_branch": "main",
        "gh_repo": "",
        "cold_load": [
            {"claim_template": "HEAD is {value}", "probe": {"kind": "git-head"}},
            {"claim": "a static fact", "source": "config", "as_of": "2026-01-01"},
        ],
        "verification": [],
        "decisions": [
            {"id": "D-1", "summary": "a synthetic ruling awaiting sign-off",
             "kind": "ruling", "waiting_since": "2026-01-01", "source": "config"},
        ],
        "next_lanes": [{"entry": "L-1 -- synthetic lane", "source": "config",
                        "as_of": "2026-01-01"}],
        "do_not_read": [{"entry": "junk/ -- synthetic", "source": "config",
                         "as_of": "2026-01-01"}],
        "pitfalls": [{"entry": "a synthetic pitfall", "source": "config",
                      "as_of": "2026-01-01"}],
    }
    cfg.update(over)
    return cfg


class SchemaShape(unittest.TestCase):
    def test_top_level_shape_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            cfg = _base_config(repo)
            surface, degraded = gs.build_surface(repo, cfg)
            self.assertEqual(
                list(surface.keys()),
                ["schema", "environment", "source_commit_time", "generated_at",
                 "identity", "cold_load", "state", "decisions", "next_lanes",
                 "do_not_read", "pitfalls", "next_session"])
            self.assertEqual(surface["schema"], "surface/v1")
            self.assertEqual(surface["environment"], "synthrepo")
            ident = surface["identity"]
            for k in ("repo_path", "canonical", "role", "default_branch",
                      "head_sha", "behind_origin_main", "source", "as_of"):
                self.assertIn(k, ident)
            self.assertIn("open_prs", surface["state"])
            self.assertIn("active_branches", surface["state"])
            self.assertIn("verification", surface["state"])

    def test_every_leaf_entry_has_provenance_or_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            surface, _ = gs.build_surface(repo, _base_config(repo))
            buckets = ["cold_load", "decisions", "next_lanes", "do_not_read",
                       "pitfalls"]
            for b in buckets:
                for e in surface[b]:
                    ok = e.get("degraded") is True or (
                        "source" in e and "as_of" in e)
                    self.assertTrue(ok, "%s entry lacks provenance: %r" % (b, e))
            for e in surface["state"]["active_branches"]:
                self.assertIn("source", e)
                self.assertIn("as_of", e)


class DegradedPath(unittest.TestCase):
    def test_gh_unconfigured_degrades_open_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            cfg = _base_config(repo, gh_repo="", cold_load=[
                {"claim_template": "open PRs {value}",
                 "probe": {"kind": "gh-pr-count"}}])
            surface, degraded = gs.build_surface(repo, cfg)
            self.assertTrue(degraded)
            self.assertTrue(gs._is_degraded(surface["state"]["open_prs"]))
            self.assertIn("reason", surface["state"]["open_prs"])
            # the cold_load gh-pr-count entry degrades in place, never omitted
            self.assertEqual(len(surface["cold_load"]), 1)
            self.assertTrue(surface["cold_load"][0].get("degraded"))

    def test_missing_next_session_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            surface, _ = gs.build_surface(repo, _base_config(repo))
            self.assertTrue(gs._is_degraded(surface["next_session"]))

    def test_missing_script_probe_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            cfg = _base_config(repo, verification=[
                {"name": "absent-check", "probe": {
                    "kind": "script-exit-code", "script": "_tools/nope.py",
                    "args": [], "timeout": 5}}])
            surface, degraded = gs.build_surface(repo, cfg)
            self.assertTrue(degraded)
            v = surface["state"]["verification"][0]
            self.assertTrue(v.get("degraded"))
            self.assertIn("not found", v["reason"])

    def test_present_script_probe_records_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            os.makedirs(os.path.join(repo, "_tools"))
            with open(os.path.join(repo, "_tools", "ok.py"), "w") as fh:
                fh.write("import sys; sys.exit(0)\n")
            cfg = _base_config(repo, verification=[
                {"name": "ok-check", "probe": {
                    "kind": "script-exit-code", "script": "_tools/ok.py",
                    "args": [], "timeout": 10}}])
            surface, _ = gs.build_surface(repo, cfg)
            v = surface["state"]["verification"][0]
            self.assertEqual(v["checker_exit"], 0)
            self.assertEqual(v["source"], "script-exit-code")


class Determinism(unittest.TestCase):
    @staticmethod
    def _carve(surface):
        """Recursively replace volatile as_of/generated_at with a sentinel."""
        def scrub(x):
            if isinstance(x, dict):
                for k in list(x):
                    if k in ("generated_at", "as_of"):
                        x[k] = "<carved>"
                    else:
                        scrub(x[k])
            elif isinstance(x, list):
                for i in x:
                    scrub(i)
        scrub(surface)
        return surface

    def test_two_runs_identical_after_carving_volatiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            cfg = _base_config(repo)
            s1, _ = gs.build_surface(repo, cfg)
            s2, _ = gs.build_surface(repo, cfg)
            a = json.dumps(self._carve(s1), sort_keys=True)
            b = json.dumps(self._carve(s2), sort_keys=True)
            self.assertEqual(a, b)

    def test_render_single_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            surface, _ = gs.build_surface(repo, _base_config(repo))
            text = gs.render(surface)
            self.assertTrue(text.endswith("}\n"))
            self.assertFalse(text.endswith("}\n\n"))

    def test_repo_fact_as_of_is_commit_time_not_wallclock(self):
        # cold_load git-head entry's as_of must be the deterministic %cI, so it
        # survives carving as a stable value across runs.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            cfg = _base_config(repo, cold_load=[
                {"claim_template": "HEAD {value}", "probe": {"kind": "git-head"}}])
            s1, _ = gs.build_surface(repo, cfg)
            s2, _ = gs.build_surface(repo, cfg)
            self.assertEqual(s1["cold_load"][0]["as_of"],
                             s2["cold_load"][0]["as_of"])


class ConfigResolution(unittest.TestCase):
    def _args(self, **kw):
        ns = type("A", (), {})()
        ns.repo = kw.get("repo")
        ns.environment = kw.get("environment")
        ns.config = kw.get("config")
        ns.out = kw.get("out")
        ns.stdout = kw.get("stdout", False)
        return ns

    def test_explicit_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            p = os.path.join(tmp, "explicit.json")
            with open(p, "w") as fh:
                json.dump(_base_config(repo, environment="explicit-env"), fh)
            cfg, path, r = gs.resolve_config(self._args(config=p, repo=repo))
            self.assertEqual(cfg["environment"], "explicit-env")
            self.assertEqual(path, os.path.abspath(p))

    def test_repo_local_beats_central(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            os.makedirs(os.path.join(repo, ".macro"))
            local = os.path.join(repo, ".macro", "surface-config.json")
            with open(local, "w") as fh:
                json.dump(_base_config(repo, environment="repo-local-env"), fh)
            # A central config with the same env name should be shadowed.
            cfg, path, r = gs.resolve_config(
                self._args(repo=repo, environment="synthrepo"))
            self.assertEqual(cfg["environment"], "repo-local-env")
            self.assertEqual(path, local)

    def test_central_fallback_by_environment(self):
        # The shipped central configs resolve by --environment with no repo-local.
        cfg, path, repo = gs.resolve_config(
            self._args(environment="saga-protocol"))
        self.assertEqual(cfg["environment"], "saga-protocol")
        self.assertTrue(path.endswith("configs/saga-protocol.json"))

    def test_unresolvable_config_raises(self):
        with self.assertRaises(gs.GenError):
            gs.resolve_config(self._args(environment="no-such-env-xyz"))


class GitignoreGuard(unittest.TestCase):
    def test_refuses_non_ignored_in_repo_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            out = os.path.join(repo, ".macro", "surface.json")
            with self.assertRaises(gs.GenError):
                gs.assert_gitignored(repo, out)

    def test_allows_ignored_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            with open(os.path.join(repo, ".gitignore"), "w") as fh:
                fh.write(".macro/surface.json\n")
            _git(repo, "add", ".gitignore")
            _git(repo, "commit", "-m", "ignore")
            out = os.path.join(repo, ".macro", "surface.json")
            gs.assert_gitignored(repo, out)  # must not raise

    def test_allows_path_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            gs.assert_gitignored(repo, os.path.join(tmp, "outside.json"))


class BehindDefaultBranch(unittest.TestCase):
    """behind_origin_main (frozen key name) must track the DECLARED default
    branch, not a hardcoded origin/main -- Atlas's default is master and its
    currency was invisible under the hardcoded ref."""

    @staticmethod
    def _sha(repo):
        return subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()

    def test_counts_against_declared_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            _git(repo, "commit", "--allow-empty", "-m", "second")
            _git(repo, "update-ref", "refs/remotes/origin/master",
                 self._sha(repo))
            _git(repo, "reset", "--hard", "HEAD~1")
            surface, _ = gs.build_surface(
                repo, _base_config(repo, default_branch="master"))
            self.assertEqual(surface["identity"]["behind_origin_main"], 1)

    def test_missing_declared_ref_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(tmp)
            surface, _ = gs.build_surface(
                repo, _base_config(repo, default_branch="master"))
            marker = surface["identity"]["behind_origin_main"]
            self.assertTrue(isinstance(marker, dict) and marker.get("degraded"))
            self.assertIn("origin/master", marker.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
