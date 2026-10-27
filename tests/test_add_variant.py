"""``add-variant``: adopting a member pair that shares a group's database and
differs from the primary pair in other configuration.
"""

from __future__ import annotations

import yaml

from conftest import (
    FUTURE_VARIANT,
    LEGACY_VARIANT,
    PRIMARY,
    TICKET,
    Tree,
    committed_external,
)


def test_add_variant_adopts_the_pair_and_seeds_its_copies(prepared: Tree) -> None:
    """A variant pair is recorded in its primary's migration and its externals
    are seeded as stored copies at entry, so the pair can be edited and
    analyzed like the primary's members."""
    run = prepared.run("add-variant", TICKET, PRIMARY, LEGACY_VARIANT, FUTURE_VARIANT)
    assert run.ok, f"{run.stdout}\n{run.stderr}"

    assert prepared.migration(PRIMARY)["variants"] == [
        {"legacy_repo": LEGACY_VARIANT, "future_repo": FUTURE_VARIANT}
    ]
    for alias in (LEGACY_VARIANT, FUTURE_VARIANT):
        assert prepared.stored(alias, "old").read_bytes() == committed_external(alias), alias
        assert prepared.stored(alias, "new").read_bytes() == committed_external(alias), alias


def test_add_variant_seeds_the_variant_own_config(prepared: Tree) -> None:
    """The seeded copies are the variant's own configuration, not a copy of the
    primary's: the variant chains a ``SasquatchDatastore`` on top of the shared
    ``FileDatastore``, and the shared section — including the formatter entries
    that decide compression — is identical to the primary's, which is the
    group-configuration invariant ``add-variant`` enforces at entry."""
    prepared.run("add-variant", TICKET, PRIMARY, LEGACY_VARIANT, FUTURE_VARIANT)

    variant = yaml.safe_load(prepared.stored(LEGACY_VARIANT, "old").read_text())
    primary = yaml.safe_load(prepared.stored(PRIMARY, "old").read_text())
    assert variant["datastore"]["cls"].endswith("ChainedDatastore")
    assert primary["datastore"]["cls"].endswith("FileDatastore")
    shared = variant["datastore"]["datastores"][0]
    assert shared["cls"] == primary["datastore"]["cls"]
    assert shared["root"] == primary["datastore"]["root"]
    assert shared["formatters"] == primary["datastore"]["formatters"]


def test_add_variant_rejects_a_variant_that_diverges_from_the_shared_datastore(
    prepared: Tree,
) -> None:
    """A variant chains configuration onto the group's shared datastore; it does
    not change it. A variant pointing its file datastore elsewhere would have
    records written where the primary cannot read them, while sharing the
    primary's registry — which is what makes it a variant of this group at all."""
    prepared.edit(prepared.external(LEGACY_VARIANT), "root: <butlerRoot>", "root: <butlerRoot>/diverged")

    run = prepared.run("add-variant", TICKET, PRIMARY, LEGACY_VARIANT, FUTURE_VARIANT)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert LEGACY_VARIANT in run.stderr, run.stderr
    assert PRIMARY in run.stderr, run.stderr
