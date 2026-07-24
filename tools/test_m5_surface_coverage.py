#!/usr/bin/env python3
"""Tests for the M5 surface-coverage configs + morning-pack.sh wrapper.

Stdlib unittest, no third-party deps. These tests validate the SHIPPED central
surface configs (structure + schema, not live repo presence) and the wrapper's
config-selection contract. Run with:

    python3 -m unittest test_m5_surface_coverage -v

The generator filename has a hyphen, so it is loaded via importlib.
"""

import importlib.util
import json
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIGS_DIR = os.path.join(_HERE, "configs")
_WRAPPER = os.path.join(_HERE, "morning-pack.sh")

# The five environments M5 adds coverage for, plus the two M2 pilots -- all
# seven are surface-config/v1 and must resolve centrally.
M5_ENVIRONMENTS = [
    "fully-aware",
    "saga-mission-control",
    "anthony-wiki-vault",
    "imprint",
    "atlas",
]
PILOT_ENVIRONMENTS = ["saga-protocol", "marketing-os-main"]
ALL_SURFACE_ENVIRONMENTS = M5_ENVIRONMENTS + PILOT_ENVIRONMENTS

# Non-repo configs the wrapper must SKIP by schema.
NON_REPO_CONFIGS = {"seed-manifest.json", "ratification-backlog.json"}

# The probe-kind whitelist the generator understands (mirrors resolve_cold_probe).
VALID_PROBE_KINDS = {
    "git-head", "git-head-time", "git-behind", "gh-pr-count",
    "file-exists", "script-exit-code",
}


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_surface", os.path.join(_HERE, "generate-surface.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load_generator()


def _config_path(env):
    return os.path.join(_CONFIGS_DIR, env + ".json")


def _load_raw(env):
    with open(_config_path(env), "r", encoding="utf-8") as fh:
        return json.load(fh)


class M5ConfigValidity(unittest.TestCase):
    """Every M5 config parses, is surface-config/v1, and is well-formed."""

    def test_all_m5_configs_present(self):
        for env in M5_ENVIRONMENTS:
            self.assertTrue(os.path.isfile(_config_path(env)),
                            "missing config for %s" % env)

    def test_parse_and_schema(self):
        for env in M5_ENVIRONMENTS:
            with self.subTest(env=env):
                cfg = _load_raw(env)
                self.assertEqual(cfg.get("schema"), "surface-config/v1")

    def test_required_top_level_fields(self):
        for env in M5_ENVIRONMENTS:
            with self.subTest(env=env):
                cfg = _load_raw(env)
                for field in ("environment", "repo_path", "canonical", "role",
                              "default_branch", "gh_repo"):
                    self.assertIn(field, cfg, "%s missing %s" % (env, field))
                self.assertEqual(cfg["environment"], env)
                self.assertIsInstance(cfg["canonical"], bool)
                self.assertTrue(cfg["role"].strip(), "%s empty role" % env)
                self.assertTrue(cfg["repo_path"].startswith("/"),
                                "%s repo_path must be absolute" % env)
                self.assertRegex(cfg["gh_repo"], r"^[\w.-]+/[\w.-]+$")

    def test_list_sections_are_lists(self):
        for env in M5_ENVIRONMENTS:
            with self.subTest(env=env):
                cfg = _load_raw(env)
                for key in ("cold_load", "verification", "decisions",
                            "next_lanes", "do_not_read", "pitfalls"):
                    self.assertIsInstance(cfg.get(key, []), list,
                                          "%s.%s must be a list" % (env, key))

    def test_cold_load_entries_wellformed(self):
        for env in M5_ENVIRONMENTS:
            cfg = _load_raw(env)
            for i, entry in enumerate(cfg.get("cold_load", [])):
                with self.subTest(env=env, i=i):
                    self.assertIsInstance(entry, dict)
                    if "probe" in entry:
                        kind = entry["probe"].get("kind")
                        self.assertIn(kind, VALID_PROBE_KINDS,
                                      "%s cold_load[%d] bad probe kind %r"
                                      % (env, i, kind))
                        self.assertIn("{value}", entry.get("claim_template", ""),
                                      "%s cold_load[%d] template needs {value}"
                                      % (env, i))
                        if kind == "file-exists":
                            self.assertTrue(entry["probe"].get("path"),
                                            "%s file-exists needs a path" % env)
                    else:
                        self.assertTrue(entry.get("claim"),
                                        "%s static cold_load needs a claim" % env)

    def test_tagged_lists_have_source_and_as_of(self):
        for env in M5_ENVIRONMENTS:
            cfg = _load_raw(env)
            for key in ("next_lanes", "do_not_read", "pitfalls"):
                for i, e in enumerate(cfg.get(key, [])):
                    with self.subTest(env=env, key=key, i=i):
                        # entries may be bare strings per spec, but ours are dicts
                        if isinstance(e, dict):
                            self.assertTrue(e.get("entry"),
                                            "%s.%s[%d] empty entry" % (env, key, i))
                            self.assertTrue(e.get("source"))
                            self.assertRegex(e.get("as_of", ""),
                                             r"^\d{4}-\d{2}-\d{2}")

    def test_generator_accepts_config(self):
        """gs._load_config validates schema without raising for each config."""
        for env in M5_ENVIRONMENTS:
            with self.subTest(env=env):
                cfg = gs._load_config(_config_path(env))
                self.assertEqual(cfg["environment"], env)

    def test_central_resolution_by_environment(self):
        """Each config resolves centrally by --environment (spec SS1.2 order)."""
        for env in ALL_SURFACE_ENVIRONMENTS:
            with self.subTest(env=env):
                args = _NS(environment=env, repo=None, config=None)
                cfg, path, _repo = gs.resolve_config(args)
                self.assertEqual(cfg["environment"], env)
                self.assertTrue(path.endswith("configs/%s.json" % env))

    def test_atlas_default_branch_is_master(self):
        """Atlas's fork default branch is master, not main (documented judgment)."""
        cfg = _load_raw("atlas")
        self.assertEqual(cfg["default_branch"], "master")


class _NS:
    """Minimal argparse-Namespace stand-in for resolve_config."""

    def __init__(self, environment=None, repo=None, config=None):
        self.environment = environment
        self.repo = repo
        self.config = config


class MorningPackWrapper(unittest.TestCase):
    """The wrapper exists, is executable, and honors its selection contract."""

    def test_wrapper_present_and_executable(self):
        self.assertTrue(os.path.isfile(_WRAPPER))
        self.assertTrue(os.access(_WRAPPER, os.X_OK),
                        "morning-pack.sh must be executable")

    def test_wrapper_orchestrates_both_tools(self):
        with open(_WRAPPER, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("generate-surface.py", body)
        self.assertIn("assemble-boot-pack.py", body)
        # regenerate BEFORE assemble: the generate call must precede the assemble.
        self.assertLess(body.index("generate-surface.py"),
                        body.rindex("assemble-boot-pack.py"))
        # writes into the local cache, never another repo.
        self.assertIn("state/surfaces", body)
        # degrade-not-abort: must not use `set -e`.
        self.assertNotRegex(body, r"set -e(?:[^a-zA-Z]|$)")

    def test_wrapper_selection_matches_configs_on_disk(self):
        """Exactly the surface-config/v1 files are selected; non-repo ones skipped."""
        selected, skipped = [], []
        for fn in sorted(os.listdir(_CONFIGS_DIR)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(_CONFIGS_DIR, fn), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("schema") == "surface-config/v1":
                selected.append(fn)
            else:
                skipped.append(fn)
        self.assertEqual(sorted(selected),
                         sorted(e + ".json" for e in ALL_SURFACE_ENVIRONMENTS))
        for nr in NON_REPO_CONFIGS:
            self.assertIn(nr, skipped)


if __name__ == "__main__":
    unittest.main()
