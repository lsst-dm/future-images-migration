# Tooling for lsst.images storage class migrations

NOTE: code in this package is _heavily_ AI-assisted; YMMV.

The `future-iamges-migrate.py` tool here is designed to be used in three
phases:

- `init` copies configuration from a butler repository (which must already be a
  `_legacy` / `_future` pair) into a new ticket-named migration in this package
  (the expectation is that migrations are committed here for posterity).

- `add-variant` is optionally used to add variant pairs that differ in some
  other way (e.g. a sasquatch datastore), copying over the configs for those,
  too.

- A human edits the the `.new.yaml` variant of each of those configs to add
  `legacy_` / `future_` dataset type rename overrides and
  compression-configuration.

- `analyize` is used to validate those edits, identify whether this is just a
  config override for the `_future` repo or an update to the butler DB that
  makes the legacy repo carry a reverse override instead.

- `apply` registers dataset types, copies the `.new.yaml` configs to into the
  repos, and updates the database. It uses the `.old.yaml` configs to revert
  the change if there's a failure.

In addition, the `check` command can be used to check that a live butler
repository's configuration is valid.

The checks here expect a degree of consistency that might not actually be
necessary (e.g. a read-only repo variant might be expected to have consistent
compression configuration with its parent, even though nobody can `put` to it).
