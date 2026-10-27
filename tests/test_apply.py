"""``apply``: the database renames and the config writes that follow an
analysis, ``--repo`` scoping, and ``--undo``.
"""

from __future__ import annotations

from conftest import (
    ALL_MEMBERS,
    GROUPS,
    INITIAL_ROW_CLASSES,
    LEGACY_VARIANT,
    PRIMARY,
    SECONDARY,
    SECONDARY_FUTURE,
    TICKET,
    Tree,
    committed_external,
    committed_target,
)

# The dataset_type rows each group's database must hold after apply, name ->
# storage class.
APPLIED_ROWS = {
    PRIMARY: {
        # Post-arrangement already, from an earlier migration.
        "legacy_visit_image": "ExposureF",
        "visit_image": "VisitImage",
        "legacy_deep_coadd": "ExposureF",
        "deep_coadd": "CellCoadd",
        "legacy_difference_image": "ExposureF",
        "difference_image": "DifferenceImage",
        # Config-only: its legacy definition keeps the base name and the
        # future definition is registered under its prefixed name, because
        # apply's registration pass precedes every config rewrite.
        "template_coadd": "ExposureF",
        "future_template_coadd": "CellCoadd",
        # Nothing to do: a future-only family, and rows renamed ad hoc.
        "template_detector": "DifferenceImage",
        "source": "SourceCatalog",
        "source2": "SourceCatalog",
    },
    SECONDARY: {
        "legacy_deep_coadd": "ExposureF",
        "deep_coadd": "CellCoadd",
        "preliminary_visit_image": "ExposureF",
        "future_preliminary_visit_image": "VisitImage",
    },
}

# Rows apply registers but does not rename, so they outlive ``--undo``: the
# future definitions it found missing.
REGISTRATIONS_APPLY_MADE = {
    PRIMARY: {"future_difference_image": "DifferenceImage", "future_template_coadd": "CellCoadd"},
    SECONDARY: {"future_preliminary_visit_image": "VisitImage"},
}


def test_apply_arranges_every_selected_database(applied: Tree) -> None:
    """Each group's ``dataset_type`` rows land exactly in the post-arrangement
    state: migrated bases park their legacy definition under ``legacy_`` and
    serve the future definition under the base name, config-only bases keep
    their legacy row and gain a registered ``future_`` one, and bases with
    nothing to do — the already-migrated ``visit_image``, the future-only
    ``template_detector``, the ad-hoc ``source`` rows — keep their names."""
    assert {group: applied.rows(group) for group in GROUPS} == APPLIED_ROWS


def test_apply_writes_the_stored_targets_onto_the_externals(applied: Tree) -> None:
    """After a successful apply every member's external config is its stored
    target, byte for byte, including the two variant members."""
    for alias in ALL_MEMBERS:
        assert applied.external(alias).read_bytes() == committed_target(alias), alias


def test_apply_never_rewrites_the_analysis(analyzed: Tree, applied: Tree) -> None:
    """``apply`` executes an analysis without touching it: what it read is what
    ``apply --undo`` will later read to know which renames to reverse."""
    for group in GROUPS:
        assert applied.migration(group) == analyzed.migration(group), group


def test_apply_repo_scope_leaves_the_other_group_alone(analyzed: Tree) -> None:
    """``--repo`` selects one migration to apply; the other keeps its database
    rows and its external configs, which is how independent groups are
    sequenced after a failure in the first."""
    run = analyzed.run("apply", TICKET, "--repo", PRIMARY)
    assert run.ok, f"{run.stdout}\n{run.stderr}"

    assert analyzed.rows(PRIMARY) == APPLIED_ROWS[PRIMARY]
    assert analyzed.rows(SECONDARY) == INITIAL_ROW_CLASSES[SECONDARY]
    for alias in GROUPS[PRIMARY]:
        assert analyzed.external(alias).read_bytes() == committed_target(alias), alias
    for alias in (SECONDARY, SECONDARY_FUTURE):
        assert analyzed.external(alias).read_bytes() == committed_external(alias), alias


def test_apply_undo_restores_names_and_configs(applied: Tree) -> None:
    """``--undo`` immediately after apply puts the rows back under their
    original names and rewrites the externals from their stored rollback
    copies, and keeps the registrations apply made: an extra registered row
    changes nothing until something uses it, and re-registering is
    idempotent."""
    run = applied.run("apply", TICKET, "--undo")
    assert run.ok, f"{run.stdout}\n{run.stderr}"

    for group in GROUPS:
        assert applied.rows(group) == INITIAL_ROW_CLASSES[group] | REGISTRATIONS_APPLY_MADE[group]
    for alias in ALL_MEMBERS:
        assert applied.external(alias).read_bytes() == committed_external(alias), alias


def test_apply_undo_leaves_the_interim_group_consistent(applied: Tree) -> None:
    """Undo is not only a byte-level restore: what it leaves behind is the
    pre-migration arrangement, which for a group whose future member was
    already interim is still a pair ``check`` reads as consistent."""
    run = applied.run("apply", TICKET, "--undo")
    assert run.ok, f"{run.stdout}\n{run.stderr}"
    assert applied.run("check", SECONDARY, SECONDARY_FUTURE).ok


def test_apply_refuses_without_touching_a_clean_migration(analyzed: Tree) -> None:
    """Every check runs against every selected migration before any of them is
    registered, renamed or copied, so a migration in good order is left alone
    when its sibling's config has moved. Apply across databases is not atomic;
    this ordering is what keeps a rejection from being half-applied."""
    analyzed.edit(analyzed.external(SECONDARY), "root: <butlerRoot>", "root: <butlerRoot>/drifted")

    run = analyzed.run("apply", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert SECONDARY in run.stderr, run.stderr

    assert analyzed.rows(PRIMARY) == INITIAL_ROW_CLASSES[PRIMARY]
    for alias in GROUPS[PRIMARY]:
        assert analyzed.external(alias).read_bytes() == committed_external(alias), alias


def test_apply_restores_the_configs_it_had_already_written(analyzed: Tree) -> None:
    """The config copies happen outside the rename transaction, so a write that
    fails partway through a group must leave every external already rewritten
    restored from its rollback copy and every rename rolled back. The
    registrations it made before copying stay: an extra registered row changes
    nothing until something uses it, and re-registering is idempotent."""
    victim = analyzed.external(LEGACY_VARIANT)
    victim.chmod(0o444)
    try:
        run = analyzed.run("apply", TICKET, "--repo", PRIMARY)
    finally:
        victim.chmod(0o644)

    assert run.returncode == 1, f"expected failure, got rc={run.returncode}: {run.stdout}"
    for alias in GROUPS[PRIMARY]:
        assert analyzed.external(alias).read_bytes() == committed_external(alias), alias
    assert analyzed.rows(PRIMARY) == INITIAL_ROW_CLASSES[PRIMARY] | REGISTRATIONS_APPLY_MADE[PRIMARY]


def test_apply_refuses_two_migrations_naming_one_database(analyzed: Tree) -> None:
    """``init`` refuses two migrations on one registry, but ``add-variant
    --force`` deliberately accepts a database named under another spelling, so
    apply re-checks that no two selected migrations name the same registry: the
    second would rename rows the first has already renamed."""
    for path in (analyzed.external(SECONDARY), analyzed.stored(SECONDARY, "old")):
        analyzed.edit(path, "v3_skills_unit.sqlite", "dp2_prep.sqlite")

    run = analyzed.run("apply", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert PRIMARY in run.stderr and SECONDARY in run.stderr, run.stderr

    assert analyzed.rows(PRIMARY) == INITIAL_ROW_CLASSES[PRIMARY]
    assert analyzed.rows(SECONDARY) == INITIAL_ROW_CLASSES[SECONDARY]
