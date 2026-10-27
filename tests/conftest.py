"""Fixtures for future-images-migrate.py CLI regression tests.

Every test drives the tool through ``run_migration(tmp_path, *args)`` (the
script is not importable because of its hyphenated name) against a per-test
tree::

    tmp/dp2_prep/butler.yaml           external legacy repo (pre-migration)
    tmp/dp2_prep/butler_future.yaml    external future repo (pre-migration)
    tmp/dp2_prep/dp2_prep.sqlite       shared sqlite DB
    tmp/repo-index.yaml                ButlerRepoIndex (DAF_BUTLER_REPOSITORY_INDEX)
    tmp/migrations/<ticket>/...        tool working dir (FUTURE_IMAGES_MIGRATIONS_ROOT)

The configs use the historical datastore root tag plus the analogous db-URL
tag, ``sqlite:///<butlerRoot>/dp2_prep.sqlite``, which butler expands against
the config file's directory; both configs thus share one DB with no
process-cwd dependence.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest
import sqlalchemy
import yaml

from lsst.daf.butler import Butler, DatasetType

TOOL_DIR = Path(__file__).parent.parent
DATA_DIR = Path(__file__).parent / "data"

REPO = "dp2_prep"
FUTURE_REPO = REPO + "_future"
TICKET = "T1"

# Dataset types registered in the shared DB: name -> (storage class,
# dimensions).  The dimension sets expand automatically against the
# universe (e.g. {visit, detector} also brings instrument, band, ...).
BASE_TYPES = {
    "visit_image": ("ExposureF", ("visit", "detector")),
    "difference_image": ("ExposureF", ("visit", "detector")),
    "deep_coadd": ("ExposureF", ("patch", "band")),
}

# Registry dataset_type rows (as named in the database) before and after
# the canonical apply: the renames swap base names onto the future
# definitions and park the legacy definitions under legacy_.
PRE_APPLY_NAMES = set(BASE_TYPES) | {f"future_{name}" for name in BASE_TYPES}
APPLIED_NAMES = set(BASE_TYPES) | {f"legacy_{name}" for name in BASE_TYPES}

# --- add-variant feature contract (single points of change for
# integration) ---
# Key in migration.json holding the variant list.
VARIANTS_KEY = "variants"
# Variant repo aliases (fixture-level per the agreed contract), modeled on
# the historical DM-55129 dp2_prep+sasquatch_dev chained-datastore variant.
LEGACY_VARIANT = REPO + "+sasquatch_dev"
FUTURE_VARIANT = REPO + "_future+sasquatch_dev"

# --- second independent migration pair (for whole-ticket summary grouping
# tests): same dataset-type story, its own sqlite DB.  future_visit_image is
# deliberately NOT registered in its DB, so analyze falls back to
# storage-classes.json for its future storage class ("VisitImage", matching
# the first pair's registered one): the same base name then renders as two
# summary entries (is_registered differs) while the cross-repo
# future-storage-class guard passes.  Tests that want a genuine class
# conflict register future_visit_image themselves (now rejected by analyze).
# ---
SECOND = "second"
SECOND_FUTURE = SECOND + "_future"
SECOND_FUTURE_TYPES = {
    "future_difference_image": ("DifferenceImage", ("visit", "detector")),
    "future_deep_coadd": ("CellCoadd", ("patch", "band")),
}


def data_text(name: str) -> str:
    """Contents of a historical config file from ``tests/data``, verbatim."""
    return (DATA_DIR / name).read_text()


def register_dataset_types(alias: str, types: dict[str, tuple[str, tuple[str, ...]]]) -> None:
    """Register dataset types in the shared sqlite DB via a real Butler.

    The repository is opened by alias, resolved through the
    ``DAF_BUTLER_REPOSITORY_INDEX`` file set up by the fixture, just as
    production butlers resolve repo labels.
    """
    with Butler.from_config(alias, writeable=True) as butler:
        registry = butler.registry
        universe = registry.dimensions
        for name, (storage_class, dimensions) in types.items():
            registry.registerDatasetType(
                DatasetType(
                    name=name,
                    storageClass=storage_class,
                    dimensions=dimensions,
                    universe=universe,
                )
            )


def dataset_type_rows(alias: str) -> dict[str, str]:
    """The dataset_type table rows straight from the database, name to
    storage class, ignoring any config-level renames.
    """
    with Butler.from_config(alias) as butler:
        database = butler.registry._registry._db
        table = f"{database.namespace}.dataset_type" if database.namespace else "dataset_type"
        with database._engine.connect() as connection:
            return {
                row[0]: row[1]
                for row in connection.execute(sqlalchemy.text(f"SELECT name, storage_class FROM {table}"))
            }


def dataset_type_names(alias: str) -> set[str]:
    return set(dataset_type_rows(alias))


class MigrationRun(NamedTuple):
    """Outcome of one ``run_migration`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    migration: dict | None
    """Parsed ``migration.json`` (``None`` if it does not exist yet)."""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def migration_dir(tmp_path: Path, prime: str = REPO) -> Path:
    """Working dir of one migration (keyed by its primary/legacy repo)."""
    return tmp_path / "migrations" / TICKET / prime


def stored_config(tmp_path: Path, repo: str, kind: str, prime: str = REPO) -> Path:
    """Path to a stored config file in a migration working dir, named
    ``{repo}.{kind}.yaml`` by repo id (mirrors RepoMigration.file_path);
    ``prime`` selects which migration (directory) to look in."""
    return migration_dir(tmp_path, prime) / f"{repo}.{kind}.yaml"


def set_stored_config(tmp_path: Path, repo: str, kind: str, data_name: str, prime: str = REPO) -> None:
    """Overwrite a stored config file with a historical file from
    tests/data."""
    stored_config(tmp_path, repo, kind, prime).write_text(data_text(data_name))


def run_migration(tmp_path: Path, *args: str) -> MigrationRun:
    """Run the migration tool with the given CLI args against this tree and
    return its result along with the parsed migration.json, if any."""
    env = dict(
        os.environ,
        DAF_BUTLER_REPOSITORY_INDEX=str(tmp_path / "repo-index.yaml"),
        FUTURE_IMAGES_MIGRATIONS_ROOT=str(tmp_path / "migrations"),
    )
    result = subprocess.run(
        [sys.executable, str(TOOL_DIR / "future-images-migrate.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    json_path = migration_dir(tmp_path) / "migration.json"
    migration = json.loads(json_path.read_text()) if json_path.exists() else None
    return MigrationRun(result.returncode, result.stdout, result.stderr, migration)


def assert_rejected(run: MigrationRun, substring: str) -> None:
    assert run.returncode == 1, f"expected failure, got rc={run.returncode}: {run.stdout}"
    assert substring in run.stderr, f"{substring!r} not in stderr:\n{run.stderr}"


class RepoPair:
    """The external legacy/future repository pair sharing one sqlite DB."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.repo_dir = tmp_path / REPO

    def external(self, side: str) -> Path:
        return self.repo_dir / ("butler.yaml" if side == "legacy" else "butler_future.yaml")

    def install_external(self, side: str, data_name: str) -> None:
        """Install a historical config file as one side's external config."""
        shutil.copyfile(DATA_DIR / data_name, self.external(side))

    def drift_external(self, side: str) -> None:
        """Deliberately drift an external config after init by changing its
        datastore root (legal, but makes the stored copy deviate)."""
        path = self.external(side)
        text = path.read_text()
        assert "root: <butlerRoot>" in text
        path.write_text(text.replace("root: <butlerRoot>", "root: <butlerRoot>/drifted", 1))

    def variant_external(self, side: str) -> Path:
        """External config file of the sasquatch-dev variant of one side
        (lives in the repo dir by default)."""
        if side == "legacy":
            return self.repo_dir / f"butler{LEGACY_VARIANT.removeprefix(REPO)}.yaml"
        suffix = FUTURE_VARIANT.removeprefix(f"{REPO}_future")
        return self.repo_dir / f"butler_future{suffix}.yaml"

    def install_variant_externals(self) -> None:
        """Install the historical variant externals (pre-migration baseline
        state: old contents) for both sides."""
        shutil.copyfile(DATA_DIR / "legacy-variant-old.yaml", self.variant_external("legacy"))
        shutil.copyfile(DATA_DIR / "future-variant-old.yaml", self.variant_external("future"))

    def add_alias(self, alias: str, config_path: Path) -> None:
        """Point a repo-index alias at a (possibly relocated) config file."""
        index_path = self.tmp_path / "repo-index.yaml"
        index = yaml.safe_load(index_path.read_text())
        index[alias] = str(config_path)
        index_path.write_text(yaml.safe_dump(index))

    def register_types(self, types: dict[str, tuple[str, tuple[str, ...]]]) -> None:
        register_dataset_types(REPO, types)

    def register_future_types(self) -> None:
        self.register_types(
            {
                "future_visit_image": ("VisitImage", ("visit", "detector")),
                "future_difference_image": ("DifferenceImage", ("visit", "detector")),
                "future_deep_coadd": ("CellCoadd", ("patch", "band")),
            }
        )


def write_repo_index(tmp_path: Path) -> Path:
    """Write ``tmp_path/repo-index.yaml`` mapping every fixture alias (both
    migration pairs and the variant pair) to its conventional location
    under ``tmp_path``, and return the index path.

    ``SECOND``/``SECOND_FUTURE`` point at the configs ``build_second_pair``
    materializes separately; the aliases are registered here because
    daf-butler caches the repo index per process (in-process butlers resolve
    them from the fixture's first Butler touch onward).
    """
    pair = RepoPair(tmp_path)
    index_path = tmp_path / "repo-index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            {
                REPO: str(pair.external("legacy")),
                FUTURE_REPO: str(pair.external("future")),
                LEGACY_VARIANT: str(pair.variant_external("legacy")),
                FUTURE_VARIANT: str(pair.variant_external("future")),
                SECOND: str(tmp_path / SECOND / "butler.yaml"),
                SECOND_FUTURE: str(tmp_path / SECOND / "butler_future.yaml"),
            }
        )
    )
    return index_path


def build_repo_pair(tmp_path: Path) -> RepoPair:
    """Create the external legacy/future repo pair sharing one sqlite DB
    (base dataset types registered), plus the repo index and migrations
    root, and return the handle with no migration run.

    The caller must point ``DAF_BUTLER_REPOSITORY_INDEX`` at the
    conventional ``write_repo_index`` path first: alias-based registration
    resolves through it, in-process just as the tool's subprocesses do.
    """
    repo_pair = RepoPair(tmp_path)
    repo_pair.repo_dir.mkdir()
    migrations_root = tmp_path / "migrations"
    migrations_root.mkdir()
    shutil.copy(TOOL_DIR / "storage-classes.json", migrations_root)
    write_repo_index(tmp_path)
    # makeRepo (not hand-forged) writes the legacy external config and
    # initializes the shared sqlite schema; the <butlerRoot> db-URL tag
    # resolves against the repo root passed to makeRepo.
    Butler.makeRepo(
        repo_pair.repo_dir,
        config=str(DATA_DIR / "legacy-old.yaml"),
        forceConfigRoot=False,
    )
    repo_pair.install_external("future", "future-old.yaml")
    repo_pair.install_variant_externals()
    repo_pair.register_types(BASE_TYPES)
    return repo_pair


def canonical_new_state(pair: RepoPair) -> None:
    """Put the stored configs in the canonical dp2_prep post-edit state."""
    pair.register_future_types()
    set_stored_config(pair.tmp_path, REPO, "new", "legacy-new.yaml")
    set_stored_config(pair.tmp_path, FUTURE_REPO, "new", "future-new.yaml")


def mirror_variant_state(pair: RepoPair) -> None:
    """Edit the variant stored new files so their overrides mirror the
    primary's post-edit state."""
    set_stored_config(pair.tmp_path, LEGACY_VARIANT, "new", "legacy-variant-new.yaml")
    set_stored_config(pair.tmp_path, FUTURE_VARIANT, "new", "future-variant-new.yaml")


def build_second_pair(repo_pair: RepoPair) -> None:
    """Materialize the second independent migration pair
    (``second``/``second_future``) under ``repo_pair.tmp_path``: externals
    installed, base types registered in its own DB, and future_* types
    registered per ``SECOND_FUTURE_TYPES`` (visit_image's future definition
    is left unregistered on purpose — see the constant's comment).  Does not
    run ``init`` for the second pair; tests drive the CLI themselves."""
    second_dir = repo_pair.tmp_path / SECOND
    second_dir.mkdir()
    Butler.makeRepo(
        second_dir,
        config=str(DATA_DIR / "second-old.yaml"),
        forceConfigRoot=False,
    )
    shutil.copyfile(DATA_DIR / "second-future-old.yaml", second_dir / "butler_future.yaml")
    repo_pair.add_alias(SECOND, second_dir / "butler.yaml")
    repo_pair.add_alias(SECOND_FUTURE, second_dir / "butler_future.yaml")
    register_dataset_types(SECOND, BASE_TYPES)
    register_dataset_types(SECOND, SECOND_FUTURE_TYPES)


@pytest.fixture
def repo_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepoPair:
    """Create the external legacy/future repo pair sharing one sqlite DB
    (base dataset types registered), but do not run any migration."""
    monkeypatch.setenv("DAF_BUTLER_REPOSITORY_INDEX", str(tmp_path / "repo-index.yaml"))
    return build_repo_pair(tmp_path)


@pytest.fixture
def tree(repo_pair: RepoPair) -> RepoPair:
    """``repo_pair`` with ``init`` run against the (untouched) pre-migration
    externals, so the stored *-old files match and deviation checks pass.

    Tests that need non-default externals should use ``repo_pair`` directly,
    call ``install_external()``, and ``run_migration(..., "init", ...)``
    themselves.
    """
    run = run_migration(repo_pair.tmp_path, "init", TICKET, REPO)
    assert run.ok, f"init failed: {run.stdout}\n{run.stderr}"
    return repo_pair


@contextlib.contextmanager
def _index_env(index_path: Path) -> Iterator[None]:
    """Point ``DAF_BUTLER_REPOSITORY_INDEX`` at ``index_path`` (restoring the
    previous value on exit) so in-process butlers resolve the right index."""
    previous = os.environ.get("DAF_BUTLER_REPOSITORY_INDEX")
    os.environ["DAF_BUTLER_REPOSITORY_INDEX"] = str(index_path)
    try:
        yield
    finally:
        if previous is None:
            del os.environ["DAF_BUTLER_REPOSITORY_INDEX"]
        else:
            os.environ["DAF_BUTLER_REPOSITORY_INDEX"] = previous


@pytest.fixture(scope="session")
def migration_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Session-built prepared migration trees (``run_migration`` drives the
    CLI against them with ``FUTURE_IMAGES_MIGRATIONS_ROOT`` derived from
    their root); tests deep-copy entries via ``copy_template`` instead of
    rebuilding state through the CLI:

    ``ready`` / ``ready_unanalyzed``
        First pair with variant: init, add-variant, canonical ``*-new``
        edits with mirrored variant state; analyze run / not yet run.
    ``two_unanalyzed``
        Both pairs init'd and stored-new edited, but no variant and no
        analyze (matching the fixtures those tests built by hand).
    ``two_ready``
        ``two_unanalyzed`` with analyze run.

    Stored configs and migration.json are ``<butlerRoot>``-tagged or
    path-free, so a copied tree is valid once ``write_repo_index``
    regenerates its index; the template's own index is never used by a
    test."""
    roots = {
        key: tmp_path_factory.mktemp(f"migration-{key}")
        for key in ("ready_unanalyzed", "ready", "two_unanalyzed", "two_ready")
    }
    base = tmp_path_factory.mktemp("migration-base")

    def snapshot(src: Path, dest: Path) -> None:
        # The copied tree's repo-index.yaml still names src's absolute
        # paths; regenerate it (and later copy_template regenerates the
        # per-test copies' indices again).
        shutil.copytree(src, dest, dirs_exist_ok=True)
        write_repo_index(dest)

    def cli(root: Path, *args: str) -> None:
        run = run_migration(root, *args)
        assert run.ok, f"{args[0]} failed: {run.stdout}\n{run.stderr}"

    with _index_env(base / "repo-index.yaml"):
        build_repo_pair(base)
        cli(base, "init", TICKET, REPO)
        canonical_new_state(RepoPair(base))
        build_second_pair(RepoPair(base))
        cli(base, "init", TICKET, REPO, SECOND)
        set_stored_config(base, SECOND, "new", "second-new.yaml", prime=SECOND)
        set_stored_config(base, SECOND_FUTURE, "new", "second-future-new.yaml", prime=SECOND)
        snapshot(base, roots["two_unanalyzed"])
        cli(base, "add-variant", TICKET, REPO, LEGACY_VARIANT, FUTURE_VARIANT)
        mirror_variant_state(RepoPair(base))
        shutil.rmtree(base / SECOND)
        shutil.rmtree(migration_dir(base, prime=SECOND))
        snapshot(base, roots["ready_unanalyzed"])
        cli(base, "analyze", TICKET)
        snapshot(base, roots["ready"])
    # two_ready's analyze must see the second pair again, so re-copy the
    # un-stripped snapshot and analyze in the copy (run_migration builds
    # the subprocess env from the root argument).
    snapshot(roots["two_unanalyzed"], roots["two_ready"])
    cli(roots["two_ready"], "analyze", TICKET)
    return roots


def copy_template(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RepoPair:
    """Deep-copy a prepared migration tree into this test's ``tmp_path``
    (same relative layout, so ``run_migration``'s env needs no change) and
    regenerate its repo index to point at the copy."""
    shutil.copytree(template, tmp_path, dirs_exist_ok=True)
    index_path = write_repo_index(tmp_path)
    monkeypatch.setenv("DAF_BUTLER_REPOSITORY_INDEX", str(index_path))
    return RepoPair(tmp_path)


@pytest.fixture
def ready_unanalyzed_tree(
    migration_templates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RepoPair:
    """The prepared first migration (variant + canonical edits) before any
    analyze."""
    return copy_template(migration_templates["ready_unanalyzed"], tmp_path, monkeypatch)


@pytest.fixture
def two_unanalyzed_tree(
    migration_templates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RepoPair:
    """Both ticket migrations prepared (no variant, no mirror edits)
    before any analyze."""
    return copy_template(migration_templates["two_unanalyzed"], tmp_path, monkeypatch)
