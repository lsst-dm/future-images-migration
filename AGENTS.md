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

## Ad-hoc renames of unmigrated rows

A config may also rename rows this tool does not migrate. Rows stranded by
earlier generations of migrations carry names of their own: `main`'s database
holds the older and newer definitions of `source` as rows `source` and
`source2`, and its future members serve them as `source_v1` and `source`. Such
a rename fits neither prefix pattern, so `extract_overrides` collects it in the
config's _foreign_ map (database row name to visible name) and everything
migration-facing ignores it: never diffed into additions or subtractions, never
renamed in the database, absent from `migration.json` and the summary.

Foreign renames are still validated. `extract_overrides` rejects one whose
names meet a migrated pair's names (a mistyped pair rather than a one-off), one
using a `legacy_` or `future_` prefix, and one that renames a second row onto a
visible name already taken. `analyze`'s mirror check and `add-variant`'s
baseline check compare the whole override set, so a variant must carry its
primary's foreign renames: a variant serving `source` as another row serves
different data than its primary. `check` requires each foreign rename to
resolve in the member declaring it — a row deleted or misspelled leaves that
member serving nothing under the name.

Nothing is known about the definitions behind a foreign rename: the members of
a group can serve different rows under one name (on `main`, visible `source` is
the older definition in the legacy member and the newer one in the future
member), so the compression checks skip any name a foreign rename touches.

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
a `SasquatchDatastore` on top of it. The shared part is group configuration:
a variant's datastore config must contain every datastore section of its
same-side primary byte-identically — the variant chains on top, it does not
diverge. `add-variant` enforces this at entry; `analyze` re-checks both
stored stages.

`init` and `add-variant` test group membership by raw `(registry.db,
registry.namespace)` equality. Proxy and symlink indirection means two
different URL spellings can address one database: raw equality can prove two
configs _differ_, never that they _match_. Do not build logic that assumes
unequal URL strings imply distinct databases, unless the caller can override
that logic in a straightforward, targeted way — `add-variant --force` is that
override, and it relaxes the db URL comparison only (a namespace mismatch is
never excused). `apply` does not re-check intra-group identity: its drift
check runs first and catches any config move since the membership checks.

## Formatter (compression) configuration

`datastore.formatters` entries bind write behavior — compression recipes,
formatter choice — to dataset types. daf-butler resolves these keys
against the dataset type name *visible to the client*: the row name with
the dataset-manager override `rename` applied (the row name never reaches
the client), falling back to a dimensions key and then storage-class name
keys. Chained datastores nest their sections (and `formatters`) under
`datastore.datastores`. Keys are also taken per-section, so a
dataset type absent from the map simply gets the storage-class default
formatter; `default`, `write_recipes`, and `instrument<Name>` sections
carry other things and are not dataset-type keys.

For a migrated base `X` this makes formatter keys stable across the
migration: `X` on the legacy member covers the legacy definition's writes
before and after apply, and `X` on the future member covers the future
definition's writes during interim and after — the config rewrite carries
the renames' effect. Cutover changes *coverage*, not key meaning:
post-apply the legacy member also serves the future definition (under
visible `future_X`), and the future member also serves the legacy
definition (under visible `legacy_X`). A key no client can ask for — its row
is absent, or a pair renames that row away from the spelling — never selects a
formatter.

Requirement: compression configured for a base name must be configured
consistently across the legacy and future sides, and entries naming the
same definition through different members must be identical. `analyze`
enforces both on the `new` (target) configs — never `old`, so a ticket
seeded from an inconsistent live state can still be read, analyzed, and
fixed — and `check` enforces both on live configs. The
`future_X`/`legacy_X` prefixed entries are the nice-to-have that extends
compression to cutover writes: their absence is warned, not rejected,
because base names are what producers use (and they are meaningless
entirely for bases that never had a legacy definition).

Observed convention (not enforced by the tool): entries serving legacy
definitions use `FitsExposureFormatter` with `recipe: lossy16`, entries
serving future definitions use `GenericFormatter` with `recipe: lossy16`.
Which class belongs to an entry follows from the definition it serves,
not from the member it sits in: an F-state base has its (future) entries
on the legacy member too.

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
  stored `old` copies for every group member, no two selected migrations
  name the same database identity, a fresh in-memory analysis, and cross-repo
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
  apply are kept: an extra registered row changes nothing until something uses
  it, and re-registering is idempotent. Undo is wrong once data has been
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
definition must resolve to one storage class. A member's foreign renames take
no part in those comparisons — only their visible names are probed, for
rows.

It is not direction-aware: it probes `(base, future_X)` in the legacy member
and `(legacy_X, base)` in the future member — the name set of a correctly
configured pair in either the interim or the final arrangement. A pre-interim
pair legitimately fails (the future member has no pair and the future
definition is unresolvable).

It also enforces the compression-configuration requirements on the live
pair (same rules as `analyze`, applied to the externals), warning — not
failing — on absent prefixed cutover entries. Bases with no row in the
database are skipped entirely: no client name reaches them, so their formatter
entries can never be selected.

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

- Tests drive the CLI through subprocesses only, never by importing the script.
- `tests/fixtures` is the fixture: two committed butler repository groups, each
  a real repo directory with its own sqlite registry — `dp2_prep` (primary pair
  plus a chained-datastore variant pair) and `v3_skills_unit` (primary pair) —
  and `targets/`, the hand-edited target configs. Configs name the registry and
  the datastore only through `<butlerRoot>`, so copying a group gives a working
  repository wherever it is placed; tests copy one into their own tree and
  regenerate `DAF_BUTLER_REPOSITORY_INDEX` there (index values must be absolute,
  and `ButlerRepoIndex` resolves relative ones against the cwd). Set
  `FUTURE_IMAGES_MIGRATIONS_ROOT` to a per-test migrations tree.
- `tests/fixtures/build_fixture.py` regenerates the committed registries and
  holds the groups' state model (which dataset types each database starts with);
  tests read it instead of restating it, and `test_fixture.py` fails if the
  committed registries drift from it. Committing a registry ties the suite to
  this daf-butler's schema, so re-run the builder after a registry change.
- `tests/conftest.py` drives one copy of the fixture through init → add-variant
  → target edits → analyze → apply and snapshots it after each stage; per-test
  fixtures copy a snapshot rather than rebuilding state.
- Coverage the fixture consciously leaves out: its registries are
  namespace-free, because sqlite cannot carry a `registry.namespace` in this
  daf-butler (`SqliteDatabase` calls that case untested), which leaves
  `apply`'s namespace-qualified SQL branch — the one production postgres takes
  — untested; the registries hold no dataset records, so nothing here shows
  records and datastore files surviving a rename; remote-hosted externals
  (`remote-test://`) are uncovered; as are two selected migrations disagreeing
  on a shared base's future storage class, and `add-variant --force` semantics.
- Rejections are provoked by forging a copy of the fixture — `Tree.edit`
  substitutes text in one of its stored or external configs — rather than by
  adding fixtures for broken states; a forgery asserts the string it replaces is
  present so it cannot silently become a no-op.
- Rejection tests assert return code plus data substrings (repo or dataset
  type names), not tool message wording.
