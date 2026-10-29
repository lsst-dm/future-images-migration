"""CLI tests for the ``apply``/``apply --undo`` subcommand: dataset-type
renames in each migration's database plus copy-via-writing of the stored
configs onto the external ones, behind a global zero-write check phase.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest
import sqlalchemy
import yaml
from lsst.daf.butler import Butler, ButlerConfig

from conftest import (
    APPLIED_NAMES,
    DATA_DIR,
    FUTURE_REPO,
    FUTURE_VARIANT,
    LEGACY_VARIANT,
    MigrationRun,
    PRE_APPLY_NAMES,
    REPO,
    SECOND,
    SECOND_FUTURE,
    TICKET,
    assert_rejected,
    copy_template,
    data_text,
    dataset_type_names,
    dataset_type_rows,
    migration_dir,
    register_dataset_types,
    run_migration,
    set_stored_config,
    stored_config,
)

FUTURE_CLASSES = {
    "visit_image": "VisitImage",
    "difference_image": "DifferenceImage",
    "deep_coadd": "CellCoadd",
}


@pytest.fixture
def ready_group(migration_templates, tmp_path, monkeypatch):
    """The first migration with its variant, stored *-new edits in the
    canonical state, and analyze already run (so migration.json holds the
    analyzed dataset types for the undo path); deep-copied from the
    session's prepared template.
    """
    return copy_template(migration_templates["ready"], tmp_path, monkeypatch)


@pytest.fixture
def two_group(migration_templates, tmp_path, monkeypatch):
    """Both ticket migrations in the canonical analyzed state (first pair
    unvaried, second pair's stored *-new edited); deep-copied from the
    session's prepared template.
    """
    return copy_template(migration_templates["two_ready"], tmp_path, monkeypatch)


def apply_cmd(pair, *args: str) -> MigrationRun:
    return run_migration(pair.tmp_path, "apply", TICKET, *args)


def group_externals() -> dict[str, str]:
    return {
        repo: ButlerConfig(repo).configFile.read()
        for repo in (REPO, FUTURE_REPO, LEGACY_VARIANT, FUTURE_VARIANT)
    }


def test_apply_forward(ready_group) -> None:
    """Forward apply swaps the database names, rewrites every group
    member's external config from its stored *-new, and leaves
    migration.json byte-identical."""
    tmp = ready_group.tmp_path
    json_before = (migration_dir(tmp) / "migration.json").read_bytes()
    assert apply_cmd(ready_group).ok
    assert (migration_dir(tmp) / "migration.json").read_bytes() == json_before
    assert dataset_type_names(REPO) == APPLIED_NAMES
    # Historical check() idiom: renames visible in the post-apply configs.
    with Butler.from_config(REPO) as butler:
        for base_name, future_class in FUTURE_CLASSES.items():
            assert butler.get_dataset_type(base_name).storageClass_name == "ExposureF"
            assert butler.get_dataset_type(f"future_{base_name}").storageClass_name == future_class
    with Butler.from_config(FUTURE_REPO) as butler:
        for base_name, future_class in FUTURE_CLASSES.items():
            assert butler.get_dataset_type(base_name).storageClass_name == future_class
            assert butler.get_dataset_type(f"legacy_{base_name}").storageClass_name == "ExposureF"
    for repo in (REPO, FUTURE_REPO, LEGACY_VARIANT, FUTURE_VARIANT):
        assert ButlerConfig(repo).configFile.read() == stored_config(tmp, repo, "new").read_bytes()


def test_apply_drift_rejection(ready_group) -> None:
    """A hand-edited external fails the zero-write check phase: nothing
    anywhere is mutated, including the other repos' databases and files."""
    tmp = ready_group.tmp_path
    ready_group.drift_external("future")
    names_before = dataset_type_names(REPO)
    externals_before = group_externals()
    json_before = (migration_dir(tmp) / "migration.json").read_bytes()
    run = apply_cmd(ready_group)
    assert run.returncode == 1, f"expected rejection, got rc=0:\n{run.stdout}"
    assert "does not match" in run.stderr, run.stderr
    assert dataset_type_names(REPO) == names_before == PRE_APPLY_NAMES
    for repo, text in externals_before.items():
        assert ButlerConfig(repo).configFile.read() == text
    assert (migration_dir(tmp) / "migration.json").read_bytes() == json_before


def test_apply_rejects_two_migrations_on_one_db(ready_group) -> None:
    """Two independent migrations from separate init invocations over one
    database are rejected in the check phase: they would race on the same
    dataset_type table and each other's externals."""
    dup_legacy = ready_group.repo_dir / "dupdb.yaml"
    dup_future = ready_group.repo_dir / "dupdb_future.yaml"
    shutil.copyfile(DATA_DIR / "legacy-old.yaml", dup_legacy)
    shutil.copyfile(DATA_DIR / "future-old.yaml", dup_future)
    ready_group.add_alias("dupdb", dup_legacy)
    ready_group.add_alias("dupdb_future", dup_future)
    assert run_migration(ready_group.tmp_path, "init", TICKET, "dupdb").ok
    names_before = dataset_type_names(REPO)
    run = apply_cmd(ready_group)
    assert_rejected(run, "same repository")
    assert "Rewrote" not in run.stdout
    assert dataset_type_names(REPO) == names_before


def test_apply_registers_unregistered_including_config_only(repo_pair) -> None:
    """apply registers future types that only storage-classes.json knows
    about — database migrations rename them afterward, config-only entries
    just gain the row their new config points at — and rewrites the config
    files."""
    tmp = repo_pair.tmp_path
    repo_pair.register_types({"weird_thing": ("ExposureF", ("visit", "detector"))})
    register_dataset_types(
        REPO,
        {
            "future_difference_image": ("DifferenceImage", ("visit", "detector")),
            "future_deep_coadd": ("CellCoadd", ("patch", "band")),
        },
    )  # future_visit_image deliberately left out.
    assert run_migration(tmp, "init", TICKET, REPO).ok
    classes_path = tmp / "migrations" / "storage-classes.json"
    classes = json.loads(classes_path.read_text())
    classes["weird_thing"] = "ExposureI"
    classes_path.write_text(json.dumps(classes))
    set_stored_config(tmp, REPO, "new", "legacy-new.yaml")
    future_new = yaml.safe_load(data_text("future-new.yaml"))
    future_new["registry"]["managers"]["datasets"] = {
        "cls": "lsst.daf.butler.registry.datasets.byDimensions"
               ".ByDimensionsDatasetRecordStorageManagerUUID",
        "config": {
            "overrides": {
                "weird_thing": {"rename": "legacy_weird_thing"},
                "future_weird_thing": {"rename": "weird_thing"},
            }
        },
    }
    stored_config(tmp, FUTURE_REPO, "new").write_text(yaml.safe_dump(future_new))
    assert apply_cmd(repo_pair).ok
    # With no pre-existing future_visit_image row to rename, the swap could
    # not have produced the base name unless apply registered it.
    assert dataset_type_names(REPO) == APPLIED_NAMES | {"weird_thing", "future_weird_thing"}
    with Butler.from_config(REPO) as butler:
        assert butler.get_dataset_type("future_visit_image").storageClass_name == "VisitImage"
        # The config-only entry's future type is registered under its raw
        # name (the legacy member has no overrides to hide it).
        assert butler.get_dataset_type("future_weird_thing").storageClass_name == "ExposureI"
    # A config-only entry never takes part in renames.
    assert "legacy_weird_thing" not in dataset_type_names(REPO)
    assert ButlerConfig(FUTURE_REPO).configFile.read() == (
        stored_config(tmp, FUTURE_REPO, "new").read_bytes()
    )


def test_apply_rollback(ready_group) -> None:
    """An unwritable copy target late in the group leaves the transaction
    rolled back (database names untouched) and the already-copied externals
    restored from their stored *-old files."""
    tmp = ready_group.tmp_path
    # Copies walk the group in iteration order; the variant's future config
    # is last, so the failure lands after the renames and earlier copies.
    target = ready_group.variant_external("future")
    os.chmod(target, 0o444)
    try:
        run = apply_cmd(ready_group)
    finally:
        os.chmod(target, 0o644)
    assert run.returncode == 1, f"expected failure, got rc=0:\n{run.stdout}"
    assert "Permission denied" in run.stderr, run.stderr
    assert dataset_type_names(REPO) == PRE_APPLY_NAMES
    for repo in (REPO, FUTURE_REPO, LEGACY_VARIANT):
        assert ButlerConfig(repo).configFile.read() == stored_config(tmp, repo, "old").read_bytes()
    assert ButlerConfig(FUTURE_VARIANT).configFile.read() == (
        stored_config(tmp, FUTURE_VARIANT, "old").read_bytes()
    )


def test_apply_undo_roundtrip(ready_group) -> None:
    """apply followed by --undo restores the pre-apply database names and
    external bytes exactly."""
    names_before = dataset_type_names(REPO)
    externals_before = group_externals()
    assert apply_cmd(ready_group).ok
    assert apply_cmd(ready_group, "--undo").ok
    assert dataset_type_names(REPO) == names_before == PRE_APPLY_NAMES
    for repo, text in externals_before.items():
        assert ButlerConfig(repo).configFile.read() == text


def test_apply_undo_rejects_drift(ready_group) -> None:
    """--undo gates on externals matching the stored *-new; a drifted
    external aborts it with nothing mutated."""
    assert apply_cmd(ready_group).ok
    drifted = ready_group.variant_external("legacy")
    drifted.write_text(
        drifted.read_text().replace("root: <butlerRoot>", "root: <butlerRoot>/drifted", 1)
    )
    externals_before = {
        repo: text
        for repo, text in group_externals().items()
        if repo != LEGACY_VARIANT
    }
    run = apply_cmd(ready_group, "--undo")
    assert run.returncode == 1, f"expected rejection, got rc=0:\n{run.stdout}"
    assert "does not match" in run.stderr, run.stderr
    assert dataset_type_names(REPO) == APPLIED_NAMES
    for repo, text in externals_before.items():
        assert ButlerConfig(repo).configFile.read() == text
    assert "butlerRoot>/drifted" in drifted.read_text()


def test_apply_repo_scope(two_group) -> None:
    """--repo limits every phase (checks, registration, mutation) to the
    selected migration."""
    tmp = two_group.tmp_path
    names_before = dataset_type_names(REPO)
    externals_before = group_externals()
    assert apply_cmd(two_group, "--repo", SECOND).ok
    assert dataset_type_names(REPO) == names_before == PRE_APPLY_NAMES
    for repo, text in externals_before.items():
        assert ButlerConfig(repo).configFile.read() == text
    assert dataset_type_names(SECOND) == APPLIED_NAMES
    assert ButlerConfig(SECOND).configFile.read() == (
        stored_config(tmp, SECOND, "new", prime=SECOND).read_bytes()
    )


def test_apply_undo_requires_analyzed_state(ready_unanalyzed_tree) -> None:
    """apply analyzes in-memory only, so a migration whose migration.json
    never received dataset types from analyze is refused by --undo instead
    of being half-reverted (configs restored while renames stay)."""
    assert apply_cmd(ready_unanalyzed_tree).ok
    migration_json = json.loads(
        (migration_dir(ready_unanalyzed_tree.tmp_path) / "migration.json").read_text()
    )
    assert migration_json["dataset_types"] == {}
    names_applied = dataset_type_names(REPO)
    externals_applied = group_externals()
    run = apply_cmd(ready_unanalyzed_tree, "--undo")
    assert run.returncode == 1, f"expected refusal, got rc=0:\n{run.stdout}"
    assert "has not been analyzed" in run.stderr, run.stderr
    assert dataset_type_names(REPO) == names_applied == APPLIED_NAMES
    for repo, text in externals_applied.items():
        assert ButlerConfig(repo).configFile.read() == text


def test_apply_undo_aborts_on_missing_rename_row(ready_group) -> None:
    """A rename matching no row aborts the whole transaction: deleting a
    legacy_ row behind the tool's back makes --undo raise, with the
    database and every external left in the applied state."""
    assert apply_cmd(ready_group).ok
    with Butler.from_config(REPO, writeable=True) as butler:
        database = butler.registry._registry._db
        with database._engine.begin() as connection:
            connection.execute(sqlalchemy.text("DELETE FROM dataset_type WHERE name='legacy_deep_coadd'"))
    externals_applied = group_externals()
    run = apply_cmd(ready_group, "--undo")
    assert run.returncode == 1, f"expected abort, got rc=0:\n{run.stdout}"
    assert "legacy_deep_coadd" in run.stderr, run.stderr
    assert "matched 0 rows" in run.stderr, run.stderr
    # Everything the transaction did rolled back; only the committed
    # DELETE above is missing from the applied state.
    assert dataset_type_names(REPO) == APPLIED_NAMES - {"legacy_deep_coadd"}
    for repo, text in externals_applied.items():
        assert ButlerConfig(repo).configFile.read() == text


def test_apply_checks_reject_across_migrations(two_group) -> None:
    """The zero-write check phase spans all selected migrations: drift in
    one aborts the run with the other migration's database, externals, and
    migration.json untouched."""
    tmp = two_group.tmp_path
    drifted = tmp / SECOND / "butler.yaml"
    drifted.write_text(drifted.read_text().replace("root: <butlerRoot>", "root: <butlerRoot>/drifted", 1))
    json_before = (migration_dir(tmp) / "migration.json").read_bytes()
    names_before = {repo: dataset_type_names(repo) for repo in (REPO, SECOND)}
    externals_before = group_externals()
    second_before = ButlerConfig(SECOND).configFile.read()
    run = apply_cmd(two_group)
    assert run.returncode == 1, f"expected rejection, got rc=0:\n{run.stdout}"
    assert "does not match" in run.stderr, run.stderr
    for repo, names in names_before.items():
        assert dataset_type_names(repo) == names
    for repo, text in externals_before.items():
        assert ButlerConfig(repo).configFile.read() == text
    assert ButlerConfig(SECOND).configFile.read() == second_before
    assert (migration_dir(tmp) / "migration.json").read_bytes() == json_before


def test_apply_undo_keeps_registrations(two_group) -> None:
    """Undo reverses renames and files but not registrations: the
    future_visit_image type apply registered from storage-classes.json
    comes back through the reverse rename rather than disappearing."""
    tmp = two_group.tmp_path
    rows_before = dataset_type_rows(SECOND)
    assert "future_visit_image" not in rows_before
    assert apply_cmd(two_group, "--repo", SECOND).ok
    assert "future_visit_image" not in dataset_type_names(SECOND)
    assert apply_cmd(two_group, "--repo", SECOND, "--undo").ok
    rows_after = dataset_type_rows(SECOND)
    assert set(rows_after) == set(rows_before) | {"future_visit_image"}
    assert rows_after["future_visit_image"] == "VisitImage"
    for repo in (SECOND, SECOND_FUTURE):
        assert ButlerConfig(repo).configFile.read() == (
            stored_config(tmp, repo, "old", prime=SECOND).read_bytes()
        )
