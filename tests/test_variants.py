"""Regression tests for the `add-variant` feature.

Contract: ``add-variant TICKET LEGACY_BASE LEGACY_VARIANT FUTURE_VARIANT``
associates a legacy/future repo pair (the historical dp2_prep+sasquatch_dev
chained-datastore variant) as a *variant* of the primary pair of an existing
migration: the pair is persisted under ``migration.json``'s VARIANTS_KEY and
four config files (``{variant}.old/new.yaml``) are seeded in the primary
migration dir; analyze mirrors the primary's overrides across the group
(check_variant_overrides).

Production implements group identity as the raw ``(db, namespace)`` registry
pair from ``repo_alias_key``: variants are
required to live in the same repo directory as their primary (a relocated
config would break their file-datastore side), so raw comparison is the
agreed contract.
"""

from __future__ import annotations

from conftest import (
    FUTURE_VARIANT,
    LEGACY_VARIANT,
    REPO,
    TICKET,
    VARIANTS_KEY,
    assert_rejected,
    canonical_new_state,
    data_text,
    mirror_variant_state,
    run_migration,
    set_stored_config,
    stored_config,
)
from test_analyze import CANONICAL_TYPES

# Actual production error tokens (aligned to production wording; keep one
# distinctive substring each).
ERR_ALREADY = "already included as standalone repos"
ERR_BASELINE = "do not match"
ERR_MIRROR = "Mismatch between"

PAIR_JSON = [{"legacy_repo": LEGACY_VARIANT, "future_repo": FUTURE_VARIANT}]


def add_variant(handle, legacy_variant: str = LEGACY_VARIANT, future_variant: str = FUTURE_VARIANT):
    return run_migration(handle.tmp_path, "add-variant", TICKET, REPO, legacy_variant, future_variant)


def test_add_variant_rejects_unknown_base(repo_pair) -> None:
    """A migration must exist for (TICKET, legacy_base) before a variant can
    join it (currently a FileNotFoundError naming the missing path)."""
    run = run_migration(
        repo_pair.tmp_path, "add-variant", "no_such_ticket", REPO, LEGACY_VARIANT, FUTURE_VARIANT
    )
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}"
    assert "no_such_ticket" in run.stderr, run.stderr
    run = run_migration(
        repo_pair.tmp_path, "add-variant", TICKET, "no_such_repo", LEGACY_VARIANT, FUTURE_VARIANT
    )
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}"
    assert "no_such_repo" in run.stderr, run.stderr


def test_add_variant_seeds_and_persists(tree) -> None:
    """Happy path: pair persisted under VARIANTS_KEY; the four seeded files
    byte-match the variant externals (old == new initially)."""
    run = add_variant(tree)
    assert run.ok, run.stderr
    assert run.migration[VARIANTS_KEY] == PAIR_JSON
    for variant, side in ((LEGACY_VARIANT, "legacy"), (FUTURE_VARIANT, "future")):
        for kind in ("old", "new"):
            assert stored_config(tree.tmp_path, variant, kind).read_bytes() == (
                tree.variant_external(side).read_bytes()
            )


def test_add_variant_rejects_collisions(tree) -> None:
    """Variant names may not collide with primary names or extend an
    existing pair."""
    assert_rejected(add_variant(tree, legacy_variant=REPO), ERR_ALREADY)
    assert add_variant(tree).ok, "first add-variant should succeed"
    assert_rejected(add_variant(tree), ERR_ALREADY)


def test_add_variant_rejects_baseline_mismatch(tree) -> None:
    """The variant cannot be adopted mid-advance: its external config's
    overrides must match the primary stored baseline on each side."""
    tree.variant_external("legacy").write_text(data_text("legacy-variant-new.yaml"))
    assert_rejected(add_variant(tree), ERR_BASELINE)


def test_add_variant_names_both_databases_on_mismatch(tree) -> None:
    """A variant naming a different registry database is refused, and the
    refusal names both sides' db file names."""
    external = tree.variant_external("legacy")
    external.write_text(external.read_text().replace("dp2_prep.sqlite", "dp2_prep_other.sqlite"))
    run = add_variant(tree)
    assert_rejected(run, "dp2_prep.sqlite")
    assert "dp2_prep_other.sqlite" in run.stderr, run.stderr


def test_analyze_variant_dataset_types_once(tree) -> None:
    """With a mirrored variant added, analyze reports each dataset type
    exactly once (dataset_types come from the primary pair only)."""
    assert add_variant(tree).ok
    canonical_new_state(tree)
    mirror_variant_state(tree)
    run = run_migration(tree.tmp_path, "analyze", TICKET)
    assert run.ok, run.stderr
    assert run.migration["dataset_types"] == CANONICAL_TYPES
    assert run.migration[VARIANTS_KEY] == PAIR_JSON


def test_analyze_variant_mirror_violation(tree) -> None:
    """A variant whose stored new config does not mirror the primary stored
    new config is rejected by analyze."""
    assert add_variant(tree).ok
    # canonical_new_state edits only the primary's stored new files; the
    # variant new files stay at the seeded baseline overrides, so the (new)
    # mirror check must fire.
    canonical_new_state(tree)
    assert_rejected(run_migration(tree.tmp_path, "analyze", TICKET), ERR_MIRROR)


def test_init_reset_reseeds_primary_not_variants(tree) -> None:
    """Baseline drift: init --reset reseeds the PRIMARY pair's files (wiping
    edits) but leaves variant seeded/edited files untouched — variant files
    are managed by add-variant, not init."""
    assert add_variant(tree).ok
    edit = data_text("legacy-variant-new.yaml")
    stored_config(tree.tmp_path, LEGACY_VARIANT, "new").write_text(edit)
    tree.drift_external("legacy")
    assert run_migration(tree.tmp_path, "init", "--reset", TICKET, REPO).ok
    external = tree.external("legacy").read_bytes()
    assert stored_config(tree.tmp_path, REPO, "old").read_bytes() == external
    assert stored_config(tree.tmp_path, REPO, "new").read_bytes() == external
    assert stored_config(tree.tmp_path, LEGACY_VARIANT, "new").read_text() == edit
    assert stored_config(tree.tmp_path, LEGACY_VARIANT, "old").read_bytes() == (
        tree.variant_external("legacy").read_bytes()
    )


def test_init_reset_preserves_variant_edits(tree) -> None:
    """With no baseline drift, init --reset leaves variant stored files
    alone (group-wide skip)."""
    assert add_variant(tree).ok
    set_stored_config(tree.tmp_path, LEGACY_VARIANT, "new", "legacy-variant-new.yaml")
    before_old = stored_config(tree.tmp_path, LEGACY_VARIANT, "old").read_bytes()
    before_new = stored_config(tree.tmp_path, LEGACY_VARIANT, "new").read_bytes()
    assert run_migration(tree.tmp_path, "init", "--reset", TICKET, REPO).ok
    assert stored_config(tree.tmp_path, LEGACY_VARIANT, "old").read_bytes() == before_old
    assert stored_config(tree.tmp_path, LEGACY_VARIANT, "new").read_bytes() == before_new


def test_init_reset_preserves_variants_list(tree) -> None:
    """init --reset round-trips migration.json, so the variants list
    survives; analyze and apply skip variant externals once it is gone."""
    assert add_variant(tree).ok
    tree.drift_external("legacy")
    run = run_migration(tree.tmp_path, "init", "--reset", TICKET, REPO)
    assert run.ok
    assert run.migration[VARIANTS_KEY] == PAIR_JSON
