"""Shared test helper: seed a kanban routing config into a tmp HERMES_HOME.

Repository routing (push/merge targets, reviewer map, signature aliases) used
to be a hardcoded registry inside ``hermes_cli.kanban_db``. It is now operator
configuration under the ``kanban:`` block of config.yaml so a fresh customer
instance inherits nothing (DSGVO multi-instance isolation). Tests that exercise
routing behavior must therefore seed the profiles explicitly; this module is
the single source of truth for the canonical six-profile set that used to live
in code.

The fail-closed EMPTY-config default (an unconfigured instance routes nothing)
is covered by tests/hermes_cli/test_kanban_repo_profiles_isolation.py — do NOT
make seeding automatic/autouse, or that guarantee would stop being tested.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli import config as _hconfig
from hermes_cli import kanban_db as _kb

# The canonical profiles that were migrated out of kanban_db.py. Kept verbatim
# (same push/merge/base/signature values) so routing assertions keep meaning.
CANONICAL_REPOSITORY_PROFILES = {
    "hermes": {
        "coder": "hermes-coder", "reviewer": "hermes-reviewer",
        "base_ref": "fork/main", "merge_target": "main", "push_target": "fork",
        "signatures": [
            "hermes-agent-framework", ".hermes/hermes-agent",
            "hermes-agent", "hermes_agent",
        ],
    },
    "goya": {
        "coder": "goya-coder", "reviewer": "goya-reviewer",
        "base_ref": "origin/develop", "merge_target": "develop",
        "push_target": "origin", "signatures": ["goya-v2", "goya"],
    },
    "voicera": {
        "coder": "voicera-coder", "reviewer": "voicera-reviewer",
        "base_ref": "origin/develop", "merge_target": "develop",
        "push_target": "origin", "signatures": ["voicera-os", "voicera"],
    },
    "ams-erp": {
        "coder": "ams-erp-coder", "reviewer": "ams-erp-reviewer",
        "base_ref": "origin/main", "merge_target": "main",
        "push_target": "origin", "signatures": ["ams-erp", "ams_erp"],
    },
    "voicera-website": {
        "coder": "voicera-website-coder", "reviewer": "voicera-website-reviewer",
        "base_ref": "origin/main", "merge_target": "main",
        "push_target": "origin", "signatures": ["voicera-website"],
    },
    "life-os": {
        "coder": "life-os-coder", "reviewer": "life-os-reviewer",
        "base_ref": "origin/main", "merge_target": "main",
        "push_target": "origin", "signatures": ["life-os", "life_os"],
    },
}

CANONICAL_TENANT_REVIEWERS = {
    "goya": "goya-reviewer",
    "voicera": "voicera-reviewer",
}


def clear_routing_caches() -> None:
    """Drop every memoized read of config.yaml / the derived routing tables."""
    _hconfig._LOAD_CONFIG_CACHE.clear()
    _hconfig._RAW_CONFIG_CACHE.clear()
    _kb._reset_routing_config_cache()


def write_kanban_routing_config(
    home, repository_profiles, tenant_reviewers=None,
) -> None:
    """Write (merge) a ``kanban:`` routing block into ``home/config.yaml``.

    Preserves any pre-existing config.yaml content (only the ``kanban`` key is
    replaced), then clears the caches so the next resolve re-reads it.
    """
    home = Path(home)
    config_path = home / "config.yaml"
    data = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    kanban_block = {"repository_profiles": repository_profiles}
    if tenant_reviewers is not None:
        kanban_block["tenant_reviewers"] = tenant_reviewers
    data["kanban"] = kanban_block
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clear_routing_caches()


def seed_canonical(home) -> None:
    """Seed the canonical six-profile routing config (Till's live set)."""
    write_kanban_routing_config(
        home, CANONICAL_REPOSITORY_PROFILES, CANONICAL_TENANT_REVIEWERS,
    )


def profiles_with_override(name, **field_overrides):
    """Canonical profiles with one profile's fields overridden (shallow copy)."""
    profiles = {k: dict(v) for k, v in CANONICAL_REPOSITORY_PROFILES.items()}
    profiles[name] = {**profiles[name], **field_overrides}
    return profiles
