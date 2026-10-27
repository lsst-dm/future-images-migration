"""``analyze``: classifying what each changed base name needs, from the stored
config pair and the live database, and reporting it.
"""

from __future__ import annotations

import json

from conftest import (
    FUTURE_PRIMARY,
    FUTURE_VARIANT,
    GROUPS,
    LEGACY_VARIANT,
    PRIMARY,
    SECONDARY,
    TICKET,
    Tree,
)

# What the fixture's initial state plus the committed target edits classify to,
# per group: base name -> (legacy storage class, future storage class,
# is_registered, is_db_migration).
#
# Bases the fixture deliberately leaves out of the analysis: `visit_image`,
# already post-arrangement; `template_detector`, a future-only family;
# `deconvolved_deep_coadd`, whose future-side pair guards no row at all and is
# therefore dropped as a config-only change; and the `source` rows, renamed ad
# hoc rather than by this tool's grammar.
EXPECTED = {
    PRIMARY: {
        # Interim added on the future member alone: no rename pending, and its
        # future definition is not in the database yet.
        "template_coadd": ("ExposureF", "CellCoadd", False, False),
        # Interim already live on the future member, future definition
        # registered: the database rename is what is pending.
        "deep_coadd": ("ExposureF", "CellCoadd", True, True),
        # Interim skipped entirely: the database holds only the legacy
        # definition, so apply registers it before renaming.
        "difference_image": ("ExposureF", "DifferenceImage", False, True),
    },
    SECONDARY: {
        "preliminary_visit_image": ("ExposureF", "VisitImage", False, False),
        "deep_coadd": ("ExposureF", "CellCoadd", True, True),
    },
}


def classified(tree: Tree, group: str) -> dict[str, tuple[str, str, bool, bool]]:
    """A group's analyzed dataset types as base name -> (legacy class, future
    class, is_registered, is_db_migration)."""
    return {
        base: (
            change["legacy_storage_class"],
            change["future_storage_class"],
            change["is_registered"],
            change["is_db_migration"],
        )
        for base, change in tree.migration(group)["dataset_types"].items()
    }


# The two directions a pair for deep_coadd can be written in, as they appear in
# the fixture's indented override maps.  Swapping one for the other is how these
# tests forge a member that disagrees with its counterpart about which definition
# the base name serves.
PRE_ARRANGEMENT_PAIR = """          deep_coadd:
            rename: legacy_deep_coadd
          future_deep_coadd:
            rename: deep_coadd
"""
POST_ARRANGEMENT_PAIR = """          legacy_deep_coadd:
            rename: deep_coadd
          deep_coadd:
            rename: future_deep_coadd
"""


def test_analyze_classifies_every_changed_base(edited: Tree) -> None:
    """Every base the target edits touch is classified with its pair's storage
    classes and whether it needs a database rename and a registration, and
    nothing else is classified — including the vestigial pair the edits drop,
    which guards no row in this database."""
    run = edited.run("analyze", TICKET)
    assert run.ok, f"{run.stdout}\n{run.stderr}"
    assert {group: classified(edited, group) for group in GROUPS} == EXPECTED
    assert "deconvolved_deep_coadd" not in run.stdout


def test_analyze_ignores_ad_hoc_renames(edited: Tree) -> None:
    """Rows renamed outside the ``legacy_``/``future_`` grammar take no part in a
    migration: the future members serve rows ``source`` and ``source2`` as
    ``source_v1`` and ``source``, and neither the rows' names nor the visible
    ones reach ``migration.json`` or the summary."""
    run = edited.run("analyze", TICKET)
    assert run.ok, f"{run.stdout}\n{run.stderr}"
    for group in GROUPS:
        assert set(edited.migration(group)["dataset_types"]) == set(EXPECTED[group]), group
    for name in ("source2", "source_v1"):
        assert name not in run.stdout, name


def test_summary_groups_identical_changes_and_nests_variants(edited: Tree) -> None:
    """One summary for the whole ticket: changes split by whether the database
    moves, identical changes across groups print once naming every group that
    needs them, and each group's variants nest under its primary."""
    lines = [line.strip() for line in edited.run("analyze", TICKET).stdout.splitlines()]
    assert "Database migrations (2):" in lines
    assert "Config-only overrides (2):" in lines

    deep = lines.index(next(line for line in lines if line.startswith("deep_coadd")))
    assert lines[deep + 1].startswith(PRIMARY)
    assert f"(+ {LEGACY_VARIANT})" in lines[deep + 1]
    assert lines[deep + 2] == SECONDARY


def test_missing_cutover_entries_warn_without_failing(edited: Tree) -> None:
    """Compression configured on a base name must agree across the two sides of
    a group, and the prefixed entries that extend it to cutover writes are a
    nice-to-have: ``difference_image``, ``template_coadd`` and ``visit_image``
    lack theirs on one side each, which is reported and does not stop the
    analysis. ``deep_coadd`` has both, so its matching pair of entries is
    compared and accepted."""
    run = edited.run("analyze", TICKET)
    assert run.ok, f"{run.stdout}\n{run.stderr}"

    warnings = [line for line in run.stderr.splitlines() if line.startswith("WARNING")]
    for base in ("difference_image", "template_coadd", "visit_image"):
        assert sum(base in line for line in warnings) == 2, base
    assert not [line for line in warnings if "deep_coadd" in line]


def test_analyzing_twice_changes_nothing(analyzed: Tree) -> None:
    """Re-analysis of an unedited ticket reproduces the stored classification,
    so a human can re-run it while working through the warnings."""
    before = {group: analyzed.migration(group) for group in GROUPS}
    run = analyzed.run("analyze", TICKET)
    assert run.ok, f"{run.stdout}\n{run.stderr}"
    assert {group: analyzed.migration(group) for group in GROUPS} == before


def test_analyze_rejects_a_pair_whose_entries_disagree_about_direction(edited: Tree) -> None:
    """A pair's two entries decide together which definition owns the base name,
    so entries pointing opposite ways leave the direction of the database rename
    to whichever entry parsed first — and a rename applied the wrong way moves
    real rows."""
    edited.edit(
        edited.stored(PRIMARY, "new"),
        POST_ARRANGEMENT_PAIR,
        POST_ARRANGEMENT_PAIR + "          future_deep_coadd:\n            rename: deep_coadd\n",
    )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert ".registry.managers.datasets.future_deep_coadd" in run.stderr, run.stderr


def test_analyze_rejects_a_migrated_pair_that_does_not_invert_the_removed_one(edited: Tree) -> None:
    """The pair a database migration adds to the legacy member must run opposite
    to the interim pair it removes from the future member: two members serving
    one base name in the same direction serve different definitions, and writes
    through them land under the wrong one."""
    for alias in (PRIMARY, LEGACY_VARIANT):
        edited.edit(edited.stored(alias, "new"), POST_ARRANGEMENT_PAIR, PRE_ARRANGEMENT_PAIR)
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "deep_coadd" in run.stderr, run.stderr
    assert FUTURE_PRIMARY in run.stderr, run.stderr
    assert "invert" in run.stderr, run.stderr


def test_analyze_rejects_a_migrated_pair_whose_interim_override_stays(edited: Tree) -> None:
    """A database migration must remove the future member's pair along with
    adding the legacy member's: the rename retires the row names that pair
    renames to, so keeping it leaves that member with nothing to serve under the
    base name."""
    for alias in (FUTURE_PRIMARY, FUTURE_VARIANT):
        edited.edit(
            edited.stored(alias, "new"), "        overrides:\n", "        overrides:\n" + PRE_ARRANGEMENT_PAIR
        )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "deep_coadd" in run.stderr, run.stderr
    assert FUTURE_PRIMARY in run.stderr, run.stderr
    assert "not being removed" in run.stderr, run.stderr


def test_analyze_rejects_a_legacy_pair_added_in_the_wrong_direction(edited: Tree) -> None:
    """A pair on the legacy member is the mark of a completed database migration,
    so it has to be the legacy-facing one: written future-facing it claims the
    database already gives the base name to the future definition, which for
    difference_image it does not."""
    for alias in (PRIMARY, LEGACY_VARIANT):
        edited.edit(
            edited.stored(alias, "new"),
            """          legacy_difference_image:
            rename: difference_image
          difference_image:
            rename: future_difference_image
""",
            """          difference_image:
            rename: legacy_difference_image
          future_difference_image:
            rename: difference_image
""",
        )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "difference_image" in run.stderr, run.stderr


def test_analyze_rejects_a_pair_changed_rather_than_added_or_removed(edited: Tree) -> None:
    """A pair present in both stages of a member's config must be identical: a
    base whose override was edited rather than added or removed falls between
    additions and subtractions, so a change the diff cannot see would be reported
    as migrated and never migrated."""
    for alias in (FUTURE_PRIMARY, FUTURE_VARIANT):
        edited.edit(
            edited.stored(alias, "new"), "        overrides:\n", "        overrides:\n" + POST_ARRANGEMENT_PAIR
        )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "deep_coadd" in run.stderr, run.stderr
    assert FUTURE_PRIMARY in run.stderr, run.stderr


def test_analyze_rejects_a_base_with_no_future_storage_class(edited: Tree) -> None:
    """The future definition of difference_image is not registered, so its storage
    class comes from storage-classes.json; with no entry there either, apply would
    have to invent one. Refusing the base beats skipping it, which would leave it
    out of the migration in silence."""
    classes = json.loads(edited.storage_classes().read_text())
    del classes["difference_image"]
    edited.storage_classes().write_text(json.dumps(classes))
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "difference_image" in run.stderr, run.stderr


def test_analyze_requires_a_variant_to_carry_the_primary_ad_hoc_renames(edited: Tree) -> None:
    """A variant that drops its primary's ad-hoc renames serves a different row
    under those names, which makes it another database rather than a variant of
    this one."""
    edited.edit(
        edited.stored(FUTURE_VARIANT, "new"),
        "          source2:\n            rename: source\n          source:\n            rename: source_v1\n",
        "",
    )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert FUTURE_VARIANT in run.stderr, run.stderr
