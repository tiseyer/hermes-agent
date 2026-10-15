"""Regression tests for instance-root-only Kanban routing configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb


def _write_routing_config(
    home: Path,
    *,
    profile_name: str,
    reviewer: str,
    workdir: str,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = {
        "kanban": {
            "repository_profiles": {
                profile_name: {
                    "coder": f"{profile_name}-coder",
                    "reviewer": reviewer,
                    "base_ref": "origin/main",
                    "merge_target": "main",
                    "push_target": "origin",
                    "signatures": [profile_name],
                }
            },
            "tenant_reviewers": {profile_name: reviewer},
            "tenant_workdirs": {profile_name: workdir},
        }
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


def _routing_snapshot() -> tuple[set[str], dict[str, str], dict[str, str]]:
    routing = kb._load_routing_config()
    return (
        set(routing.profiles),
        routing.tenant_reviewers,
        routing.tenant_workdirs,
    )


def test_role_profile_reads_all_routing_maps_only_from_each_instance_root(
    tmp_path, monkeypatch,
):
    """The worker profile config cannot override or augment root routing."""
    root_one = tmp_path / "instance-one"
    profile_one = root_one / "profiles" / "worker"
    root_two = tmp_path / "instance-two"
    profile_two = root_two / "profiles" / "worker"

    _write_routing_config(
        root_one,
        profile_name="root-one",
        reviewer="root-one-reviewer",
        workdir="/srv/root-one",
    )
    _write_routing_config(
        profile_one,
        profile_name="profile-only",
        reviewer="profile-reviewer",
        workdir="/srv/profile-only",
    )
    _write_routing_config(
        root_two,
        profile_name="root-two",
        reviewer="root-two-reviewer",
        workdir="/srv/root-two",
    )
    _write_routing_config(
        profile_two,
        profile_name="profile-two-only",
        reviewer="profile-two-reviewer",
        workdir="/srv/profile-two-only",
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated-home")
    monkeypatch.setenv("HERMES_HOME", str(profile_one))
    kb._reset_routing_config_cache()

    assert _routing_snapshot() == (
        {"root-one"},
        {"root-one": "root-one-reviewer"},
        {"root-one": "/srv/root-one"},
    )
    resolved = kb.resolve_repository_profile("Repository: root-one", None)
    assert resolved is not None
    assert resolved.reviewer == "root-one-reviewer"
    with pytest.raises(kb.RepositoryProfileError):
        kb.resolve_repository_profile("Repository: profile-only", None)

    token = set_hermes_home_override(profile_two)
    try:
        assert _routing_snapshot() == (
            {"root-two"},
            {"root-two": "root-two-reviewer"},
            {"root-two": "/srv/root-two"},
        )
    finally:
        reset_hermes_home_override(token)

    # The first instance must still retain its own independently cached tables.
    assert _routing_snapshot() == (
        {"root-one"},
        {"root-one": "root-one-reviewer"},
        {"root-one": "/srv/root-one"},
    )


def test_missing_or_malformed_root_config_ignores_profile_routing(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated-home")

    missing_root = tmp_path / "missing-root"
    missing_profile = missing_root / "profiles" / "worker"
    _write_routing_config(
        missing_profile,
        profile_name="profile-only",
        reviewer="profile-reviewer",
        workdir="/srv/profile-only",
    )
    monkeypatch.setenv("HERMES_HOME", str(missing_profile))
    kb._reset_routing_config_cache()
    assert _routing_snapshot() == (set(), {}, {})

    malformed_root = tmp_path / "malformed-root"
    malformed_profile = malformed_root / "profiles" / "worker"
    _write_routing_config(
        malformed_profile,
        profile_name="profile-only",
        reviewer="profile-reviewer",
        workdir="/srv/profile-only",
    )
    (malformed_root / "config.yaml").write_text(
        "kanban: [unterminated", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(malformed_profile))
    kb._reset_routing_config_cache()
    assert _routing_snapshot() == (set(), {}, {})
