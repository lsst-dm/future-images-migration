"""Tests for analyze's whole-ticket summary output (format_summary) and the
cross-repo future-storage-class guard that precedes it.

Deliberately LOOSE assertions only (returncode plus substrings/regexes on
stdout): the exact wording, spacing, and column widths are the author's to
tweak, so these pin only the contract-visible behaviors — section
placement, per-section counts, collapse of identical changes across
primaries, registered/unregistered splits at the same future storage class
rendering as separate entries, variant nesting under the prime, the
empty-result line, and the guard rejecting genuine cross-repo future-class
conflicts before any summary is printed.
"""

from __future__ import annotations

import json
import re

import yaml

from conftest import (
    BASE_TYPES,
    FUTURE_REPO,
    FUTURE_VARIANT,
    LEGACY_VARIANT,
    REPO,
    SECOND,
    TICKET,
    canonical_new_state,
    data_text,
    register_dataset_types,
    run_migration,
    stored_config,
)


def header_count(stdout: str, word: str) -> int:
    """The parenthesized count on the section header line containing word."""
    match = re.search(re.escape(word) + r"[^\d(]*\((\d+)\)", stdout)
    assert match, f"no '{word}' header with a count in stdout:\n{stdout}"
    return int(match.group(1))


def entry_line(stdout: str, base_name: str) -> int:
    """Index of the first summary line mentioning base_name."""
    lines = stdout.splitlines()
    at = [i for i, ln in enumerate(lines) if base_name in ln]
    assert len(at) == 1, f"expected exactly one line for {base_name!r}:\n{stdout}"
    return at[0]


def analyze_two_migrations(pair, *, conflict: bool = False):
    """Run ``analyze`` over both migrations of the prepared ticket; with
    ``conflict=True``, register future_visit_image in the second pair's DB
    with a storage class that clashes with the first pair's (registered)
    one.
    """
    if conflict:
        register_dataset_types(SECOND, {"future_visit_image": ("ExposureF", ("visit", "detector"))})
    return run_migration(pair.tmp_path, "analyze", TICKET)


def test_summary_sections(tree) -> None:
    """One migration carrying both kinds of change: the three canonical
    DB-migrated types land under the database section, an added
    config-only override (weird_thing, registered nowhere in the DB) lands
    under the config section, and both header counts are right."""
    tree.register_types({"weird_thing": ("ExposureF", ("visit", "detector"))})
    classes_path = tree.tmp_path / "migrations" / "storage-classes.json"
    classes = json.loads(classes_path.read_text())
    classes["weird_thing"] = "WeirdImage"
    classes_path.write_text(json.dumps(classes))
    canonical_new_state(tree)  # the three DB-migration entries
    # Give the future stored-new config a config-only weird_thing override
    # (absent from the stored old, absent from the DB).
    config = yaml.safe_load(data_text("future-new.yaml"))
    config["registry"]["managers"]["datasets"] = {
        "cls": "lsst.daf.butler.registry.datasets.byDimensions"
               ".ByDimensionsDatasetRecordStorageManagerUUID",
        "config": {
            "overrides": {
                "weird_thing": {"rename": "legacy_weird_thing"},
                "future_weird_thing": {"rename": "weird_thing"},
            }
        },
    }
    stored_config(tree.tmp_path, FUTURE_REPO, "new").write_text(yaml.safe_dump(config))
    run = run_migration(tree.tmp_path, "analyze", TICKET)
    assert run.ok, run.stderr
    out = run.stdout
    assert header_count(out, "Database") == 3
    assert header_count(out, "Config") == 1
    config_at = out.index("Config")
    assert out.index("visit_image") < config_at
    assert out.index("deep_coadd") < config_at
    assert config_at < out.index("weird_thing")
    wt_line = out.splitlines()[entry_line(out, "weird_thing")]
    assert re.search(r"\(unregistered\)", wt_line), out
    db_line = out.splitlines()[entry_line(out, "deep_coadd")]
    assert re.search(r"\(registered\)", db_line), out


def test_summary_groups_identical_changes(two_unanalyzed_tree) -> None:
    """Two independent migrations in one ticket: changes identical across
    primaries (base name AND storage classes AND flags) print once with one
    nested line per prime; the same base name at the SAME future storage
    class but a mixed registered state (the second DB omits
    future_visit_image, so its class comes from storage-classes.json)
    renders as two separate entries and still passes the cross-repo
    guard."""
    run = analyze_two_migrations(two_unanalyzed_tree)
    assert run.ok, run.stderr
    out = run.stdout
    # deep_coadd/difference_image: same key in both migrations -> one
    # entry each.  visit_image: same future storage class (VisitImage),
    # registered vs JSON-fallback -> two entries, one each marker.
    entry_line(out, "deep_coadd")
    entry_line(out, "difference_image")
    visit_lines = [ln for ln in out.splitlines() if "visit_image" in ln]
    assert len(visit_lines) == 2, out
    assert sum(bool(re.search(r"\(registered\)", ln)) for ln in visit_lines) == 1, out
    assert sum(bool(re.search(r"\(unregistered\)", ln)) for ln in visit_lines) == 1, out
    assert header_count(out, "Database") == 4
    assert header_count(out, "Config") == 0
    # the collapsed deep_coadd entry nests one line per prime repo...
    lines = out.splitlines()
    nested: list[str] = []
    for ln in lines[entry_line(out, "deep_coadd") + 1 :]:
        if not ln.strip() or re.search(r"\((un)?registered\)", ln):
            break
        nested.append(ln)
    assert any(REPO in ln for ln in nested), nested
    assert any(SECOND in ln for ln in nested), nested
    # ...and no variants exist here, so no variant marker anywhere.
    assert "+" not in out


def test_summary_variant_nesting(ready_unanalyzed_tree) -> None:
    """A variant's legacy repo id is printed inline on the nested line of
    its own prime (with the + marker), once per entry it belongs to, and
    the variant's future-side id never appears."""
    run = run_migration(ready_unanalyzed_tree.tmp_path, "analyze", TICKET)
    assert run.ok, run.stderr
    out = run.stdout
    lines = out.splitlines()
    variant_lines = [ln for ln in lines if LEGACY_VARIANT in ln]
    # one nested line per DB-migrated entry (variants sorted inline after
    # the prime id)
    assert len(variant_lines) == len(BASE_TYPES), out
    for ln in variant_lines:
        assert re.match(r"^\s*" + REPO + r"\s", ln), ln
        assert "+" in ln, ln
        # a variant never appears on an entry line
        assert not re.search(r"\((un)?registered\)", ln), ln
    # each base entry's first nested line is the primed-with-variant line
    for base in BASE_TYPES:
        assert LEGACY_VARIANT in lines[entry_line(out, base) + 1], out
    assert FUTURE_VARIANT not in out


def test_summary_empty(tree) -> None:
    """Nothing changed (no stored-new edits): the summary is exactly the
    no-changes line, with no section headers."""
    run = run_migration(tree.tmp_path, "analyze", TICKET)
    assert run.ok, run.stderr
    assert run.migration["dataset_types"] == {}
    assert "No dataset type changes" in run.stdout
    assert "Database" not in run.stdout
    assert "Config" not in run.stdout


def test_analyze_rejects_conflicting_future_storage_class(two_unanalyzed_tree) -> None:
    """The same base name held by two selected migrations with different
    future storage classes is rejected, before any summary is printed;
    selecting only one repo has no cross-repo comparison and passes."""
    run = analyze_two_migrations(two_unanalyzed_tree, conflict=True)
    assert run.returncode == 1, f"expected rejection, got rc=0:\n{run.stdout}"
    # base name, both storage classes, and the holding repos are
    # contract-visible (phrasing is not).
    for token in ("visit_image", "ExposureF", "VisitImage", REPO, SECOND):
        assert token in run.stderr, run.stderr
    # the guard runs before the summary: nothing summary-like is printed.
    assert "Database" not in run.stdout, run.stdout
    assert "Config" not in run.stdout, run.stdout
    assert "dataset type changes" not in run.stdout, run.stdout
    # scope is the selected repos only.
    solo = run_migration(two_unanalyzed_tree.tmp_path, "analyze", TICKET, "--repo", SECOND)
    assert solo.ok, solo.stderr
    assert "visit_image" in solo.stdout
