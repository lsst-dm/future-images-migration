# future-images-migration

State and tooling for migrating butler repositories from legacy to future
dataset type definitions. Each migration ticket gets a directory holding
copies of the butler configs involved plus a `migration.json` describing the
change; `future-images-migrate.py` drives the workflow against live
repositories.

## Repo layout

- `future-images-migrate.py` — the migration CLI (click). The hyphenated name
  makes it unimportable; tests invoke it as a subprocess.
- `storage-classes.json` — the future storage class for each migrated base
  dataset type name, read from the migrations root
  (`FUTURE_IMAGES_MIGRATIONS_ROOT`, default the script's directory).
- `DM-55129/dp2_prep/` — the historical one-off migration: before/after config
  copies and its bespoke `run-migration.py`.
- `<TICKET>/<legacy_repo>/` — per-ticket working dirs (e.g. `DM-56581a/`):
  stored config copies `<repo>.old.yaml` (rollback copies of the externals)
  and `<repo>.new.yaml` (hand-edited targets), plus `migration.json`.

`migration.json` (a serialized `RepoMigration`) is written only by `init`,
`add-variant`, and `analyze`. `apply` never writes it; `apply --undo` reads
its `dataset_types` to know what to reverse.

## Domain model

A migrated dataset type is a _pair of definitions_ sharing one dimensions
set: a legacy definition (old storage classes, e.g. `ExposureF`) and a future
definition (`VisitImage`, `CellCoadd`, `DifferenceImage`, …, per
`storage-classes.json`). The pair lives as two rows in
`<registry namespace>.dataset_type` of the registry database. Exactly one
definition owns the un-prefixed _base name_:

- **pre-arrangement** (`db_is_future=False`): row `X` = legacy definition,
  row `future_X` = future definition.
- **post-arrangement** (`db_is_future=True`): row `legacy_X` = legacy
  definition, row `X` = future definition.

A repository presents a pair through dataset-manager config `overrides`: a
mapping keyed by the database row name whose `rename:` value is the
user-visible name. A member that maps a base declares a complete pair —
one-sided overrides are rejected by `extract_overrides`:

- **future-facing** pair (the future member of a pre-arrangement group):
  `X: {rename: legacy_X}` plus `future_X: {rename: X}` — presents the future
  definition under the base name.
- **legacy-facing** pair (the legacy member of a post-arrangement group):
  `legacy_X: {rename: X}` plus `X: {rename: future_X}` — presents the legacy
  definition under the base name.

A member with no overrides for a base exposes the database names directly,
which is correct for the future member post-arrangement and the legacy
member pre-arrangement.

Renames apply at read time and are registry-wide: a butler client cannot
register or query a hidden row name, so renaming rows requires raw SQL (no
public rename API exists in daf-butler). A rename changes only
`dataset_type.name`; dataset records reference rows by id, so collections,
records, and datastore files survive a rename untouched.

Per base name, these database row sets are all states that occur or have
been agreed as legitimate:

| state | rows                         | where                                                                         |
| ----- | ---------------------------- | ----------------------------------------------------------------------------- |
| L     | `X` = legacy definition only | before the future definition was ever registered                              |
| P0    | `X`, `future_X`              | pre-arrangement                                                               |
| P1    | `legacy_X`, `X`              | post-arrangement (migration done)                                             |
| F     | `X` = future definition only | families that never had a legacy definition (e.g. `visit_image` in `embargo`) |
| N     | no rows                      | type not in this database                                                     |

A config may declare an override pair whose prefixed row is absent from the
database (a _vestigial_ pair, typically from configs copied between repo
families); whether a pair is live or vestigial is decided by the presence of
the prefixed row in that database, not by the config.

## Migration-group invariant: one shared database

Every member of a migration group — legacy, future, and all variants — uses
the same physical registry database (same `registry.db` and
`registry.namespace`). Members are alias configs colocated in the repository
directory (`butler.yaml`, `butler_future.yaml`, `butler+<variant>.yaml`,
`butler_future+<variant>.yaml`), addressed through
`DAF_BUTLER_REPOSITORY_INDEX` aliases. The primary legacy/future pair shares
one file datastore (same `<butlerRoot>` root and records table).

A _variant_ is a member that differs from the primary pair in some
configuration other than the dataset type overrides — the overrides must
match the primary's exactly (enforced by `add-variant`'s baseline check and
`analyze`'s mirror check), while other configuration differs. That other
difference is often, but is not required to be, datastore configuration:
`dp2_prep+sasquatch_dev` keeps the shared primary `FileDatastore` and chains
a `SasquatchDatastore` on top of it.

`init` and `add-variant` test group membership by raw `(registry.db,
registry.namespace)` equality. Proxy and symlink indirection means two
different URL spellings can address one database: raw equality can prove two
configs _differ_, never that they _match_. Do not build logic that assumes
unequal URL strings imply distinct databases, unless the caller can override
that logic in a straightforward, targeted way.

## Lifecycle and the script's guarantees

Per migration ticket: `init` seeds stored `old`/`new` copies from the
externals; a human edits the `new` files; `analyze` diffs old vs new stored
configs and the live database to classify each changed base name; `apply`
executes. Two kinds of change:

- **Config-only** (`is_db_migration=False`): the future member's `new` config
  adds a future-facing pair; the only database effect is registering a
  missing `future_X`, never a rename. This is the _interim_ stage that lets
  the future member resolve the future definition under the base name before
  the database is renamed. Removing a vestigial override pair (see `analyze`)
  is config-only as well, with no database effect at all.
- **Database migration** (`is_db_migration=True`): the legacy member's `new`
  config adds a legacy-facing pair and the future member's pair is removed
  (required only if its old config had one — the interim can be skipped
  entirely when the future member is still override-free).

Every apply first registers each migrated base whose `future_X` row is
missing: storage class from `storage-classes.json` — resolved through the
`StorageClassFactory`, so the class must exist in the environment running
apply — with dimensions copied from the legacy definition. Database
migrations then, in one `engine.begin()`
transaction per database, rename `X → legacy_X` then `future_X → X` (each
rename must match exactly one row) and rewrite each member's external config
from its stored `new` file — config-only applies rewrite configs only. The
file copies are outside the transaction: on failure, `apply` restores every
already-copied external from its stored `old` copy before the transaction
rolls back alone.

Guarantees the script establishes and checks it assumes:

- `apply` mutates nothing unless every check passed (externals match their
  stored `old` copies for every group member, each selected migration owns a
  distinct database identity, a fresh in-memory analysis, and cross-repo
  agreement on future storage classes). A failure in migration #k leaves
  #1..#k-1 committed: apply across databases is not atomic; sequence
  independent groups with `--repo`.
- After a successful apply, every external byte-equals its stored `new`
  file, and each database-migrated base is exactly in the post-arrangement.
- `apply` never writes `migration.json`; re-analysis during apply is
  in-memory only.
- `apply --undo` is valid only immediately after `apply`: it requires
  externals to still match the stored `new` files, restores them from the
  stored `old` files, and runs the reverse renames. Registrations made by
  apply are kept (inert and idempotent). Undo is wrong once data has been
  written through the migrated configuration.

Policy: the `future_X` row is registered _before_ the interim config goes
live, so the future member always resolves. `apply` implements this itself:
its registration pass precedes every config rewrite (config-only applies
included), and registers through the legacy member's client — the one
without overrides for `X`. Hand registration uses the same client. Wild
repositories that skipped registration remain migratable the same way.

`analyze` classifies a subtracted future-side override pair as a
config-only drop (no `DatasetTypeMigration` entry, no rename — `apply`
only rewrites externals) when it guards nothing the future member should
keep: no row under any of `X`, `legacy_X`, or `future_X`, or the pair's
direction is `db_is_future` with `X` resolvable and `legacy_X` not. The
membership check probes `get_dataset_type` through the legacy member's
client; only future-side pairs need classifying, since a legacy member
acquires its pair only from a completed migration and legacy-side
subtractions are rejected outright. Where that prober's own renames hide
`legacy_X` from a probe, the hidden row marks a completed migration —
and the correct post-arrangement config for the future member is no pair
at all, so the probe's one blind spot always points at a droppable pair.

`analyze` rejects: incomplete or inconsistent override pairs; live
legacy-side subtractions; live future-side removals not paired with a
database migration; additions acting in the wrong direction; a future-only
addition implying the database already post-arranges; future storage
classes that disagree across selected migrations; and base names neither
registered as `future_X` nor present in `storage-classes.json`.

## The `check` subcommand

`check LEGACY_REPO FUTURE_REPO` is a read-only (`without_datastore=True`)
verifier of a fully configured live pair, independent of any ticket. For
every base declared by either member's overrides, both definitions must
resolve in both members under that member's visible names; within a member
the two definitions must agree on dimensions, and across members each
definition must resolve to one storage class.

It is not direction-aware: it probes `(base, future_X)` in the legacy member
and `(legacy_X, base)` in the future member — the name set of a correctly
configured pair in either the interim or the final arrangement. A pre-interim
pair legitimately fails (the future member has no pair and the future
definition is unresolvable).

A missing legacy definition on a declared base means the _override_ is the
problem, not the schema: with no legacy registration, the base name serves
the future definition directly and the override pair is unnecessary.

## Historical documents

The specs and plans under `superpowers-docs/DM-56581/` describing the
`check` subcommand document an implementation different from this script's
and assume each production member has its own database — contrary to the
shared-database invariant above. Their tests model a future member on a
separate copied sqlite database, a topology `init` itself would reject.
Treat them as historical context, not ground truth.

## Worked example: the dp2_prep family

The per-ticket directories committed in this repo are its record of each
migration; later tickets will add more. This section maps one such
migration — the dp2_prep family — onto the state model. The production
truth is the externals (S3/postgres, not in this repo); the stored files
are the repo-tracked view as of the last `init`/edit. As of the
`DM-56581a` stored files:

| base                                            | database | notes                                                                                                                                |
| ----------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `visit_image`, `difference_image`, `deep_coadd` | P1       | migrated by the historical one-off; legacy-facing pairs in `butler.yaml` and `butler+sasquatch_dev.yaml`                             |
| `deconvolved_deep_coadd`                        | P0       | interim live: future-facing pair in `dp2_prep_future*.old.yaml`, database rename pending                                             |
| `template_coadd`                                | P0/L     | interim prepared: pair only in `*.new.yaml`, analyzed config-only with `is_registered=False`; whether apply ran is not recorded here |
| remaining `storage-classes.json` names          | —        | listed in `storage-classes.json`, not yet migrated in any recorded ticket                                                            |

## Test conventions

- Tests drive the CLI through subprocesses only (`run_migration`), never by
  importing the script.
- Fixtures materialize the four-member group layout under one directory with
  a shared sqlite registry (`db: sqlite:///<butlerRoot>/…`) resolved through
  a generated `DAF_BUTLER_REPOSITORY_INDEX`, and set
  `FUTURE_IMAGES_MIGRATIONS_ROOT` to a per-test migrations tree. Remote-hosted
  config coverage uses `remote-test://` with an absolute-path local sqlite
  (`<butlerRoot>` in a remote config expands to the remote URI and would
  corrupt a sqlite URL).
- Rejection tests assert return code plus data substrings (repo or dataset
  type names), not tool message wording.
