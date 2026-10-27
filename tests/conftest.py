"""Fixtures for future-images-migrate.py CLI tests.

Every test drives the tool through subprocesses against its own copy of the
committed fixture under ``tests/fixtures``: two butler repository groups, each
a real butler repo directory with a shared sqlite registry, committed as-is.
A copy of a group is a working repository wherever it is placed (all paths in
the configs use the ``<butlerRoot>`` tag), so per-test copies are free and
tests never rebuild a database.

The tree a test runs against::

    tmp/dp2_prep/butler.yaml                  external members (aliases)
    tmp/dp2_prep/dp2_prep.sqlite              the group's shared registry
    tmp/v3_skills_unit/...                    the second group
    tmp/repo-index.yaml                       ButlerRepoIndex (per test, absolute)
    tmp/migrations/<ticket>/...               tool working dir

``tests/fixtures/build_fixture.py`` holds the groups' state model (the dataset
types in each database) and rebuilds the committed registries; tests read it
rather than restating it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

TOOL_DIR = Path(__file__).parent.parent
TOOL = TOOL_DIR / "future-images-migrate.py"
FIXTURES = Path(__file__).parent / "fixtures"

TICKET = "T1"

PRIMARY = "dp2_prep"
FUTURE_PRIMARY = "dp2_prep_future"
LEGACY_VARIANT = "dp2_prep+sasquatch_dev"
FUTURE_VARIANT = "dp2_prep_future+sasquatch_dev"
SECONDARY = "v3_skills_unit"
SECONDARY_FUTURE = "v3_skills_unit_future"

# Repo index alias -> (group directory, config file within it).  A group is one
# migration, named for its primary legacy repo: every member of a group shares
# its registry database, and their stored config copies share its directory.
GROUPS: dict[str, dict[str, str]] = {
    PRIMARY: {
        PRIMARY: "butler.yaml",
        FUTURE_PRIMARY: "butler_future.yaml",
        LEGACY_VARIANT: "butler+sasquatch_dev.yaml",
        FUTURE_VARIANT: "butler_future+sasquatch_dev.yaml",
    },
    SECONDARY: {
        SECONDARY: "butler.yaml",
        SECONDARY_FUTURE: "butler_future.yaml",
    },
}

MEMBERS: dict[str, tuple[str, str]] = {
    alias: (group, name) for group, members in GROUPS.items() for alias, name in members.items()
}

ALL_MEMBERS = tuple(MEMBERS)

# The members ``init`` seeds, one legacy/future pair per group.  Variants join
# their group later, through ``add-variant``.
PRIMARY_MEMBERS = tuple(alias for group in GROUPS for alias in (group, f"{group}_future"))


def _load_fixture_builder():
    """The committed fixture generator, for its state model (``ROWS``)."""
    path = FIXTURES / "build_fixture.py"
    spec = importlib.util.spec_from_file_location("build_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_fixture = _load_fixture_builder()

# Dataset types present in each committed group's database, as the registry
# names them: group -> name -> (storage class, dimensions).
INITIAL_ROWS = build_fixture.ROWS

# The same rows as name -> storage class, the form a group's database is
# compared against.
INITIAL_ROW_CLASSES = {
    group: {name: storage_class for name, (storage_class, _) in rows.items()}
    for group, rows in INITIAL_ROWS.items()
}


def committed_external(alias: str) -> bytes:
    """A member's external config exactly as committed under ``tests/fixtures``."""
    group, name = MEMBERS[alias]
    return (FIXTURES / group / name).read_bytes()


def committed_target(alias: str) -> bytes:
    """The hand-edited target config committed for one member."""
    return (FIXTURES / "targets" / f"{alias}.new.yaml").read_bytes()


class Run(NamedTuple):
    """Outcome of one tool invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_cli(root: Path, *args: str) -> Run:
    """Run the migration tool against a tree and return what it printed.

    The tree's own ``repo-index.yaml`` and ``migrations`` directory define its
    world, so the same arguments run against any copy.
    """
    env = dict(
        os.environ,
        DAF_BUTLER_REPOSITORY_INDEX=str(root / "repo-index.yaml"),
        FUTURE_IMAGES_MIGRATIONS_ROOT=str(root / "migrations"),
    )
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return Run(result.returncode, result.stdout, result.stderr)


class Tree:
    """A migration tree: the committed fixture groups plus the tool's working
    dir, driven to whatever point in the lifecycle its fixture reached.

    Members are addressed by repo index alias; ``group`` names the migration
    (the primary legacy repo) whose directory holds a member's stored copies.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def external(self, alias: str) -> Path:
        """One member's external config file — the file the tool copies over."""
        group, name = MEMBERS[alias]
        return self.root / group / name

    def stored(self, alias: str, kind: str) -> Path:
        """A stored config copy of one member, named ``{alias}.{kind}.yaml``
        (mirrors ``RepoMigration.file_path``)."""
        return self.root / "migrations" / TICKET / MEMBERS[alias][0] / f"{alias}.{kind}.yaml"

    def migration(self, group: str) -> dict:
        """A group's ``migration.json``, parsed."""
        path = self.root / "migrations" / TICKET / group / "migration.json"
        return json.loads(path.read_text())

    def rows(self, group: str) -> dict[str, str]:
        """A group's ``dataset_type`` rows, name to storage class.

        Read straight from sqlite — the same table-level access ``apply`` uses.
        Config-level renames do not apply here: these are the database names.
        """
        with sqlite3.connect(self.root / group / f"{group}.sqlite") as connection:
            return dict(connection.execute("SELECT name, storage_class FROM dataset_type"))

    def storage_classes(self) -> Path:
        """This tree's copy of the future storage-class table."""
        return self.root / "migrations" / "storage-classes.json"

    def edit(self, path: Path, old: str, new: str) -> None:
        """Replace the first occurrence of ``old`` in one of this tree's config
        files, failing if it is not there: a forgery that stops matching the
        committed fixture should fail loudly rather than turn into a test that
        passes because nothing was changed."""
        text = path.read_text()
        assert old in text, f"{path} does not contain {old!r}"
        path.write_text(text.replace(old, new, 1))

    def run(self, *args: str) -> Run:
        """Invoke the tool against this tree."""
        return run_cli(self.root, *args)


def _write_repo_index(root: Path) -> None:
    """Point every fixture alias at this tree's copy of its config file.

    Regenerated for every copy: index values must be absolute paths, and
    ``ButlerRepoIndex`` resolves relative ones against the process cwd rather
    than the index file, so a committed index could not survive relocation.
    """
    (root / "repo-index.yaml").write_text(
        yaml.safe_dump({alias: str(root / group / name) for alias, (group, name) in MEMBERS.items()})
    )


def _copy_fixture(root: Path) -> Tree:
    """Copy both committed fixture groups and the storage-class table into an
    empty tree, ready for ``init``."""
    for group in GROUPS:
        shutil.copytree(FIXTURES / group, root / group)
    (root / "migrations").mkdir()
    shutil.copy(TOOL_DIR / "storage-classes.json", root / "migrations")
    _write_repo_index(root)
    return Tree(root)


def _edit_targets(tree: Tree) -> None:
    """Copy the committed hand-edited targets over the stored ``new`` configs,
    which is what editing a ticket by hand does."""
    for alias in ALL_MEMBERS:
        shutil.copyfile(FIXTURES / "targets" / f"{alias}.new.yaml", tree.stored(alias, "new"))


@pytest.fixture(scope="session")
def stages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Tree]:
    """The fixture driven through the lifecycle once, snapshotted after each
    stage: ``prepared`` (both groups initialized, no variant), ``edited`` (the
    variant added and the targets copied over the stored ``new`` files),
    ``analyzed`` and ``applied``.

    Per-test fixtures copy a snapshot instead of re-running the CLI to get
    there.  The snapshots are path-free — stored configs carry the
    ``<butlerRoot>`` tag and ``migration.json`` carries no paths — so a copy is
    valid once its repo index is regenerated.
    """
    root = tmp_path_factory.mktemp("staged")
    tree = _copy_fixture(root)
    snapshots: dict[str, Tree] = {}

    def cli(*args: str) -> None:
        run = tree.run(*args)
        assert run.ok, f"{args[0]} failed:\n{run.stdout}\n{run.stderr}"

    def snapshot(stage: str) -> None:
        dest = tmp_path_factory.mktemp(f"snapshot-{stage}")
        shutil.copytree(root, dest, dirs_exist_ok=True)
        _write_repo_index(dest)
        snapshots[stage] = Tree(dest)

    cli("init", TICKET, PRIMARY, SECONDARY)
    snapshot("prepared")
    cli("add-variant", TICKET, PRIMARY, LEGACY_VARIANT, FUTURE_VARIANT)
    _edit_targets(tree)
    snapshot("edited")
    cli("analyze", TICKET)
    snapshot("analyzed")
    cli("apply", TICKET)
    snapshot("applied")
    return snapshots


def _stage(stage: str, stages: dict[str, Tree], tmp_path: Path) -> Tree:
    """Copy one staged snapshot into a test's own tree."""
    shutil.copytree(stages[stage].root, tmp_path, dirs_exist_ok=True)
    _write_repo_index(tmp_path)
    return Tree(tmp_path)


@pytest.fixture
def prepared(stages, tmp_path: Path) -> Tree:
    """Both groups initialized from the committed externals; no variant, no
    edits, no analysis."""
    return _stage("prepared", stages, tmp_path)


@pytest.fixture
def edited(stages, tmp_path: Path) -> Tree:
    """``prepared`` with the variant pair added and every member's stored
    ``new`` file replaced by its committed target."""
    return _stage("edited", stages, tmp_path)


@pytest.fixture
def analyzed(stages, tmp_path: Path) -> Tree:
    """``edited`` with ``analyze`` run, so each group's ``migration.json``
    holds its classified dataset types."""
    return _stage("analyzed", stages, tmp_path)


@pytest.fixture
def applied(stages, tmp_path: Path) -> Tree:
    """``analyzed`` with ``apply`` run: databases renamed, externals replaced
    by their stored ``new`` copies."""
    return _stage("applied", stages, tmp_path)
