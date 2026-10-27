#!/usr/bin/env python

"""Tool for migrating butler repositories to future dataset type definitions.

External repositories resolve through lsst.resources, so index aliases may
point at remote configs (e.g. s3://, with the usual S3_ENDPOINT_URL and
credential environment); migration dirs and stored copies are always
local. ``<butlerRoot>`` in a remote-hosted config expands only to the
remote URI, so it is meaningful for datastore roots, never sqlite db URLs.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import click
import pydantic
import sqlalchemy

from lsst.daf.butler import Butler, ButlerConfig, DatasetType, MissingDatasetTypeError
from lsst.utils.classes import Singleton

MIGRATIONS_ROOT = Path(os.environ.get("FUTURE_IMAGES_MIGRATIONS_ROOT", Path(__file__).parent))
"""Base directory for migration working directories and storage-classes.json.

Defaults to the parent directory of this script.
Override with the FUTURE_IMAGES_MIGRATIONS_ROOT environment variable.
"""


@dataclasses.dataclass(frozen=True)
class AliasKey:
    """DB URL and optional namespace, used to check whether two butler configs
    point to the same underlying repo.
    """

    @classmethod
    def from_config(cls, config: ButlerConfig) -> AliasKey:
        return cls(config[".registry.db"], config.get(".registry.namespace", None))

    url: str
    namespace: str | None

    def __str__(self) -> str:
        return f"db={self.url!r}, namespace={self.namespace!r}"


type ConfigKind = Literal["old", "new"]
"""A kind of butler configration stored in a migration.

``old`` is the rollback copy of of the external repository configation.

``new`` the edited target that ``apply`` writes.
"""

type PairSide = Literal["legacy", "future"]
"""One side of a repository migration piar."""

CHAINED_DATASTORE = "lsst.daf.butler.datastores.chainedDatastore.ChainedDatastore"


class FutureStorageClasses(metaclass=Singleton):
    """A singleton that provides a centralized mapping from (base) dataset
    type name to future storage class (as saved to ``storage-class.json``.)
    """

    def __init__(self) -> None:
        self._dict: dict[str, str] | None = None

    def _classes(self) -> dict[str, str]:
        if self._dict is None:
            self._dict = json.loads((MIGRATIONS_ROOT / "storage-classes.json").read_text())
        return self._dict

    def get(self, legacy_butler: Butler, dataset_type: str) -> tuple[str, bool]:
        """Look up the storage class for the given dataset type.

        Parameters
        ----------
        legacy_butler
            Legacy-configured butler to look up the future dataset type in.
        dataset_type
            Name of the dataset type.

        Returns
        -------
        `str`
            Name of the storage class.
        `bool`
            Whether the future dataset type is registered.
        """
        try:
            future_dataset_type = legacy_butler.get_dataset_type("future_" + dataset_type)
        except MissingDatasetTypeError as err:
            try:
                storage_class = self._classes()[dataset_type]
            except KeyError:
                err.add_note(
                    "Register this dataset type or add it to the migration tool's storage-classes.json."
                )
                raise err from None
            return storage_class, False
        return future_dataset_type.storageClass_name, True


class DatasetTypeMigration(pydantic.BaseModel):
    """Persistable struct that represents a single dataset type configuration
    override change and possible associated migration.
    """

    legacy_storage_class: str
    """The storage class for the legacy dataset type."""

    future_storage_class: str
    """The storage class for the future dataset type."""

    is_registered: bool
    """Whether the future definition of the dataset type has been registered
    (legacy definitions are assumed to have already been registered).
    """

    is_db_migration: bool
    """Whether this an actual database migration that moves the future
    definition into the database (`True`) or a config override in the future_
    repo only.
    """


@dataclasses.dataclass
class DatasetTypeOverride:
    """A temporary struct used to hold information about a dataset type
    override extracted from a config file.
    """

    db_is_future: bool
    """Whether this repository's butler DB assigns the base name to the future
    definition (as implied by its config overrides).
    """

    has_legacy_side: bool = False
    """Whether the config has a mapping with a legacy_ prefix (in either
    direction).
    """

    has_future_side: bool = False
    """Whether the config has a mapping with a future_ prefix (in either
    direction).
    """


@dataclasses.dataclass
class ConfigOverrides:
    """The dataset type overrides of one butler config file."""

    pairs: dict[str, DatasetTypeOverride] = dataclasses.field(default_factory=dict)
    """Overrides this tool fully supports, keyed by dataset type base name
    (``legacy_`` / ``future_`` pairs).
    """

    foreign: dict[str, str] = dataclasses.field(default_factory=dict)
    """Other overrides that this tool minimally checks but otherwise ignores.

    These are mappings from the underlying database name to the rename visible
    in the repo.
    """


def get_dataset_type_or_none(butler: Butler, name: str) -> DatasetType | None:
    """Look up a dataset type by name and return `None` if is not registered.

    Parameters
    ----------
    butler
        Client to look the name up with.
    name
        Client-visible dataset type name.
    """
    try:
        return butler.get_dataset_type(name)
    except MissingDatasetTypeError:
        return None


class RepoPair(pydantic.BaseModel):
    """A legacy/future pair of data repositories.

    Repository pairs share the same database and file storage, and differ only
    in how certain dataset type overrides are configured.
    """

    legacy_repo: str
    """The legacy repository, in which the base name of a dataset type maps to
    its legacy definition, and a ``future_``-prefixed variant maps to the
    future definition.
    """

    future_repo: str
    """The legacy repository, in which the base name of a dataset type maps to
    its legacy definition, and a ``future_``-prefixed variant maps to the
    future definition.
    """

    def iter_all_repos(self) -> Iterator[str]:
        """Iterate over the repositories in this pair."""
        yield self.legacy_repo
        yield self.future_repo

    @pydantic.model_validator(mode="after")
    def _check_future_is_not_legacy(self) -> RepoPair:
        if self.legacy_repo == self.future_repo:
            raise ValueError(f"Invalid self-migration: {self.legacy_repo}.")
        return self

    @staticmethod
    def extract_overrides(config: ButlerConfig) -> ConfigOverrides:
        """Process a butler config file to extract dataset type overrides from
        it.
        """
        match config[".registry.managers.datasets"]:
            case str():
                return ConfigOverrides()
            case {"cls": _, "config": {"overrides": cfg_overrides}}:
                pass
            case {"cls": _}:
                return ConfigOverrides()
            case _:
                raise ValueError(f"Could not parse .registry.managers.datasets from {config.configFile}.")
        entries: dict[str, str] = {}
        for db_dt_name, nested in cfg_overrides.toDict().items():
            match nested:
                case {"rename": override_dt_name}:
                    entries[db_dt_name] = override_dt_name
                case _:
                    raise ValueError(f"Could not parse {db_dt_name} from {config.configFile}")

        result: dict[str, DatasetTypeOverride] = {}
        foreign: dict[str, str] = {}
        paired_names: set[str] = set()
        for db_dt_name, override_dt_name in entries.items():
            if db_dt_name == "legacy_" + override_dt_name:
                # If a butler DB's dataset type starts with "legacy_", it means
                # that DB gives the future definition the base name, so
                # db_is_future=True.
                override = result.setdefault(override_dt_name, DatasetTypeOverride(db_is_future=True))
                if not override.db_is_future:
                    raise ValueError(
                        "Inconsistent override direction for "
                        f".registry.managers.datasets.{db_dt_name} from {config.configFile}."
                    )
                override.has_legacy_side = True
            elif db_dt_name == "future_" + override_dt_name:
                # If a butler DB's dataset type starts with "future_", it means
                # that DB gives the legacy definition the base name, so
                # db_is_future=False.
                override = result.setdefault(override_dt_name, DatasetTypeOverride(db_is_future=False))
                if override.db_is_future:
                    raise ValueError(
                        "Inconsistent override direction for "
                        f".registry.managers.datasets.{db_dt_name} from {config.configFile}."
                    )
                override.has_future_side = True
            elif "legacy_" + db_dt_name == override_dt_name:
                # If the override name starts with "legacy_", it means the DB
                # gives the legacy definition the base name so
                # db_is_future=False.
                override = result.setdefault(db_dt_name, DatasetTypeOverride(db_is_future=False))
                if override.db_is_future:
                    raise ValueError(
                        "Inconsistent override direction for "
                        f".registry.managers.datasets.{override_dt_name} from {config.configFile}."
                    )
                override.has_legacy_side = True
            elif "future_" + db_dt_name == override_dt_name:
                # If the override name starts with "future_", it means the DB
                # gives the future definition the base name so
                # db_is_future=True.
                override = result.setdefault(db_dt_name, DatasetTypeOverride(db_is_future=True))
                if not override.db_is_future:
                    raise ValueError(
                        "Inconsistent override direction for "
                        f".registry.managers.datasets.{db_dt_name} from {config.configFile}."
                    )
                override.has_future_side = True
            else:
                # A mapping that doesn't involve ``legacy_`` / ``future_``
                # prefixes is considered "foreign" and only minimally checked.
                foreign[db_dt_name] = override_dt_name
                continue
            paired_names.update((db_dt_name, override_dt_name))
        for base_name, override in result.items():
            if not override.has_legacy_side:
                raise ValueError(f"Missing legacy-side override for {base_name} in {config.configFile}.")
            if not override.has_future_side:
                raise ValueError(f"Missing future-side override for {base_name} in {config.configFile}.")
            if base_name.startswith("future_") or base_name.startswith("legacy_"):
                raise ValueError(f"Unsupported prefix for dataset type {base_name}.")
        for db_dt_name, override_dt_name in foreign.items():
            if db_dt_name in paired_names or override_dt_name in paired_names:
                # This looked like a foreign override on one side, and
                # legacy_/future_ pair on the other side.
                raise ValueError(
                    f"Unexpected name pattern in {db_dt_name}-{override_dt_name} from {config.configFile}."
                )
            for name in (db_dt_name, override_dt_name):
                if name.startswith("future_") or name.startswith("legacy_"):
                    raise ValueError(f"Unsupported prefix for dataset type {name} from {config.configFile}.")
        # Two rows renamed onto one visible name would be ambiguous, ad-hoc
        # renames included.
        visible_names: set[str] = set()
        for db_dt_name, override_dt_name in entries.items():
            if override_dt_name in visible_names:
                raise ValueError(
                    f"{db_dt_name} and an earlier override both rename to "
                    f"{override_dt_name} in {config.configFile}."
                )
            visible_names.add(override_dt_name)
        return ConfigOverrides(pairs=result, foreign=foreign)

    @staticmethod
    def datastore_sections(config: ButlerConfig) -> list[dict]:
        """Return the concrete datastore sections of a config, expanding any
        ChainedDatastore wrapper into its ``datastores`` entries.
        """
        sections: list[dict] = []
        stack = [config[".datastore"].toDict()]
        while stack:
            section = stack.pop()
            if section.get("cls") == CHAINED_DATASTORE:
                stack.extend(section.get("datastores", ()))
            else:
                sections.append(section)
        return sections

    @staticmethod
    def formatter_entries(config: ButlerConfig) -> dict[str, Any]:
        """Return the dataset-type-keyed formatter entries of a config across
        its datastore sections.

        The ``default`` and ``write_recipes`` sections name classes and
        recipe labels rather than dataset types and are skipped; the
        instrument-hierarchy sections are not modeled.
        """
        entries: dict[str, Any] = {}
        for section in RepoPair.datastore_sections(config):
            for name, entry in (section.get("formatters") or {}).items():
                if name not in ("default", "write_recipes"):
                    entries.setdefault(name, entry)
        return entries


class RepoMigration(RepoPair):
    """A legacy-future data repository pair that will have its dataset type
    override configuration changed and its database possibly migrated.
    """

    ticket: str
    """Ticket used to to identify this change."""

    dataset_types: dict[str, DatasetTypeMigration] = pydantic.Field(default_factory=dict)
    """Details of the changes to dataset types."""

    variants: list[RepoPair] = pydantic.Field(default_factory=list)
    """Variants of the repositories that are being migrated at the same time.

    Variants share the same database and filesystem storage as the migration's
    central repo pair, but have different configuration in some other way.
    """

    @staticmethod
    def root(ticket: str) -> Path:
        """The root directory for migration state for a ticket."""
        return MIGRATIONS_ROOT / ticket

    @property
    def dir_path(self) -> Path:
        """The directory for this change's state."""
        return self.root(self.ticket) / self.legacy_repo

    def file_path(self, repo: str, kind: ConfigKind) -> Path:
        """Return the path for a butler config file in this change.

        Parameters
        ----------
        repo
            Name of the butler repository.  May be the change's main
            `legacy_repo` or `future_repo` or any member of a variant pair.
        kind
            Whether this is an "old" config file (a copy of the external one,
            used for rollbacks) or the "new" one (hand-edited to diverge from
            "old").
        """
        return self.dir_path / f"{repo}.{kind}.yaml"

    def config(self, repo: str, kind: ConfigKind) -> Path:
        """Construct a butler config object for a file in this change.

        See `file_path` for parameters.
        """
        return ButlerConfig(self.file_path(repo, kind))

    def iter_all_repos(self) -> Iterator[str]:
        """Iterate over the repositories in this change, including variants."""
        yield from super().iter_all_repos()
        for pair in self.variants:
            yield from pair.iter_all_repos()

    def init_configs(self, reset: bool = False) -> AliasKey:
        """Initialize the configuration files in this change from the actual
        data repositories.

        Parameters
        ----------
        reset
            If `True`, reset configuration files if they have diverged from
            the actual repositories. "new" configurations are only updated if
            they haven't already diverged from "old".

        Returns
        -------
        db_url
            URL for the database used by this repository.
        db_namespace
            Optional namespace for the database used by this repository.
        """
        alias_key: AliasKey | None = None
        for repo in [self.legacy_repo, self.future_repo]:
            old_path = self.file_path(repo, "old")
            new_path = self.file_path(repo, "new")
            external_config = ButlerConfig(repo)
            if alias_key is None:
                alias_key = AliasKey.from_config(external_config)
            elif alias_key != AliasKey.from_config(external_config):
                raise ValueError(
                    f"{self.legacy_repo} and {self.future_repo} are not aliases of each other: "
                    f"{self.legacy_repo} {alias_key}, "
                    f"{self.future_repo} {AliasKey.from_config(external_config)}."
                )
            if old_path.exists():
                if reset:
                    # Only reset if the external config has drifted from 'old'
                    # since the previous init, but if we do we reset the 'new'
                    # variant as well, on the assumption that it's easier to
                    # reconstruct this migration's changes.
                    old_config = ButlerConfig(old_path)
                    if old_config.toDict() == external_config.toDict():
                        logging.info(f"{repo} already matches {external_config.configFile}; skipping.")
                        continue
                    logging.info(f"Overwriting {repo} from {external_config.configFile}.")
                else:
                    logging.warning(f"{repo} already exists; skipping.")
                    continue
            self.dir_path.mkdir(parents=True, exist_ok=True)
            external_config_data = external_config.configFile.read()
            old_path.write_bytes(external_config_data)
            new_path.write_bytes(external_config_data)
        return alias_key

    def add_variant(self, legacy_variant: str, future_variant: str, *, force: bool = False) -> None:
        """Add a variant pair to this change.

        Parameters
        ----------
        legacy_variant
            Legacy member of the variant pair.
        future_varian
            Future member of the variant pair.
        force
            Accept a variant whose registry DB URL differs from the primary's.
        """
        all_repos = set(self.iter_all_repos())
        new_pair = RepoPair(legacy_repo=legacy_variant, future_repo=future_variant)
        if conflicts := (all_repos & set(new_pair.iter_all_repos())):
            raise ValueError(f"Variant(s) {conflicts} are already included as standalone repos.")
        alias_key: AliasKey | None = None
        for base, variant in [(self.legacy_repo, legacy_variant), (self.future_repo, future_variant)]:
            base_config = self.config(base, "old")
            variant_config = ButlerConfig(variant)
            base_overrides = self.extract_overrides(base_config)
            variant_overrides = self.extract_overrides(variant_config)
            if base_overrides != variant_overrides:
                raise ValueError(f"Pre-migration overrides for {variant} do not match {base}.")
            self.check_datastore_config(base, variant, base_config, variant_config)
            variant_key = AliasKey.from_config(variant_config)
            if alias_key is None:
                alias_key = AliasKey.from_config(base_config)
            mismatch = f"{base} stored config {alias_key}, {variant} external config {variant_key}."
            if alias_key.namespace != variant_key.namespace:
                raise ValueError(f"{base} and {variant} use different database namespaces: {mismatch}")
            if alias_key.url != variant_key.url:
                if not force:
                    raise ValueError(f"{base} and {variant} are not aliases of each other: {mismatch}")
                logging.warning(
                    f"--force accepts {variant} naming its database under another URL: {mismatch}"
                )
            variant_config_data = variant_config.configFile.read()
            self.file_path(variant, "old").write_bytes(variant_config_data)
            self.file_path(variant, "new").write_bytes(variant_config_data)
        self.variants.append(new_pair)

    @staticmethod
    def live_subtractions(
        butler: Butler, subtracted: dict[str, DatasetTypeOverride]
    ) -> dict[str, DatasetTypeOverride]:
        """Filter subtracted override pairs down to the ones that actually
        have dataset type registrations.
        """
        live: dict[str, DatasetTypeOverride] = {}
        for base_name, override in subtracted.items():
            rows = {
                name: get_dataset_type_or_none(butler, name)
                for name in (base_name, f"legacy_{base_name}", f"future_{base_name}")
            }
            vestigial = not any(rows.values()) or (
                override.db_is_future and rows[base_name] is not None and rows[f"legacy_{base_name}"] is None
            )
            if vestigial:
                logging.info(f"Dropping unnecessary override pair for {base_name}.")
            else:
                live[base_name] = override
        return live

    def analyze(self) -> None:
        """Analyze the configuration files in this migration, checking them
        for consistency and populating the list of dataset types that will be
        changed.
        """
        legacy_old_overrides = self.extract_overrides(self.config(self.legacy_repo, "old"))
        legacy_new_overrides = self.extract_overrides(self.config(self.legacy_repo, "new"))
        legacy_additions, legacy_subtractions = self.diff_old_new(
            legacy_old_overrides.pairs, legacy_new_overrides.pairs, self.legacy_repo
        )
        future_old_overrides = self.extract_overrides(self.config(self.future_repo, "old"))
        future_new_overrides = self.extract_overrides(self.config(self.future_repo, "new"))
        future_additions, future_subtractions = self.diff_old_new(
            future_old_overrides.pairs, future_new_overrides.pairs, self.future_repo
        )
        if legacy_subtractions:
            raise ValueError(
                f"Unexpected legacy subtractions in {self.legacy_repo}: {set(legacy_subtractions.keys())}."
            )
        self.check_variant_overrides(legacy_old_overrides, future_old_overrides, "old")
        self.check_variant_datastores("old")
        self.check_variant_overrides(legacy_new_overrides, future_new_overrides, "new")
        self.check_variant_datastores("new")
        updated_dataset_types: dict[str, DatasetTypeMigration] = {}
        with (
            Butler.from_config(self.legacy_repo) as legacy_butler,
            Butler.from_config(self.future_repo) as future_butler,
        ):
            # Drop (for now) any future subtraction overrides that don't
            # actually have dataset registrations.
            future_subtractions = self.live_subtractions(legacy_butler, future_subtractions)
            # future_additions are config overrides in the future repo only
            # that don't affect the (shared) DB definition.
            for base_name, override in future_additions.items():
                if override.db_is_future:
                    raise ValueError(
                        f"Configs suggest that {base_name} already uses the "
                        f"future definition in the {self.legacy_repo} DB."
                    )
                legacy_dataset_type = legacy_butler.get_dataset_type(base_name)
                future_storage_class, is_registered = FutureStorageClasses().get(legacy_butler, base_name)
                updated_dataset_types[base_name] = DatasetTypeMigration(
                    legacy_storage_class=legacy_dataset_type.storageClass_name,
                    future_storage_class=future_storage_class,
                    is_registered=is_registered,
                    is_db_migration=False,
                )
            # legacy_additions are config overrides in the legacy repo that
            # go along with a migration of the (shared) DB definition.  They
            # may or may not go along with a future_subtraction (i.e. we may
            # or may not skip the interim future-config-override-only stage).
            for base_name, override in legacy_additions.items():
                if base_name in future_old_overrides.pairs:
                    try:
                        paired_override = future_subtractions.pop(base_name)
                    except KeyError:
                        raise ValueError(
                            f"{base_name} is being DB-migrated but its override in "
                            f"{self.future_repo} is not being removed."
                        ) from None
                    if paired_override.db_is_future == override.db_is_future:
                        raise ValueError(
                            f"The entry for {base_name} removed from {self.future_repo} "
                            f"does not invert the entry added to {self.legacy_repo}."
                        )
                if not override.db_is_future:
                    raise ValueError(
                        f"{base_name} configurations act in the wrong direction for "
                        f"a {self.legacy_repo} DB migration."
                    )
                legacy_dataset_type = legacy_butler.get_dataset_type(base_name)
                future_storage_class, is_registered = FutureStorageClasses().get(legacy_butler, base_name)
                updated_dataset_types[base_name] = DatasetTypeMigration(
                    legacy_storage_class=legacy_dataset_type.storageClass_name,
                    future_storage_class=future_storage_class,
                    is_registered=is_registered,
                    is_db_migration=True,
                )
            if future_subtractions:
                raise ValueError(
                    f"Removals {set(future_subtractions.keys())} from {self.future_repo} are not migrated."
                )
            self.dataset_types = updated_dataset_types
            self.check_formatter_coverage(
                legacy_butler,
                future_butler,
                {self.legacy_repo: legacy_old_overrides, self.future_repo: future_old_overrides},
                {self.legacy_repo: legacy_new_overrides, self.future_repo: future_new_overrides},
            )

    def check_formatter_coverage(
        self,
        legacy_butler: Butler,
        future_butler: Butler,
        current_overrides: Mapping[str, ConfigOverrides],
        target_overrides: Mapping[str, ConfigOverrides],
    ) -> None:
        """Check formatter (compression) configuration consistency across the
        primary members for every dataset type named in their ``formatters``
        sections (see `check_formatter_configs`).

        Raises the first inconsistency found, carrying the rest as notes: the
        target configs are edited by hand, so one run should show every one.
        """
        errors = check_formatter_configs(
            self.legacy_repo,
            self.future_repo,
            legacy_butler,
            future_butler,
            current_overrides,
            target_overrides,
            {
                repo: RepoPair.formatter_entries(self.config(repo, "new"))
                for repo in (self.legacy_repo, self.future_repo)
            },
            self.dataset_types,
        )
        if not errors:
            return
        error = ValueError(errors[0])
        for message in errors[1:]:
            error.add_note(message)
        raise error

    def check_drift(self, kind: ConfigKind) -> None:
        """Raise if any group member's external config differs from its
        stored copy.
        """
        for repo in self.iter_all_repos():
            if self.config(repo, kind).toDict() != ButlerConfig(repo).toDict():
                raise ValueError(f"External config for {repo} does not match the stored {kind} config.")

    def _register_future_types(self) -> None:
        """Register the future dataset types that are only in
        storage-classes.json, config-only entries as well as database
        migrations; idempotent, so registrations left by a failed apply are
        harmless.
        """
        if all(dataset_type.is_registered for dataset_type in self.dataset_types.values()):
            return
        with Butler.from_config(self.legacy_repo, writeable=True) as butler:
            for base_name, dataset_type in self.dataset_types.items():
                if not dataset_type.is_registered:
                    legacy_dataset_type = butler.get_dataset_type(base_name)
                    butler.registry.registerDatasetType(
                        DatasetType(
                            name=f"future_{base_name}",
                            storageClass=dataset_type.future_storage_class,
                            dimensions=legacy_dataset_type.dimensions,
                            universe=butler.dimensions,
                            isCalibration=legacy_dataset_type.isCalibration(),
                        )
                    )

    def apply(self) -> None:
        """Rename dataset types in the database and rewrite the external
        configs from the stored *-new files; all checks must have passed.
        """
        self._rename_and_copy(forward=True)

    def undo(self) -> None:
        """Reverse apply(): rename the dataset types back and restore the
        external configs from the stored *-old files.
        """
        self._rename_and_copy(forward=False)

    def _rename_and_copy(self, *, forward: bool) -> None:
        stored_kind = "new" if forward else "old"
        restore_kind = "old" if forward else "new"
        with Butler.from_config(self.legacy_repo, writeable=True) as butler:
            db = butler.registry._registry._db
            table = f"{db.namespace}.dataset_type" if db.namespace else "dataset_type"
            copied: list[str] = []
            try:
                with db._engine.begin() as connection:
                    for base_name, dataset_type in self.dataset_types.items():
                        if not dataset_type.is_db_migration:
                            continue
                        if forward:
                            self._rename(connection, table, base_name, f"legacy_{base_name}")
                            self._rename(connection, table, f"future_{base_name}", base_name)
                        else:
                            self._rename(connection, table, base_name, f"future_{base_name}")
                            self._rename(connection, table, f"legacy_{base_name}", base_name)
                    for repo in self.iter_all_repos():
                        # Track before copying: a copy that fails mid-write
                        # (disk full, flaky mount) still gets restored.
                        copied.append(repo)
                        self._copy_config(self.config(repo, stored_kind), ButlerConfig(repo))
            except BaseException:
                # The file copies are not part of the database transaction;
                # put them back before the transaction rolls back alone.
                # A restore that itself fails is reported, not raised, so
                # it cannot mask the original error — and remote storage
                # client errors are not OSError.
                for repo in copied:
                    try:
                        self._copy_config(self.config(repo, restore_kind), ButlerConfig(repo), restoring=True)
                    except Exception as err:
                        logging.exception(f"Failed to restore {repo}: {err}")
                raise

    @staticmethod
    def _rename(connection: sqlalchemy.Connection, table: str, old_name: str, new_name: str) -> None:
        result = connection.execute(
            sqlalchemy.text(f"UPDATE {table} SET name=:new WHERE name=:old"),
            {"new": new_name, "old": old_name},
        )
        if result.rowcount != 1:
            raise ValueError(f"Rename of {old_name} to {new_name} in {table} matched {result.rowcount} rows.")

    @staticmethod
    def _copy_config(source: ButlerConfig, destination: ButlerConfig, *, restoring: bool = False) -> None:
        # Writing instead of mv/cp preserves the destination's access bits.
        destination.configFile.write(source.configFile.read())
        verb = "Restored" if restoring else "Rewrote"
        logging.info(f"{verb} {destination.configFile} from {source.configFile}")

    def check_variant_overrides(
        self,
        legacy_base: ConfigOverrides,
        future_base: ConfigOverrides,
        kind: ConfigKind,
    ) -> None:
        """Check that the dataset type overrides in a variant repo pair match
        those in the main pair.

        The comparison covers the whole override set, so a variant must mirror
        ad-hoc renames of unmigrated rows as well as the migration's pairs.

        Parameters
        ----------
        legacy_base
            Dataset type overrides extracted from the legacy repo's config.
        future_base
            Dataset type overrides extracted from the future repo's config.
        kind
            Whether this is a check of the "old" config file or the "new" one.
        """
        for variant in self.variants:
            if legacy_base != self.extract_overrides(self.config(variant.legacy_repo, kind)):
                raise ValueError(f"Mismatch between {self.legacy_repo} and {variant.legacy_repo} ({kind}).")
            if future_base != self.extract_overrides(self.config(variant.future_repo, kind)):
                raise ValueError(f"Mismatch between {self.future_repo} and {variant.future_repo} ({kind}).")

    def check_variant_datastores(self, kind: ConfigKind) -> None:
        """Check that each variant's datastore configuration contains every
        datastore section of its same-side primary byte-identically.

        A variant adds configuration (a chained datastore); it does not
        diverge from the primary's file-datastore settings, which the
        shared datastore invariant makes group-wide configuration.
        """
        for variant in self.variants:
            for base, other in (
                (self.legacy_repo, variant.legacy_repo),
                (self.future_repo, variant.future_repo),
            ):
                self.check_datastore_config(base, other, self.config(base, kind), self.config(other, kind))

    @staticmethod
    def check_datastore_config(
        base: str, variant: str, base_config: ButlerConfig, variant_config: ButlerConfig
    ) -> None:
        """Raise if any datastore section of the primary is absent
        byte-identically from the variant; sections only the variant has
        (chained additions) are fine.
        """
        variant_sections = RepoPair.datastore_sections(variant_config)
        for base_section in RepoPair.datastore_sections(base_config):
            if base_section not in variant_sections:
                raise ValueError(
                    f"Datastore section {base_section.get('cls')} of {variant} "
                    f"does not match the one in {base}."
                )

    def diff_old_new(
        self, old: dict[str, DatasetTypeOverride], new: dict[str, DatasetTypeOverride], config_name: str
    ) -> tuple[dict[str, DatasetTypeOverride], dict[str, DatasetTypeOverride]]:
        """Diff the "old" dataset type overrides for a repo against the "new"
        ones to extract the changes.

        Parameters
        ----------
        old
            Dataset type config overrides extracted from the old repository
            configuration.
        new
            Dataset type config overrides extracted fromteh new repository
            configuration.

        Returns
        -------
        `dict`
            Dataset type config overrides added to the "new" repository.
        `dict`
            DAtaset type config overrides removed from the "old" repository.
        """
        additions = new.keys() - old.keys()
        subtractions = old.keys() - new.keys()
        for base_name in old.keys() & new.keys():
            old_override = old[base_name]
            new_override = new[base_name]
            if old_override != new_override:
                raise ValueError(f"{base_name} has changed in an unexpected way in {config_name}.")
        return {k: new[k] for k in additions}, {k: old[k] for k in subtractions}

    def write(self) -> None:
        """Write the change state to the JSON file."""
        (self.dir_path / "migration.json").write_text(self.model_dump_json(indent=2))

    @classmethod
    def read(cls, path: Path) -> RepoMigration:
        """Read the change state from the JSON file."""
        return cls.model_validate_json(path.read_text())

    @classmethod
    def read_many(cls, ticket: str, repos: Iterable[str] | None = None) -> list[RepoMigration]:
        """Load repository changes by looking for their JSON files in a ticket
        directory.
        """
        if repos is not None:
            return [cls.read(cls.root(ticket) / repo / "migration.json") for repo in repos]
        else:
            return [
                cls.read(json_path)
                for dir_path in cls.root(ticket).iterdir()
                if (json_path := dir_path / "migration.json").exists()
            ]


def check_formatter_configs(
    legacy_repo: str,
    future_repo: str,
    legacy_butler: Butler,
    future_butler: Butler,
    current_overrides: Mapping[str, ConfigOverrides],
    target_overrides: Mapping[str, ConfigOverrides],
    target_entries: Mapping[str, dict[str, Any]],
    dataset_types: Mapping[str, DatasetTypeMigration],
) -> list[str]:
    """Check formatter (compression) configuration consistency across a
    legacy/future pair for every dataset type named in their ``formatters``
    sections.

    Base-name entries must be configured on both sides or neither, and
    entries naming the same definition through different members must be
    identical.  Absent ``legacy_``/``future_``-prefixed entries where they
    would cover a member's cutover writes are warnings, not rejections.

    The ``*_overrides`` maps take a repo to the overrides that member's config
    declares: ``current_overrides`` is what the members run on now, and
    ``target_overrides`` what they will run on.  ``target_entries`` gives the
    formatter entries of the configs those writes go through, so only the
    target is checked for consistency.  A live pair has one config per member,
    which serves as both current and target.

    Returns one message per problem (empty when consistent).
    """
    errors: list[str] = []
    bases = {
        key.removeprefix("legacy_").removeprefix("future_") for ep in target_entries.values() for key in ep
    }
    # An ad-hoc rename of an unmigrated row makes a name's definition
    # unknowable here.  On `main`, rows `source` and `source2` hold the older
    # and newer definitions and the future members rename them to `source_v1`
    # and `source`, so visible `source` is the older row in the legacy member
    # and the newer one in the future member.  Nothing here can say which
    # definition a formatter entry keyed `source` covers, so the names an
    # ad-hoc rename touches are skipped.
    renamed_names = {
        name
        for config in (*current_overrides.values(), *target_overrides.values())
        for name in (*config.foreign, *config.foreign.values())
    }
    for base_name in sorted(bases):
        if base_name in renamed_names:
            continue
        errors.extend(
            _check_base_compression(
                base_name,
                legacy_repo,
                future_repo,
                legacy_butler,
                future_butler,
                current_overrides,
                target_overrides,
                target_entries,
                dataset_types,
            )
        )
    return errors


def _check_base_compression(
    base_name: str,
    legacy_repo: str,
    future_repo: str,
    legacy_butler: Butler,
    future_butler: Butler,
    current_overrides: Mapping[str, ConfigOverrides],
    target_overrides: Mapping[str, ConfigOverrides],
    target_entries: Mapping[str, dict[str, Any]],
    dataset_types: Mapping[str, DatasetTypeMigration],
) -> list[str]:
    """One base name of `check_formatter_configs`; see that function."""
    errors: list[str] = []
    legacy_prefixed = f"legacy_{base_name}"
    future_prefixed = f"future_{base_name}"
    clients = {legacy_repo: legacy_butler, future_repo: future_butler}

    probed = {
        (repo, name): get_dataset_type_or_none(clients[repo], name)
        for repo in clients
        for name in (base_name, legacy_prefixed, future_prefixed)
    }
    if not any(probed.values()):
        # No row answers any of these names, so no client can name this base
        # here and none of its formatter entries can be selected at write time
        # — the same absence the vestigial-override rule establishes for
        # override pairs.
        return errors
    # Presence parity needs no definition knowledge; the rest does.
    if (base_name in target_entries[legacy_repo]) != (base_name in target_entries[future_repo]):
        errors.append(
            f"Formatter entry for {base_name} configured on only one side of {legacy_repo} and {future_repo}."
        )
        return errors
    try:
        future_storage_class = FutureStorageClasses().get(legacy_butler, base_name)[0]
    except MissingDatasetTypeError:
        return errors  # future class unknown: definitions indistinguishable
    legacy_exists = any(
        dt is not None and dt.storageClass_name != future_storage_class for dt in probed.values()
    )
    future_exists = any(
        dt is not None and dt.storageClass_name == future_storage_class for dt in probed.values()
    )
    pair = current_overrides[legacy_repo].pairs.get(base_name)
    if pair is not None and pair.db_is_future:
        # A legacy-side pair is installed only by a completed migration.
        on_future = True
    elif any(probed[(repo, legacy_prefixed)] is not None for repo in clients):
        on_future = True
    elif any(probed[(repo, future_prefixed)] is not None for repo in clients):
        on_future = False
    elif not legacy_exists:
        on_future = True  # F state: the base name carries the future definition
    elif not future_exists:
        on_future = False  # L state
    else:
        on_future = True  # both definitions, prefixed rows renamed away: completed
    migration = dataset_types.get(base_name)
    # Post-migration facts: apply registers future_X for any analyzed
    # base and renames X to legacy_X for database migrations.  A
    # prefixed row name is only live while the database holds the
    # definition under it: pre-arrangement for future_X.
    on_future_new = on_future or (migration is not None and migration.is_db_migration)
    future_row_new = (future_exists or migration is not None) and not on_future_new
    legacy_row_new = legacy_exists or (migration is not None and migration.is_db_migration)

    def side_of_key(repo: str, key: str) -> PairSide | None:
        """Which side of the migrated pair ``key`` names in this member's target
        config.

        A side is named by the definition it holds: ``"legacy"`` is the one the
        base name carried before migration (``ExposureF`` for ``deep_coadd``),
        ``"future"`` the one registered from ``storage-classes.json``
        (``CellCoadd`` for ``deep_coadd``).  A key is a spelling a client can
        ask for: the base name or one of its prefixed forms.  A legacy member
        answers ``deep_coadd`` with ``"legacy"`` and ``future_deep_coadd`` with
        ``"future"`` both before it gains a pair and after it gains a
        legacy-facing one; there ``legacy_deep_coadd`` answers `None`, because
        that row is what the pair serves under ``deep_coadd`` and the spelling
        reaches no client.

        A key whose row is missing once the target is in place also answers
        `None`: a formatter entry beside it can never be selected at write
        time.
        """
        member_pair = target_overrides[repo].pairs.get(base_name)
        row = key
        if member_pair is not None:
            if member_pair.db_is_future:
                row = {base_name: legacy_prefixed, future_prefixed: base_name, legacy_prefixed: None}[key]
                if row is None:
                    return None  # hidden by the pair
            else:
                row = {base_name: future_prefixed, legacy_prefixed: base_name, future_prefixed: None}[key]
                if row is None:
                    return None
        if row == base_name:
            return "future" if on_future_new else "legacy"
        if row == legacy_prefixed:
            return "legacy" if legacy_row_new else None
        if row == future_prefixed:
            return "future" if future_row_new else None
        return None

    entries_by_side: dict[PairSide, list[tuple[str, str]]] = {}
    for repo in clients:
        for key in (base_name, legacy_prefixed, future_prefixed):
            if key in target_entries[repo]:
                if (side := side_of_key(repo, key)) is not None:
                    entries_by_side.setdefault(side, []).append((repo, key))
    for side, sites in entries_by_side.items():
        first_repo, first_key = sites[0]
        for repo, key in sites[1:]:
            if target_entries[repo][key] != target_entries[first_repo][first_key]:
                errors.append(
                    f"Formatter entries for the {side} definition of "
                    f"{base_name} disagree between {first_repo}:{first_key} and {repo}:{key}."
                )
    if any(base_name in target_entries[repo] for repo in clients):
        for repo, prefixed, expected in (
            (future_repo, legacy_prefixed, "legacy"),
            (legacy_repo, future_prefixed, "future"),
        ):
            if side_of_key(repo, prefixed) == expected and prefixed not in target_entries[repo]:
                logging.warning(
                    f"{repo} has no {prefixed} formatter entry to cover its cutover writes of {base_name}."
                )
    return errors


def check_distinct_alias_keys(migrations: Sequence[RepoMigration]) -> None:
    """Ensure no two selected migrations migrate the same repository.

    Intra-group identity is not re-checked here: ``init`` and ``add-variant``
    enforce it when the migration is written (``add-variant --force`` being
    the sanctioned override), and ``apply``'s drift check runs first, so a
    member whose config moved since then fails there with a clearer message.
    """
    owners: dict[AliasKey, str] = {}
    for migration in migrations:
        key = AliasKey.from_config(ButlerConfig(migration.legacy_repo))
        if (prior := owners.get(key)) is not None:
            raise ValueError(
                f"{migration.legacy_repo} and {prior} are both migrations of the same repository ({key})."
            )
        owners[key] = migration.legacy_repo


def check_future_storage_classes(migrations: Sequence[RepoMigration]) -> None:
    """Ensure the selected migrations agree on the future storage class for
    every base name they share.
    """
    by_base: dict[str, dict[str, list[str]]] = {}
    for migration in migrations:
        for base_name, dataset_type in migration.dataset_types.items():
            by_base.setdefault(base_name, {}).setdefault(dataset_type.future_storage_class, []).append(
                migration.legacy_repo
            )
    for base_name, classes in sorted(by_base.items()):
        if len(classes) > 1:
            detail = " vs ".join(
                f"{storage_class} ({', '.join(sorted(repos))})"
                for storage_class, repos in sorted(classes.items())
            )
            raise ValueError(f"Future storage class for {base_name} conflicts across repos: {detail}.")


def get_dataset_type_or_report(
    butler: Butler, name: str, failures: list[str], repo: str
) -> DatasetType | None:
    dataset_type = get_dataset_type_or_none(butler, name)
    if dataset_type is None:
        failures.append(f"{name} is not registered in {repo}.")
    return dataset_type


def compare_dataset_type_attrs(
    a: DatasetType | None, b: DatasetType | None, attr: str, failures: list[str], msg: str
) -> None:
    if a is not None and b is not None and getattr(a, attr) != getattr(b, attr):
        failures.append(f"{msg}: {getattr(a, attr)} != {getattr(b, attr)}.")


def check_repos(legacy_repo: str, future_repo: str) -> list[str]:
    """Check a live legacy/future repository pair for a consistent dataset
    type overrides.

    Migration-managed pairs are probed in both members; the ad-hoc renames of
    unmigrated rows are checked only in the member that declares them.

    Parameters
    ----------
    legacy_repo
        Location of the legacy member of the pair.
    future_repo
        Location of the future member of the pair.

    Returns
    -------
    `list` of `str`
        Human-readable description of each inconsistency found; empty when
        the pair is consistent.
    """
    if legacy_repo == future_repo:
        return [f"Invalid self-check: {legacy_repo}."]
    failures: list[str] = []
    configs: dict[str, ButlerConfig] = {}
    for repo in (legacy_repo, future_repo):
        try:
            configs[repo] = ButlerConfig(repo)
        except Exception as err:
            failures.append(f"{repo} could not be read: {err}.")
    if failures:
        return failures
    extractions = {repo: RepoPair.extract_overrides(configs[repo]) for repo in configs}
    if not any(config.pairs or config.foreign for config in extractions.values()):
        return [f"Neither {legacy_repo} nor {future_repo} declares dataset type overrides."]
    base_names = extractions[legacy_repo].pairs.keys() | extractions[future_repo].pairs.keys()
    with (
        Butler.from_config(legacy_repo, without_datastore=True) as legacy_butler,
        Butler.from_config(future_repo, without_datastore=True) as future_butler,
    ):
        for base_name in sorted(base_names):
            legacy_repo_legacy_type = get_dataset_type_or_report(
                legacy_butler, base_name, failures, legacy_repo
            )
            legacy_repo_future_type = get_dataset_type_or_report(
                legacy_butler, f"future_{base_name}", failures, legacy_repo
            )
            future_repo_legacy_type = get_dataset_type_or_report(
                future_butler, f"legacy_{base_name}", failures, future_repo
            )
            future_repo_future_type = get_dataset_type_or_report(
                future_butler, base_name, failures, future_repo
            )
            compare_dataset_type_attrs(
                legacy_repo_legacy_type,
                legacy_repo_future_type,
                "dimensions",
                failures,
                f"Inconsistent dimensions for variants of {base_name} in {legacy_repo}",
            )
            compare_dataset_type_attrs(
                future_repo_legacy_type,
                future_repo_future_type,
                "dimensions",
                failures,
                f"Inconsistent dimensions for variants of {base_name} in {future_repo}",
            )
            compare_dataset_type_attrs(
                legacy_repo_legacy_type,
                future_repo_legacy_type,
                "storageClass_name",
                failures,
                f"Inconsistent storage class for legacy variant of {base_name}",
            )
            compare_dataset_type_attrs(
                legacy_repo_future_type,
                future_repo_future_type,
                "storageClass_name",
                failures,
                f"Inconsistent storage class for future variant of {base_name}",
            )
        # Ad-hoc renames take no part in the comparisons above, but a rename
        # whose row is gone would leave its member with nothing to resolve
        # under that name.
        for repo, butler in ((legacy_repo, legacy_butler), (future_repo, future_butler)):
            for visible_name in sorted(extractions[repo].foreign.values()):
                get_dataset_type_or_report(butler, visible_name, failures, repo)
        entries = {repo: RepoPair.formatter_entries(configs[repo]) for repo in configs}
        # A live pair has one config per member, which is both what it runs on
        # now and what it would run on next.
        failures.extend(
            check_formatter_configs(
                legacy_repo,
                future_repo,
                legacy_butler,
                future_butler,
                extractions,
                extractions,
                entries,
                {},
            )
        )
    return failures


def format_summary(migrations: Sequence[RepoMigration]) -> str:
    """Format a summary of the changes across a set of analyzed migrations,
    grouping identical changes so that changes common to all repos print
    only once.
    """
    groups: dict[tuple[bool, str, str, str, bool], dict[str, list[str]]] = {}
    for migration in migrations:
        variants = sorted(variant.legacy_repo for variant in migration.variants)
        for base_name, dataset_type in migration.dataset_types.items():
            key = (
                dataset_type.is_db_migration,
                base_name,
                dataset_type.legacy_storage_class,
                dataset_type.future_storage_class,
                dataset_type.is_registered,
            )
            groups.setdefault(key, {})[migration.legacy_repo] = variants
    if not groups:
        return "No dataset type changes."
    width = max(len(key[1]) for key in groups)
    lines: list[str] = []
    for is_db_migration, header in ((True, "Database migrations"), (False, "Config-only overrides")):
        entries = sorted((key, repo_lines) for key, repo_lines in groups.items() if key[0] == is_db_migration)
        lines.append(f"{header} ({len(entries)}):")
        for (_, base_name, _, _, is_registered), repo_lines in entries:
            status = "registered" if is_registered else "unregistered"
            lines.append(f"  {base_name:{width}}  ({status})")
            for prime, variants in sorted(repo_lines.items()):
                if variants:
                    lines.append(f"    {prime}  (+ {', '.join(variants)})")
                else:
                    lines.append(f"    {prime}")
    return "\n".join(lines)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output.")
def cli(verbose: bool) -> None:
    """Future images migration tool."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )


@cli.command()
@click.argument("ticket", type=str, help="Ticket used to name the migration.")
@click.argument("repos", type=str, nargs=-1, help="Repositories to apply the migration to.")
@click.option(
    "--reset",
    is_flag=True,
    help="Reset existing config files to their external state.",
)
def init(ticket: str, repos: Iterable[str], reset: bool = False) -> None:
    """Make a new migration for the given ticket, seeding the 'old' configs
    for the given repositories.
    """
    alias_keys: set[AliasKey] = set()
    for repo in repos:
        json_path = RepoMigration.root(ticket) / repo / "migration.json"
        if json_path.exists():
            # Round-trip the stored model so variants survive a reset.
            migration = RepoMigration.read(json_path)
            # A reset always starts analysis over.
            migration.dataset_types = {}
        else:
            migration = RepoMigration(ticket=ticket, legacy_repo=repo, future_repo=repo + "_future")
        new_alias_key = migration.init_configs(reset=reset)
        if new_alias_key in alias_keys:
            raise ValueError(f"{repo} is an alias, not an independent repository.")
        alias_keys.add(new_alias_key)
        if reset or not (migration.dir_path / "migration.json").exists():
            migration.write()


@cli.command()
@click.argument("ticket", type=str, help="Ticket used to name the migration.")
@click.argument("legacy_base", type=str, help="Legacy base repo that already has a variant.")
@click.argument("legacy_variant", type=str, help="Legacy variant repo to associate.")
@click.argument("future_variant", type=str, help="Future variant repo to associate.")
@click.option(
    "--force",
    is_flag=True,
    help="Accept a variant naming the shared database under another URL spelling.",
)
def add_variant(
    ticket: str, legacy_base: str, legacy_variant: str, future_variant: str, *, force: bool = False
) -> None:
    """Mark a pair of legacy/future butler repositories as a variant of one
    already included in this migration.
    """
    migration = RepoMigration.read(RepoMigration.root(ticket) / legacy_base / "migration.json")
    migration.add_variant(legacy_variant, future_variant, force=force)
    migration.write()


@cli.command()
@click.argument("ticket", type=str, help="Ticket used to name the migration.")
@click.option(
    "--repo",
    type=str,
    multiple=True,
    help="Repositories to apply the overrides (default is all repos for this ticket).",
)
def analyze(ticket: str, *, repo: Iterable[str] = ()) -> None:
    """Analyze the new and old config files for a migration ticket and
    identify migrations that need to be performed for them.
    """
    migrations = RepoMigration.read_many(ticket, repo or None)
    for migration in migrations:
        migration.analyze()
        migration.write()
    check_future_storage_classes(migrations)
    click.echo(format_summary(migrations))


@cli.command()
@click.argument("ticket", type=str, help="Ticket used to name the migration.")
@click.option(
    "--repo",
    type=str,
    multiple=True,
    help="Repositories to apply the migration to (default is all repos for this ticket).",
)
@click.option("--undo", is_flag=True, help="Reverse a migration that was just applied.")
def apply(ticket: str, *, repo: Iterable[str] = (), undo: bool = False) -> None:
    """Apply a migration: rename dataset types in each database and
    rewrite the external configs from the stored files.
    """
    migrations = RepoMigration.read_many(ticket, repo or None)
    if not undo:
        for migration in migrations:
            migration.check_drift("old")
        check_distinct_alias_keys(migrations)
        for migration in migrations:
            # Check-only: apply must leave migration.json untouched.
            migration.analyze()
        check_future_storage_classes(migrations)
        for migration in migrations:
            migration._register_future_types()
        for migration in migrations:
            migration.apply()
    else:
        # dataset_types come from migration.json: apply analyzes in-memory
        # only, so a migration that was never analyzed has nothing to undo.
        for migration in migrations:
            if not migration.dataset_types:
                raise ValueError(f"Migration for {migration.legacy_repo} has not been analyzed.")
            migration.check_drift("new")
        for migration in migrations:
            migration.undo()


@cli.command()
@click.argument("legacy_repo", type=str, help="Legacy member of the repository pair.")
@click.argument("future_repo", type=str, help="Future member of the repository pair.")
def check(legacy_repo: str, future_repo: str) -> None:
    """Check a live legacy/future repository pair, independent of any
    migration ticket, for a consistent dataset type overrides.
    """
    failures = check_repos(legacy_repo, future_repo)
    for failure in failures:
        click.echo(failure, err=True)
    if failures:
        raise SystemExit(1)
    click.echo(f"{legacy_repo} and {future_repo} are consistent.")


if __name__ == "__main__":
    cli()
