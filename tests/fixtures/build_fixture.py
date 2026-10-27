#!/usr/bin/env python
"""Rebuild the committed fixture databases.

The fixture groups under this directory are real butler repositories: their
configs are hand-authored and committed, and only the ``*.sqlite`` registries
are generated, by opening each group and registering the dataset types of its
state model.  Run this after changing a group's rows, or when a daf-butler
change makes a committed registry stale::

    python tests/fixtures/build_fixture.py

Rows are registered through a client built from the group's ``butler.yaml``
with its dataset-manager overrides dropped: rows parked under a rename (the
post-arrangement `legacy_visit_image`, say) are invisible to a client that
renames them away.  Nothing here runs during the test session: tests copy the
committed trees and never rebuild them, so the databases under version control
are the single source of truth for a group's initial state.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import yaml

FIXTURES = Path(__file__).parent

DATASETS_MANAGER = "lsst.daf.butler.registry.datasets.byDimensions.ByDimensionsDatasetRecordStorageManagerUUID"

# Dataset types of each group's database, named as the registry names them:
# name -> (storage class, dimensions).  The configs' overrides decide which
# spelling a client sees.
ROWS = {
    "dp2_prep": {
        # Post-arrangement: migrated by an earlier ticket, so no change left.
        "legacy_visit_image": ("ExposureF", ("visit", "detector")),
        "visit_image": ("VisitImage", ("visit", "detector")),
        # Pre-arrangement, future definition registered: the interim is live on
        # the future member and the database rename is what's pending.
        "deep_coadd": ("ExposureF", ("patch", "band")),
        "future_deep_coadd": ("CellCoadd", ("patch", "band")),
        # Legacy definition only.
        "difference_image": ("ExposureF", ("visit", "detector")),
        "template_coadd": ("ExposureF", ("patch", "band")),
        # Future definition only: this family never had a legacy one.
        "template_detector": ("DifferenceImage", ("visit", "detector")),
        # Two definitions of `source` left behind by an earlier generation of
        # migrations, renamed ad hoc (row source2 shows as `source`, row
        # source as `source_v1`) on the future members.
        "source": ("SourceCatalog", ("visit", "detector")),
        "source2": ("SourceCatalog", ("visit", "detector")),
        # `deconvolved_deep_coadd` is deliberately absent: the future member
        # declares a pair for it anyway, which makes that pair vestigial.
    },
    "v3_skills_unit": {
        "deep_coadd": ("ExposureF", ("patch", "band")),
        "future_deep_coadd": ("CellCoadd", ("patch", "band")),
        "preliminary_visit_image": ("ExposureF", ("visit", "detector")),
    },
}


def build(group: str, rows: dict[str, tuple[str, tuple[str, ...]]]) -> None:
    # Deferred: the test suite imports ROWS from this module, and only building
    # a registry needs daf-butler.
    from lsst.daf.butler import Butler, DatasetType

    repo_dir = FIXTURES / group
    parent = Path(tempfile.mkdtemp(prefix=f"build-{group}-"))
    config = yaml.safe_load((repo_dir / "butler.yaml").read_text())
    config["registry"]["managers"]["datasets"] = DATASETS_MANAGER
    config_path = parent / "build.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    Butler.makeRepo(str(parent / group), config=str(config_path), forceConfigRoot=False)
    butler = Butler(str(parent / group), writeable=True)
    universe = butler.registry.dimensions
    for name, (storage_class, dimensions) in rows.items():
        butler.registry.registerDatasetType(
            DatasetType(name=name, storageClass=storage_class, dimensions=dimensions, universe=universe)
        )
    del butler
    shutil.move(parent / group / f"{group}.sqlite", repo_dir / f"{group}.sqlite")
    shutil.rmtree(parent)
    # Keep the committed file small; also drops any journal residue.
    with sqlite3.connect(repo_dir / f"{group}.sqlite") as connection:
        connection.execute("VACUUM")
    print(f"{group}: registered {len(rows)} dataset types")


def committed_rows(group: str) -> list[str]:
    """Names in a committed fixture database, for comparison against ROWS."""
    with sqlite3.connect(FIXTURES / group / f"{group}.sqlite") as connection:
        return sorted(row[0] for row in connection.execute("SELECT name FROM dataset_type"))


if __name__ == "__main__":
    for group, rows in ROWS.items():
        build(group, rows)
        assert set(committed_rows(group)) == set(rows), group
