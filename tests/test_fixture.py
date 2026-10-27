"""Guards on the committed fixture, not on the tool.

The rest of the suite reads the fixture's databases as the initial state it
expects and copies the fixture's configs verbatim, so a registry that has
drifted from the state model or a config that names a build-time location has
to fail here, where the cause is obvious, rather than as a wrong classification
several files away.
"""

from __future__ import annotations

from typing import Any, Iterator

import yaml

from conftest import FIXTURES, GROUPS, INITIAL_ROW_CLASSES, MEMBERS, Tree, committed_external


def _config_values(node: Any) -> Iterator[str]:
    """Every string in a parsed config, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _config_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _config_values(value)
    elif isinstance(node, str):
        yield node


def test_committed_databases_hold_the_state_model() -> None:
    """Each group's registry holds exactly the dataset types (names and storage
    classes) its state model says, so a stale committed database — left behind
    by a daf-butler schema change or a hand edit — is reported as such."""
    fixture = Tree(FIXTURES)
    for group, rows in INITIAL_ROW_CLASSES.items():
        assert fixture.rows(group) == rows, f"{group} needs tests/fixtures/build_fixture.py"


def test_committed_configs_are_relocatable() -> None:
    """No committed file names a filesystem location: the configs address the
    registry and the datastore through the ``<butlerRoot>`` tag, and a registry
    built with a scratch root baked into it would not work once copied. Both
    are what make a per-test copy of the fixture a working repository."""
    for alias, (group, name) in MEMBERS.items():
        for value in _config_values(yaml.safe_load(committed_external(alias))):
            assert not value.startswith("/"), f"{group}/{name}: {value}"
    for group in GROUPS:
        registry = (FIXTURES / group / f"{group}.sqlite").read_bytes()
        assert b"/tmp/" not in registry and b"/home/" not in registry, group


def test_group_members_share_one_database() -> None:
    """Every member of a group addresses the same registry database — the
    invariant ``init`` and ``add-variant`` test for membership, and the reason
    one ``apply`` renames rows seen by all of them. A member pointing elsewhere
    would silently migrate part of a group."""
    for group, members in GROUPS.items():
        urls = {yaml.safe_load(committed_external(alias))["registry"]["db"] for alias in members}
        assert urls == {f"sqlite:///<butlerRoot>/{group}.sqlite"}
