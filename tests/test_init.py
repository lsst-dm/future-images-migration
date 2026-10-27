"""``init``: seeding a ticket's stored copies from the live repositories, and
what survives re-running it.
"""

from __future__ import annotations

from conftest import (
    FUTURE_PRIMARY,
    FUTURE_VARIANT,
    GROUPS,
    LEGACY_VARIANT,
    PRIMARY,
    PRIMARY_MEMBERS,
    TICKET,
    Tree,
    committed_external,
    committed_target,
)


def test_init_seeds_both_stages_of_each_primary_member(prepared: Tree) -> None:
    """Each member of a group's primary pair gets its external config copied
    into the migration directory as both the rollback copy and the target to
    edit, byte for byte."""
    for alias in PRIMARY_MEMBERS:
        assert prepared.stored(alias, "old").read_bytes() == committed_external(alias), alias
        assert prepared.stored(alias, "new").read_bytes() == committed_external(alias), alias


def test_init_creates_one_migration_per_repository(prepared: Tree) -> None:
    """Each repository named on the command line becomes its own migration,
    keyed by the legacy repo with the future member derived from its name, and
    carries no analysis or variants until the other subcommands run."""
    for group in GROUPS:
        migration = prepared.migration(group)
        assert migration["ticket"] == TICKET
        assert migration["legacy_repo"] == group
        assert migration["future_repo"] == f"{group}_future"
        assert migration["dataset_types"] == {}
        assert migration["variants"] == []


def test_init_over_an_edited_ticket_changes_nothing(edited: Tree) -> None:
    """Re-running ``init`` without ``--reset`` skips members whose stored copies
    already exist, so hand edits and the variant list survive an accidental
    second run."""
    run = edited.run("init", TICKET, PRIMARY)
    assert run.ok, run.stderr
    for alias in GROUPS[PRIMARY]:
        assert edited.stored(alias, "new").read_bytes() == committed_target(alias), alias
        assert edited.stored(alias, "old").read_bytes() == committed_external(alias), alias
    assert edited.migration(PRIMARY)["variants"] == [
        {"legacy_repo": LEGACY_VARIANT, "future_repo": FUTURE_VARIANT}
    ]


def test_reset_reseeds_a_drifted_member_and_clears_analysis(analyzed: Tree) -> None:
    """``--reset`` re-reads a member whose external has drifted since the last
    init, overwriting that member's hand-edited target along with its rollback
    copy, and starts the analysis over; members that still match their stored
    rollback copy keep their edits, and the variant list survives because the
    migration is round-tripped rather than rebuilt."""
    drifted = analyzed.external(PRIMARY).read_text().replace(
        "root: <butlerRoot>", "root: <butlerRoot>/drifted", 1
    )
    assert "/drifted" in drifted
    analyzed.external(PRIMARY).write_text(drifted)

    run = analyzed.run("init", "--reset", TICKET, PRIMARY)
    assert run.ok, run.stderr

    assert analyzed.stored(PRIMARY, "old").read_text() == drifted
    assert analyzed.stored(PRIMARY, "new").read_text() == drifted
    assert analyzed.stored(FUTURE_PRIMARY, "new").read_bytes() == committed_target(FUTURE_PRIMARY)
    assert analyzed.stored(LEGACY_VARIANT, "new").read_bytes() == committed_target(LEGACY_VARIANT)
    assert analyzed.migration(PRIMARY)["dataset_types"] == {}
    assert analyzed.migration(PRIMARY)["variants"] == [
        {"legacy_repo": LEGACY_VARIANT, "future_repo": FUTURE_VARIANT}
    ]
