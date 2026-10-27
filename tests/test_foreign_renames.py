"""Rejections of ad-hoc renames of rows this tool does not migrate.

The fixture's future members serve rows ``source`` and ``source2`` as
``source_v1`` and ``source``; each test here forges a chain that crosses a line
the accepted chain does not.
"""

from __future__ import annotations

from conftest import FUTURE_PRIMARY, TICKET, Tree


def test_analyze_rejects_an_ad_hoc_rename_touching_a_migrated_name(edited: Tree) -> None:
    """A rename of an unmigrated row that lands on one of a migrated pair's names
    is a mistyped pair rather than a one-off, and the pair's own renames would
    collide with it: the row this tool is supposed to rename would be left
    alone."""
    edited.edit(
        edited.stored(FUTURE_PRIMARY, "new"),
        "        overrides:\n",
        "        overrides:\n          source3:\n            rename: template_coadd\n",
    )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "source3" in run.stderr, run.stderr
    assert "template_coadd" in run.stderr, run.stderr


def test_analyze_rejects_a_prefixed_ad_hoc_rename(edited: Tree) -> None:
    """``legacy_`` and ``future_`` are this tool's prefixes: an ad-hoc rename that
    borrows one reads as a pair to the next reader, and gets classified — and
    eventually renamed in the database — as one."""
    edited.edit(
        edited.stored(FUTURE_PRIMARY, "new"),
        "            rename: source_v1\n",
        "            rename: legacy_source2\n",
    )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "legacy_source2" in run.stderr, run.stderr


def test_analyze_rejects_two_rows_renamed_onto_one_visible_name(edited: Tree) -> None:
    """Two rows served under one name leave the client no way to say which
    definition it wants, so the ambiguity is refused wherever it comes from —
    ad-hoc renames included."""
    edited.edit(
        edited.stored(FUTURE_PRIMARY, "new"),
        "          source:\n            rename: source_v1\n",
        "          source3:\n            rename: source\n          source:\n            rename: source_v1\n",
    )
    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "source3" in run.stderr, run.stderr
