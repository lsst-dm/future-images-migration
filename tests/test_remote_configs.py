"""Tests for repo groups whose external configs live on remote URIs
(``remote-test://``, the deterministic local-backing stand-in for
``s3://`` in lsst-resources tests).

The shared sqlite DB stays local, referenced by absolute path:
``<butlerRoot>`` expands to the remote URI string in a remote-hosted
config, which would corrupt a sqlite db URL, while datastore roots keep
``<butlerRoot>`` and expand harmlessly to the remote URI at open.

One lifecycle test chains, on a single fixture-built group: init +
add-variant seeding byte-equal stored copies, drift rejection with zero
mutations, a copy-rollback (a target that rejects the write leaves the
already-copied remote externals restored from stored *-old and the
database untouched — the restore path's non-OSError storage guard is
remote-relevant), and the canonical analyzed edit + apply + --undo round
trip restoring database names and remote external bytes exactly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml
from lsst.daf.butler import Butler, ButlerConfig

from conftest import (
    APPLIED_NAMES,
    BASE_TYPES,
    FUTURE_REPO,
    FUTURE_VARIANT,
    LEGACY_VARIANT,
    PRE_APPLY_NAMES,
    REPO,
    TICKET,
    RepoPair,
    VARIANTS_KEY,
    assert_rejected,
    data_text,
    dataset_type_names,
    register_dataset_types,
    run_migration,
    stored_config,
)
GROUP_REPOS = (REPO, FUTURE_REPO, LEGACY_VARIANT, FUTURE_VARIANT)


def templatize(text: str, tmp_path: Path) -> str:
    """Swap the butlerRoot-tagged sqlite db URL for an absolute local one
    (unusable through a remote-hosted config)."""
    return text.replace(
        "sqlite:///<butlerRoot>/dp2_prep.sqlite",
        f"sqlite:///{tmp_path}/dp2_prep.sqlite",
    )


def remote_uri(path: Path) -> str:
    """The remote-test:// URI whose backing path is this local file
    (cf. lsst.resources.tests.make_remote_test_uri)."""
    return f"remote-test://localhost{path}"


class RemoteRepoPair(RepoPair):
    """RepoPair whose external configs (both sides and the variant pair)
    are hosted at remote-test:// URIs; the path helpers point at the
    local backing paths of those URIs."""

    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.repo_dir = tmp_path / "remote" / REPO


@pytest.fixture
def remote_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteRepoPair:
    """dp2_prep group with its variant, configs on remote-test:// URIs and
    the shared sqlite DB local by absolute-path db URL."""
    group = RemoteRepoPair(tmp_path)
    group.repo_dir.mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    # One shared real sqlite DB at an absolute path (the <butlerRoot> tag
    # is unusable for db URLs through a remote config).
    for name in ("legacy-old", "future-old", "legacy-variant-old", "future-variant-old"):
        (templates / f"{name}.yaml").write_text(templatize(data_text(f"{name}.yaml"), tmp_path))
    index_path = tmp_path / "repo-index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            {
                REPO: remote_uri(group.external("legacy")),
                FUTURE_REPO: remote_uri(group.external("future")),
                LEGACY_VARIANT: remote_uri(group.variant_external("legacy")),
                FUTURE_VARIANT: remote_uri(group.variant_external("future")),
            }
        )
    )
    # The in-process index cache is keyed by this environment value, so
    # every alias must be registered before the first Butler opens.
    monkeypatch.setenv("DAF_BUTLER_REPOSITORY_INDEX", str(index_path))
    Butler.makeRepo(
        group.repo_dir,
        config=str(templates / "legacy-old.yaml"),
        forceConfigRoot=False,
    )
    shutil.copyfile(templates / "future-old.yaml", group.external("future"))
    shutil.copyfile(templates / "legacy-variant-old.yaml", group.variant_external("legacy"))
    shutil.copyfile(templates / "future-variant-old.yaml", group.variant_external("future"))
    register_dataset_types(REPO, BASE_TYPES)
    return group


def test_remote_group_lifecycle(remote_group) -> None:
    """The full apply lifecycle over remote-hosted externals: init +
    add-variant read the remote configs (stored copies byte-equal), drift
    of one external is refused through the parsed remote config with zero
    mutations anywhere, a copy target that rejects the write leaves the
    already-copied remotes restored from stored *-old with the database
    untouched, and the canonical analyzed edit applies (database renamed,
    remotes rewritten from stored *-new) and undoes back to the captured
    pre-apply bytes."""
    tmp = remote_group.tmp_path
    assert run_migration(tmp, "init", TICKET, REPO).ok
    run = run_migration(tmp, "add-variant", TICKET, REPO, LEGACY_VARIANT, FUTURE_VARIANT)
    assert run.ok, run.stderr
    assert run.migration[VARIANTS_KEY] == [
        {"legacy_repo": LEGACY_VARIANT, "future_repo": FUTURE_VARIANT}
    ]
    for repo in GROUP_REPOS:
        external = ButlerConfig(repo).configFile.read()
        assert stored_config(tmp, repo, "old").read_bytes() == external
        assert stored_config(tmp, repo, "new").read_bytes() == external

    names_before = dataset_type_names(REPO)
    externals_before = {repo: ButlerConfig(repo).configFile.read() for repo in GROUP_REPOS}
    remote_group.drift_external("future")
    assert_rejected(run_migration(tmp, "apply", TICKET), "does not match")
    assert dataset_type_names(REPO) == names_before
    for repo in (REPO, LEGACY_VARIANT, FUTURE_VARIANT):
        assert ButlerConfig(repo).configFile.read() == externals_before[repo]
    assert b"/drifted" in ButlerConfig(FUTURE_REPO).configFile.read()

    remote_group.external("future").write_bytes(externals_before[FUTURE_REPO])
    remote_group.register_future_types()
    for repo, name in (
        (REPO, "legacy-new"),
        (FUTURE_REPO, "future-new"),
        (LEGACY_VARIANT, "legacy-variant-new"),
        (FUTURE_VARIANT, "future-variant-new"),
    ):
        stored_config(tmp, repo, "new").write_text(templatize(data_text(f"{name}.yaml"), tmp))
    assert run_migration(tmp, "analyze", TICKET).ok
    # Copies walk the group in iteration order; the variant's future
    # config is last, so a chmod-blocked target lands the failure after
    # the renames and the earlier remote copies, whose restores then run
    # over remote-test://.
    names_unapplied = dataset_type_names(REPO)
    target = remote_group.variant_external("future")
    os.chmod(target, 0o444)
    try:
        run = run_migration(tmp, "apply", TICKET)
    finally:
        os.chmod(target, 0o644)
    assert run.returncode == 1, f"expected failure, got rc=0:\n{run.stdout}"
    assert dataset_type_names(REPO) == names_unapplied
    for repo in GROUP_REPOS:
        assert ButlerConfig(repo).configFile.read() == (
            stored_config(tmp, repo, "old").read_bytes()
        )

    names_pre_apply = dataset_type_names(REPO)
    externals_pre_apply = {repo: ButlerConfig(repo).configFile.read() for repo in GROUP_REPOS}
    assert run_migration(tmp, "apply", TICKET).ok
    assert dataset_type_names(REPO) == APPLIED_NAMES
    for repo in GROUP_REPOS:
        assert ButlerConfig(repo).configFile.read() == (
            stored_config(tmp, repo, "new").read_bytes()
        )
    assert run_migration(tmp, "apply", TICKET, "--undo").ok
    assert dataset_type_names(REPO) == names_pre_apply == PRE_APPLY_NAMES
    for repo, text in externals_pre_apply.items():
        assert ButlerConfig(repo).configFile.read() == text
