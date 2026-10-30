"""Tests for the `init` subcommand (seeding, skip behavior, --reset) and its
repo-pair identity guards."""

from __future__ import annotations

import json

from conftest import (
    FUTURE_REPO,
    LEGACY_VARIANT,
    REPO,
    TICKET,
    run_migration,
    set_stored_config,
    stored_config,
)


def test_init_seeding(tree) -> None:
    """After init, all four stored files exist and byte-match the external
    configs; migration.json exists with empty dataset_types."""
    tmp = tree.tmp_path
    stored = {
        (repo, kind): stored_config(tmp, repo, kind).read_bytes()
        for repo in (REPO, FUTURE_REPO)
        for kind in ("old", "new")
    }
    assert stored[(REPO, "old")] == tree.external("legacy").read_bytes()
    assert stored[(REPO, "new")] == stored[(REPO, "old")]
    assert stored[(FUTURE_REPO, "old")] == tree.external("future").read_bytes()
    assert stored[(FUTURE_REPO, "new")] == stored[(FUTURE_REPO, "old")]
    assert run_migration(tmp, "init", TICKET, REPO).migration["dataset_types"] == {}


def test_init_no_reset_keeps_edits(tree) -> None:
    """Re-running init without --reset must not clobber edited stored
    files."""
    set_stored_config(tree.tmp_path, REPO, "new", "legacy-new.yaml")
    before = stored_config(tree.tmp_path, REPO, "new").read_bytes()
    assert run_migration(tree.tmp_path, "init", TICKET, REPO).ok
    assert stored_config(tree.tmp_path, REPO, "new").read_bytes() == before


def test_init_reset_after_external_drift(tree) -> None:
    """If the external config drifted since init, --reset re-seeds both the
    old and (wiping intentional edits) the new stored files."""
    set_stored_config(tree.tmp_path, REPO, "new", "legacy-new.yaml")
    tree.drift_external("legacy")
    assert run_migration(tree.tmp_path, "init", "--reset", TICKET, REPO).ok
    external = tree.external("legacy").read_bytes()
    assert stored_config(tree.tmp_path, REPO, "old").read_bytes() == external
    assert stored_config(tree.tmp_path, REPO, "new").read_bytes() == external


def test_init_reset_preserves_new_edits(tree) -> None:
    """With an unchanged external config, intentional edits to *-new survive
    --reset."""
    set_stored_config(tree.tmp_path, REPO, "new", "legacy-new.yaml")
    before = stored_config(tree.tmp_path, REPO, "new").read_bytes()
    assert run_migration(tree.tmp_path, "init", "--reset", TICKET, REPO).ok
    assert stored_config(tree.tmp_path, REPO, "new").read_bytes() == before


def test_init_rejects_self_migration(tree) -> None:
    """A repo pair with legacy_repo == future_repo (a self-migration) is
    rejected; the CLI derives future_repo from legacy_repo, so this
    invariant is pinned through a persisted migration.json validated by
    analyze."""
    evil_dir = tree.tmp_path / "migrations" / TICKET / "self-migration"
    evil_dir.mkdir(parents=True)
    (evil_dir / "migration.json").write_text(
        json.dumps(
            {
                "ticket": TICKET,
                "legacy_repo": REPO,
                "future_repo": REPO,
                "dataset_types": {},
            }
        )
    )
    run = run_migration(tree.tmp_path, "analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}"
    assert "Invalid self-migration" in run.stderr, run.stderr


def test_init_rejects_shared_alias_pair(repo_pair) -> None:
    """Two repos whose (db, namespace) resolve identically may not be
    initialized as independent migrations."""
    # Make the variant's legacy side a legal init pair by aliasing its
    # "+_future" counterpart to the historical future-variant config.
    repo_pair.add_alias(f"{LEGACY_VARIANT}_future", repo_pair.variant_external("future"))
    run = run_migration(repo_pair.tmp_path, "init", TICKET, REPO, LEGACY_VARIANT)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "is an alias, not an independent repository" in run.stderr, run.stderr


def test_init_names_both_databases_on_pair_mismatch(repo_pair) -> None:
    """A primary pair whose legacy and future externals name different
    databases is refused with both registry db URLs in the message."""
    external = repo_pair.external("future")
    external.write_text(external.read_text().replace("dp2_prep.sqlite", "dp2_prep_other.sqlite"))
    run = run_migration(repo_pair.tmp_path, "init", TICKET, REPO)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "dp2_prep.sqlite" in run.stderr, run.stderr
    assert "dp2_prep_other.sqlite" in run.stderr, run.stderr
