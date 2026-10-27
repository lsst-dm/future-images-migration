"""``check``: read-only verification of a live pair, independent of any
ticket.
"""

from __future__ import annotations

from conftest import (
    FUTURE_PRIMARY,
    PRIMARY,
    SECONDARY,
    SECONDARY_FUTURE,
    Tree,
)

PAIRS = ((PRIMARY, FUTURE_PRIMARY), (SECONDARY, SECONDARY_FUTURE))


def test_check_accepts_an_applied_pair(applied: Tree) -> None:
    """A pair whose database has been arranged and whose externals carry the
    post-arrangement overrides resolves both definitions under both members, in
    both groups."""
    for legacy, future in PAIRS:
        run = applied.run("check", legacy, future)
        assert run.ok, f"{legacy}/{future}: {run.stdout}\n{run.stderr}"


def test_check_accepts_a_live_interim_pair(prepared: Tree) -> None:
    """The interim arrangement is a configuration check accepts, not only the
    final one: ``v3_skills_unit``'s future member presents the future definition
    under the base name through its pair while its legacy member still serves
    the database names."""
    assert prepared.run("check", SECONDARY, SECONDARY_FUTURE).ok


def test_check_reports_a_pair_whose_renames_resolve_nowhere(prepared: Tree) -> None:
    """``v3_skills_unit`` passes pre-migration and the first group does not, and
    the difference is the pair that guards no row at all:
    ``deconvolved_deep_coadd`` was copied in from another repo family, so check
    reports the names it cannot resolve instead of calling the pair fine. A
    fixture that stopped being an initial state, or a check that stopped
    probing, fails here."""
    run = prepared.run("check", PRIMARY, FUTURE_PRIMARY)
    assert run.returncode == 1, f"expected failure, got rc={run.returncode}: {run.stdout}"
    assert "deconvolved_deep_coadd" in run.stderr, run.stderr
