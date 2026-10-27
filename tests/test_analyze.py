"""End-to-end analyze tests pinned to the historical DM-55129 dp2_prep
migration (data files in tests/data)."""

from __future__ import annotations

import yaml

from conftest import (
    BASE_TYPES,
    DATA_DIR,
    FUTURE_REPO,
    REPO,
    TICKET,
    MigrationRun,
    assert_rejected,
    canonical_new_state,
    run_migration,
    set_stored_config,
    stored_config,
)


def entry(legacy: str, future: str, registered: bool, db_migration: bool) -> dict:
    return {
        "legacy_storage_class": legacy,
        "future_storage_class": future,
        "is_registered": registered,
        "is_db_migration": db_migration,
    }


def analyze(tmp_path) -> MigrationRun:
    return run_migration(tmp_path, "analyze", TICKET)


CANONICAL_TYPES = {
    "visit_image": entry("ExposureF", "VisitImage", True, True),
    "difference_image": entry("ExposureF", "DifferenceImage", True, True),
    "deep_coadd": entry("ExposureF", "CellCoadd", True, True),
}


def test_canonical_dp2_prep(tree) -> None:
    """The historical dp2_prep migration: legacy repo gains the overrides,
    future repo loses them, DB migration moves the future definitions in."""
    canonical_new_state(tree)
    run = analyze(tree.tmp_path)
    assert run.ok, run.stderr
    assert run.migration["dataset_types"] == CANONICAL_TYPES


def test_config_only(repo_pair) -> None:
    """Future repo gains overrides only (no DB migration, future_ types not
    registered): future storage classes come from storage-classes.json."""
    repo_pair.install_external("future", "future-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    set_stored_config(repo_pair.tmp_path, FUTURE_REPO, "new", "future-old.yaml")
    run = analyze(repo_pair.tmp_path)
    assert run.ok, run.stderr
    assert run.migration["dataset_types"] == {
        "visit_image": entry("ExposureF", "VisitImage", False, False),
        "difference_image": entry("ExposureF", "DifferenceImage", False, False),
        "deep_coadd": entry("ExposureF", "CellCoadd", False, False),
    }


def test_skip_intermediate(repo_pair) -> None:
    """DB migration performed without the interim config-override-only stage
    (future repo was already plain): the pairing check is skipped."""
    repo_pair.install_external("future", "future-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    repo_pair.register_future_types()
    set_stored_config(repo_pair.tmp_path, REPO, "new", "legacy-new.yaml")
    run = analyze(repo_pair.tmp_path)
    assert run.ok, run.stderr
    assert run.migration["dataset_types"] == CANONICAL_TYPES


def test_analyze_twice(tree) -> None:
    """Re-running analyze is idempotent.

    (Compares parsed JSON: each analyze is a separate process, and set
    iteration order means key order in the file is not stable.)
    """
    canonical_new_state(tree)
    first = analyze(tree.tmp_path)
    assert first.ok, first.stderr
    second = analyze(tree.tmp_path)
    assert second.ok, second.stderr
    assert second.migration == first.migration


def test_legacy_subtraction(repo_pair) -> None:
    """The legacy repo may not drop overrides it already has."""
    repo_pair.install_external("legacy", "legacy-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    set_stored_config(repo_pair.tmp_path, REPO, "new", "legacy-old.yaml")
    assert_rejected(analyze(repo_pair.tmp_path), "Unexpected legacy subtractions")


def test_missing_removal(tree) -> None:
    """A base DB-migrated in the legacy repo must have its override removed
    from the future repo."""
    set_stored_config(tree.tmp_path, REPO, "new", "legacy-new.yaml")
    set_stored_config(tree.tmp_path, FUTURE_REPO, "new", "future-old.yaml")
    assert_rejected(analyze(tree.tmp_path), "not being removed")


def test_does_not_invert(repo_pair) -> None:
    """The override removed from the future repo must invert the one added
    to the legacy repo."""
    repo_pair.install_external("future", "legacy-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    set_stored_config(repo_pair.tmp_path, REPO, "new", "legacy-new.yaml")
    set_stored_config(repo_pair.tmp_path, FUTURE_REPO, "new", "future-new.yaml")
    assert_rejected(analyze(repo_pair.tmp_path), "does not invert")


def test_wrong_direction(repo_pair) -> None:
    """Legacy-repo additions must act in the future-adopting direction."""
    repo_pair.install_external("future", "future-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    set_stored_config(repo_pair.tmp_path, REPO, "new", "future-old.yaml")
    assert_rejected(analyze(repo_pair.tmp_path), "wrong direction")


def test_future_only_db_is_future(repo_pair) -> None:
    """A future-repo-only addition cannot imply the shared DB already uses
    the future definition."""
    repo_pair.install_external("future", "future-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    set_stored_config(repo_pair.tmp_path, FUTURE_REPO, "new", "legacy-new.yaml")
    assert_rejected(analyze(repo_pair.tmp_path), "already uses the future definition")


def test_orphan_subtraction(tree) -> None:
    """Overrides removed from the future repo without a matching legacy DB
    migration are rejected."""
    set_stored_config(tree.tmp_path, FUTURE_REPO, "new", "future-new.yaml")
    assert_rejected(analyze(tree.tmp_path), "are not migrated")


def test_unknown_storage_class(repo_pair) -> None:
    """A base name neither registered as future_* nor present in
    storage-classes.json produces an error naming that file."""
    repo_pair.install_external("future", "future-new.yaml")
    assert run_migration(repo_pair.tmp_path, "init", TICKET, REPO).ok
    repo_pair.register_types(BASE_TYPES | {"bogus_image": ("ExposureF", ("visit", "detector"))})
    config = yaml.safe_load((DATA_DIR / "future-new.yaml").read_text())
    config["registry"]["managers"]["datasets"] = {
        "cls": "lsst.daf.butler.registry.datasets.byDimensions"
               ".ByDimensionsDatasetRecordStorageManagerUUID",
        "config": {
            "overrides": {
                "bogus_image": {"rename": "legacy_bogus_image"},
                "future_bogus_image": {"rename": "bogus_image"},
            }
        },
    }
    stored_config(repo_pair.tmp_path, FUTURE_REPO, "new").write_text(yaml.safe_dump(config))
    run = analyze(repo_pair.tmp_path)
    assert run.returncode == 1, f"expected failure: {run.stdout}"
    assert "storage-classes.json" in run.stderr, run.stderr
