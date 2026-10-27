"""Compression (formatter) configuration rejections.

Compression bound to a dataset type name must be configured consistently across
a group, because daf-butler resolves those keys against the name the client sees
and two members write the same datasets. The matching acceptance and warning-tier
cases are in ``test_analyze.py``.
"""

from __future__ import annotations

from conftest import FUTURE_PRIMARY, FUTURE_VARIANT, PRIMARY, TICKET, Tree

FITS_EXPOSURE = "lsst.obs.base.formatters.fitsExposure.FitsExposureFormatter"
GENERIC = "lsst.images.formatters.GenericFormatter"


def future_entry(alias: str, name: str, formatter: str) -> str:
    """One formatter entry as it is written in a future member's config: the
    primary's datastore is flat, the variant's sits inside its chain."""
    indent = "    " if alias == FUTURE_PRIMARY else "        "
    return (
        f"{indent}{name}:\n"
        f"{indent}  formatter: {formatter}\n"
        f"{indent}  parameters:\n"
        f"{indent}    recipe: lossy16\n"
    )


def test_analyze_rejects_compression_configured_on_one_side_only(edited: Tree) -> None:
    """Compression configured for a base name on one side of a group and not the
    other means the same dataset gets different treatment depending on which
    member wrote it — the base name is what producers use on both sides, so both
    have to carry the entry."""
    for alias in (FUTURE_PRIMARY, FUTURE_VARIANT):
        edited.edit(edited.stored(alias, "new"), future_entry(alias, "deep_coadd", GENERIC), "")

    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "deep_coadd" in run.stderr, run.stderr
    assert PRIMARY in run.stderr and FUTURE_PRIMARY in run.stderr, run.stderr


def test_analyze_rejects_two_entries_naming_the_same_definition(edited: Tree) -> None:
    """``legacy_deep_coadd`` on the future member and ``deep_coadd`` on the legacy
    member name one definition — the legacy one — so their entries must agree:
    two writers of the same dataset disagreeing on compression is what the
    requirement exists to catch."""
    for alias in (FUTURE_PRIMARY, FUTURE_VARIANT):
        edited.edit(
            edited.stored(alias, "new"),
            future_entry(alias, "legacy_deep_coadd", FITS_EXPOSURE),
            future_entry(alias, "legacy_deep_coadd", GENERIC),
        )

    run = edited.run("analyze", TICKET)
    assert run.returncode == 1, f"expected rejection, got rc={run.returncode}: {run.stdout}"
    assert "deep_coadd" in run.stderr, run.stderr
    assert "legacy_deep_coadd" in run.stderr, run.stderr
