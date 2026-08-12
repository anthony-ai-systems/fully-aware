"""Shared path/registry resolution for the macro-seat taste distiller.

Mirrors imprint's data-root resolution (imprint src/imprint/paths.py +
config.py) WITHOUT importing the imprint package: the Stop hook has a
sub-200ms budget and the worker runs from bare python3 under launchd,
outside imprint's venv. Resolution order matches imprint's:
IMPRINT_DATA_ROOT env > config data_root > XDG default; operator slug
from config, defaulting to "anthony".

MACROSEAT_SPOOL_DIR overrides the spool location wholesale — tests use it
so nothing here ever touches the live operator data root (imprint
NEXT_SESSION guardrail: sessions never write ~/.local/share/imprint).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

SPOOL_FILE = "distill-queue.ndjson"
LEDGER_FILE = "distill-ledger.json"
REGISTRY_FILE = "entity-registry.json"


def imprint_config() -> dict:
    override = os.environ.get("IMPRINT_CONFIG")
    if override:
        path = Path(override).expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        path = base / "imprint" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def operator_root(config: dict | None = None) -> Path:
    config = imprint_config() if config is None else config
    override = os.environ.get("IMPRINT_DATA_ROOT")
    if override:
        root = Path(override).expanduser()
    elif config.get("data_root"):
        root = Path(str(config["data_root"])).expanduser()
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        root = base / "imprint"
    slug = str(config.get("operator_slug") or "anthony")
    return root / slug


def spool_dir() -> Path:
    override = os.environ.get("MACROSEAT_SPOOL_DIR")
    if override:
        return Path(override).expanduser()
    return operator_root() / "spool"


def spool_path() -> Path:
    return spool_dir() / SPOOL_FILE


def ledger_path() -> Path:
    return spool_dir() / LEDGER_FILE


def registry_path() -> Path:
    override = os.environ.get("MACROSEAT_REGISTRY")
    if override:
        return Path(override).expanduser()
    return operator_root() / REGISTRY_FILE


def load_registry() -> list[dict]:
    """Entity registry (§2.5): [{slug, kind, aliases[], project_dir_globs[]}]."""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entities = data.get("entities") if isinstance(data, dict) else None
    return [e for e in entities or [] if isinstance(e, dict) and e.get("slug")]


def assign_scope(project_dir: str, text: str, registry: list[dict]) -> dict:
    """Scope a specimen: directory mapping + content cues (§2.5).

    Returns {"scope": "global"|"entity:<slug>", "flagged": bool,
    "candidates": [slugs]}. One unambiguous entity wins; disagreement or a
    multi-entity match is flagged for re-scoping at keep time.
    """
    dir_hits: set[str] = set()
    cue_hits: set[str] = set()
    for entity in registry:
        slug = str(entity["slug"])
        for glob in entity.get("project_dir_globs") or []:
            if fnmatch.fnmatch(project_dir or "", str(glob)):
                dir_hits.add(slug)
        for alias in entity.get("aliases") or []:
            alias = str(alias)
            # Short aliases (e.g. "PG", "Rich") stay case-sensitive so prose
            # words don't false-positive; longer names match case-insensitively.
            flags = 0 if len(alias) <= 4 else re.IGNORECASE
            if re.search(rf"\b{re.escape(alias)}\b", text or "", flags):
                cue_hits.add(slug)
    hits = dir_hits | cue_hits
    if not hits:
        return {"scope": "global", "flagged": False, "candidates": []}
    if len(hits) == 1:
        return {"scope": f"entity:{next(iter(hits))}", "flagged": False,
                "candidates": sorted(hits)}
    # Directory and cues agreeing on one entity beats a stray extra cue.
    agreed = dir_hits & cue_hits
    if len(agreed) == 1:
        return {"scope": f"entity:{next(iter(agreed))}", "flagged": True,
                "candidates": sorted(hits)}
    return {"scope": "global", "flagged": True, "candidates": sorted(hits)}
