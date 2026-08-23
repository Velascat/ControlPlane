# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""The merge method must be decided by the head's parent count, not by memory.

PR #14 reconciled github/main into the forge and was squash-merged. The API
returned 200, the merge "succeeded", and the second parent was gone -- so
github/main stopped being an ancestor and the push mirror could no longer
fast-forward. The property the PR existed to deliver was destroyed by the
merge method alone. These tests pin the behaviour that prevents that.
"""

from __future__ import annotations

import pytest

from operations_center.entrypoints.pr_review_watcher.main import _merge_method_for


class _Client:
    """Minimal stand-in exposing only what the chooser consults."""

    def __init__(self, parents: int | None = 1, raises: bool = False) -> None:
        self._parents = parents
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def commit_parent_count(self, owner: str, repo: str, sha: str) -> int | None:
        self.calls.append((owner, repo, sha))
        if self._raises:
            raise RuntimeError("api down")
        return self._parents


def test_ordinary_single_parent_head_still_squashes() -> None:
    """The repo convention (`subject (#N)`) is unchanged for normal PRs."""
    assert _merge_method_for(_Client(parents=1), "o", "r", "a" * 40) == "squash"


def test_merge_commit_head_is_merged_not_squashed() -> None:
    """The #14 regression: squashing a merge head discards its ancestry."""
    assert _merge_method_for(_Client(parents=2), "o", "r", "b" * 40) == "merge"


def test_octopus_head_is_also_merged() -> None:
    assert _merge_method_for(_Client(parents=3), "o", "r", "c" * 40) == "merge"


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC_MERGE_METHOD", "merge")
    # even for a single-parent head, and without consulting the client
    client = _Client(parents=1)
    assert _merge_method_for(client, "o", "r", "d" * 40) == "merge"
    assert client.calls == []


def test_unknown_parent_count_falls_back_to_squash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Indeterminate must be loud, not silent -- it is the dangerous case."""
    monkeypatch.delenv("OC_MERGE_METHOD", raising=False)
    with caplog.at_level("WARNING"):
        assert _merge_method_for(_Client(parents=None), "o", "r", "e" * 40) == "squash"
    assert any("unknown" in r.message.lower() for r in caplog.records)


def test_client_error_does_not_crash_the_merge_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OC_MERGE_METHOD", raising=False)
    with caplog.at_level("WARNING"):
        assert _merge_method_for(_Client(raises=True), "o", "r", "f" * 40) == "squash"


def test_client_without_the_method_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not every adapter implements it; absence must not break merging."""
    monkeypatch.delenv("OC_MERGE_METHOD", raising=False)

    class _Bare:
        pass

    assert _merge_method_for(_Bare(), "o", "r", "0" * 40) == "squash"
