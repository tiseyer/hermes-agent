"""DSGVO multi-instance isolation: repository profiles come from config.yaml.

The hardcoded ``_REPOSITORY_PROFILES`` / ``_TENANT_REVIEWER_MAP`` /
``_REPO_SIGNATURE_MAP`` registries were lifted into the ``kanban:`` block of
config.yaml (see hermes_cli/kanban_db.py::_load_routing_config). The
non-negotiable guarantee these tests lock down:

  * An instance with NO ``kanban.repository_profiles`` config inherits an EMPTY
    registry. It can therefore never route or push a card to a git remote it
    was never told about — in particular never to Till's fork / voicera /
    goya / ams-erp / life-os remotes. (Fail-closed default.)

  * A customer instance (Römer) that declares ONLY its own repository resolves
    ONLY its own push target — a card that names a foreign repo is a
    fail-closed ``RepositoryProfileError``, never a silent foreign push.

These run on a real throwaway SQLite kanban DB (kb.init_db() on a per-test
tmp HERMES_HOME) — no mocks. The kanban persistence layer is SQLite, not
PostgreSQL; "fresh throwaway DB" therefore means a fresh tmp SQLite file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli import config as hconfig
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb


# A Römer-only instance: knows exactly one repository, pointing exclusively at
# Römer-owned git remotes. Nothing here references fork / origin-of-Till /
# voicera / goya / ams-erp / life-os.
ROEMER_PROFILES = {
    "roemer-crm": {
        "coder": "roemer-crm-coder",
        "reviewer": "roemer-crm-reviewer",
        "base_ref": "roemer/main",
        "merge_target": "main",
        "push_target": "roemer",
        "signatures": ["roemer-crm", "roemer_crm", "roemer"],
    },
}

# Signatures that MUST NOT resolve on a Römer (or unconfigured) instance.
FOREIGN_SIGNATURES = [
    "hermes-agent",
    "voicera-os",
    "voicera",
    "goya-v2",
    "ams-erp",
    "life-os",
    "voicera-website",
]

# Remote / ref tokens that would prove a leak of Till's integration targets.
FORBIDDEN_TOKENS = ("fork", "tiseyer", "voicera", "goya", "ams-erp", "life-os")


def _clear_caches() -> None:
    hconfig._LOAD_CONFIG_CACHE.clear()
    hconfig._RAW_CONFIG_CACHE.clear()
    kb._reset_routing_config_cache()


def _write_config(home: Path, kanban_block: dict | None) -> None:
    cfg: dict = {}
    if kanban_block is not None:
        cfg["kanban"] = kanban_block
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _clear_caches()


@pytest.fixture
def unconfigured_home(tmp_path, monkeypatch):
    """A brand-new instance: HERMES_HOME exists, but no kanban routing config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _clear_caches()
    kb.init_db()
    return home


@pytest.fixture
def roemer_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_config(home, {"repository_profiles": ROEMER_PROFILES})
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. Unconfigured instance → EMPTY registry (the fail-closed default).
#    This is also the Gegenprobe canary: reintroduce any hardcoded default and
#    these assertions go red.
# ---------------------------------------------------------------------------

def test_unconfigured_instance_has_empty_registry(unconfigured_home):
    assert kb._repository_profiles() == {}
    assert kb._tenant_reviewer_map() == {}
    assert kb._repo_signature_map() == ()
    assert kb._repo_profile_prefixes() == ()


def test_unconfigured_instance_resolves_no_tenant_profile(unconfigured_home):
    # No declaration, tenant default: an unconfigured tenant resolves to None,
    # so the caller keeps its own (local) fallback — never a baked-in remote.
    assert kb.resolve_repository_profile(None, "voicera") is None
    assert kb.resolve_repository_profile(None, "hermes") is None
    assert kb.resolve_repository_profile("Fix the footer", "voicera") is None


@pytest.mark.parametrize("signature", FOREIGN_SIGNATURES)
def test_unconfigured_instance_blocks_every_declared_foreign_repo(
    unconfigured_home, signature,
):
    """A card naming a (foreign) repo fails closed — never a silent push."""
    with pytest.raises(kb.RepositoryProfileError):
        kb.resolve_repository_profile(f"Repository: {signature}", None)


def test_unconfigured_instance_task_resolves_to_no_push_target(unconfigured_home):
    with kb.connect() as conn:
        # Undeclared card, voicera tenant — the historical hardcoded path would
        # have produced push_target "origin"/"fork". It must now resolve to None.
        task_id = kb.create_task(conn, title="irgendwas", tenant="voicera")
        assert kb.resolve_task_repository_profile(conn, task_id) is None


# ---------------------------------------------------------------------------
# 2. Römer-only instance → ONLY Römer remotes, never Till/voicera.
# ---------------------------------------------------------------------------

def test_roemer_card_resolves_only_roemer_push_target(roemer_home):
    for sig in ("roemer-crm", "roemer_crm", "roemer"):
        profile = kb.resolve_repository_profile(f"Repository: {sig}", None)
        assert profile is not None, sig
        assert profile.name == "roemer-crm"
        assert profile.push_target == "roemer"
        assert profile.base_ref == "roemer/main"
        assert profile.merge_target == "main"


@pytest.mark.parametrize("signature", FOREIGN_SIGNATURES)
def test_roemer_instance_blocks_foreign_repo_declarations(roemer_home, signature):
    """Römer naming hermes/voicera/... is fail-closed, never a foreign push."""
    with pytest.raises(kb.RepositoryProfileError):
        kb.resolve_repository_profile(f"Repository: {signature}", None)


def test_roemer_registry_contains_no_foreign_tokens(roemer_home):
    """No configured Römer profile may reference a Till/voicera remote or ref."""
    for name, profile in kb._repository_profiles().items():
        blob = " ".join(
            [profile.base_ref, profile.merge_target, profile.push_target,
             profile.coder, profile.reviewer]
        ).lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in blob, f"{name} leaks forbidden token {token!r}"


def test_roemer_task_routes_only_to_roemer(roemer_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="CRM fix", body="Repository: roemer-crm", tenant=None,
        )
        profile = kb.resolve_task_repository_profile(conn, task_id)
    assert profile is not None
    assert profile.push_target == "roemer"
    assert "fork" not in profile.push_target
    assert "origin" not in profile.push_target


# ---------------------------------------------------------------------------
# 3. Longest-first precedence survives the config round-trip.
# ---------------------------------------------------------------------------

def test_longest_first_precedence_from_config(roemer_home):
    """A specific signature wins over a shorter substring regardless of the
    order they are declared in config (ordering is derived, not authored)."""
    # "roemer-crm" contains "roemer"; the specific one must win.
    profile = kb.resolve_repository_profile("Repository: roemer-crm", None)
    assert profile is not None and profile.name == "roemer-crm"
    # The derived signature map is ordered strictly longest-first.
    sig_map = kb._repo_signature_map()
    lengths = [len(sig) for sig, _ in sig_map]
    assert lengths == sorted(lengths, reverse=True)


# ===========================================================================
# Root-only routing contract (card t_22197b3b): the three routing maps come
# ONLY from the resolved instance ROOT config.yaml — never a profiles/<name>
# copy — keyed on the resolved root path so two custom roots never leak each
# other's tables, and fail closed (empty) on missing/malformed/non-string.
# ===========================================================================


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


def test_well_formed_yaml_with_non_string_routing_values_fails_closed(
    tmp_path, monkeypatch,
):
    """YAML collections/nulls must not become usable routing strings."""
    root = tmp_path / "typed-malformed-root"
    profile_home = root / "profiles" / "worker"
    _write_routing_config(
        profile_home,
        profile_name="profile-only",
        reviewer="profile-reviewer",
        workdir="/srv/profile-only",
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "kanban": {
                    "repository_profiles": {
                        "typed-malformed": {
                            "coder": ["typed-coder"],
                            "reviewer": {"profile": "typed-reviewer"},
                            "base_ref": None,
                            "merge_target": 7,
                            "push_target": False,
                            "signatures": [None, ["typed"], {"repo": "typed"}],
                        },
                        123: {
                            "coder": "numeric-key-coder",
                            "reviewer": "numeric-key-reviewer",
                            "base_ref": "origin/main",
                            "merge_target": "main",
                            "push_target": "origin",
                        },
                    },
                    "tenant_reviewers": {
                        "null-reviewer": None,
                        "collection-reviewer": ["reviewer"],
                        123: "numeric-key-reviewer",
                    },
                    "tenant_workdirs": {
                        "collection-workdir": ["/srv/workdir"],
                        "mapping-workdir": {"path": "/srv/workdir"},
                        456: "/srv/numeric-key",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated-home")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    kb._reset_routing_config_cache()

    routing = kb._load_routing_config()
    assert set(routing.profiles) == {"typed-malformed"}
    malformed = routing.profiles["typed-malformed"]
    assert (
        malformed.coder,
        malformed.reviewer,
        malformed.base_ref,
        malformed.merge_target,
        malformed.push_target,
    ) == ("", "", "", "", "")
    assert routing.signature_map == ()
    assert routing.tenant_reviewers == {}
    assert routing.tenant_workdirs == {}
    with pytest.raises(kb.RepositoryProfileError, match="base_ref"):
        kb.resolve_repository_profile(None, "typed-malformed")
