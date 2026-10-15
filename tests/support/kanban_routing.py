"""Shared fixtures for Kanban routing configured at an instance root."""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli import kanban_db as kb


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

CANONICAL_TENANT_WORKDIRS = {
    "goya": "/srv/goya",
    "voicera": "/srv/voicera",
}


def seed_canonical(home: Path) -> None:
    """Write the former built-in routing set to a temporary instance root."""
    config = {
        "kanban": {
            "repository_profiles": CANONICAL_REPOSITORY_PROFILES,
            "tenant_reviewers": CANONICAL_TENANT_REVIEWERS,
            "tenant_workdirs": CANONICAL_TENANT_WORKDIRS,
        }
    }
    (Path(home) / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    kb._reset_routing_config_cache()
