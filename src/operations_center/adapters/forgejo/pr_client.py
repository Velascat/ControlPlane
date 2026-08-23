# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""Forgejo implementation of the PR seam.

Fills the :class:`~operations_center.adapters.pr.PRClient` protocol against a
Forgejo/Gitea instance. Written from the live findings recorded in
``docs/specs/forgejo-pr-adapter.md`` (2026-08-18, Forgejo 13):

* **No Checks API** — ``/commits/{sha}/check-runs`` is a 404. Forgejo has
  commit *statuses* only, so :meth:`get_check_runs` synthesizes check-run-shaped
  dicts from statuses under an explicit, lossy-but-documented translation (see
  :data:`STATUS_TO_CHECK`). The three gate helpers reproduce the GitHub
  client's semantics — latest-per-name dedupe, ``"name: summary"`` failure
  strings, completed-must-be-non-empty green contract — on top of it.
* **Branch protection has a first-class admin bit.** ``apply_to_admins`` is
  GitHub's ``enforce_admins`` in Forgejo terms, and ``status_check_contexts``
  carries required contexts verbatim. :meth:`get_branch_protection` translates
  to the exact two paths the reviewer's fail-closed gate reads
  (``required_status_checks.contexts``, ``enforce_admins.enabled``), keeping
  the raw rule under ``_forgejo`` so nothing is asserted that the instance did
  not say.

The board adapter's conventions carry over: pagination reads to exhaustion
(a short read is not an error, it is a confident wrong decision), and the
constructor accepts a ``transport`` for tests.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
_MAX_PAGES = 200

#: How a Forgejo commit status becomes a GitHub-shaped check run. Two fields
#: (status, conclusion) from one (status) — the losses are chosen, not implied:
#:
#: * ``error`` collapses into conclusion ``failure``: both block a merge. The
#:   original word survives in ``output.title``, so failure strings still read
#:   ``"ctx: error"``.
#: * ``warning`` becomes ``neutral`` — completed, **not** a success and **not**
#:   a failure, which is precisely Forgejo's non-blocking semantics. A naive
#:   translation folding it into ``success`` would let "completed with
#:   warnings" masquerade as "passed".
#: * ``pending`` is ``in_progress`` with no conclusion, so the incomplete/
#:   failed distinction the merge gate depends on survives.
STATUS_TO_CHECK: dict[str, tuple[str, str | None]] = {
    "pending": ("in_progress", None),
    "success": ("completed", "success"),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
    "warning": ("completed", "neutral"),
}


class ForgejoPRClient:
    """PR operations against one Forgejo instance.

    ``owner``/``repo`` travel per call, exactly as on ``GitHubPRClient`` — the
    protocol was extracted verbatim, so a caller cannot tell which forge it
    holds.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"token {api_token}"},
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def _paginate(self, url: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Read every page. A page-one-only read of a 90-item list is not an
        error anyone sees — it is 40 items silently missing from a decision."""
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            resp = self._request(
                "GET", url, params={**(params or {}), "page": page, "limit": _PAGE_SIZE}
            )
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"expected a list from {url}, got {type(batch).__name__}")
            out.extend(batch)
            if len(batch) < _PAGE_SIZE:
                return out
        raise RuntimeError(f"{url}: still returning full pages after {_MAX_PAGES} pages")

    # ── pull requests ────────────────────────────────────────────────────────

    def create_pr(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str = ""
    ) -> dict:
        resp = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )
        return resp.json()

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}").json()

    def merge_pr(
        self, owner: str, repo: str, pr_number: int, *, merge_method: str = "squash"
    ) -> dict:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json={"Do": merge_method},
        )
        # Forgejo answers 200 with an empty body; report what happened in the
        # key GitHub callers look at.
        return {"merged": True}

    def commit_parent_count(self, owner: str, repo: str, sha: str) -> int | None:
        """Number of parents of ``sha``, or None if it cannot be determined.

        Used to decide whether squashing is safe: squashing a head that is
        itself a merge collapses its parents, which silently discards whatever
        ancestry the pull request existed to establish.
        """
        try:
            data = self._request("GET", f"/repos/{owner}/{repo}/git/commits/{sha}").json()
        except Exception:
            return None
        parents = data.get("parents")
        return len(parents) if isinstance(parents, list) else None

    def close_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        return self._request(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", json={"state": "closed"}
        ).json()

    def list_open_prs(self, owner: str, repo: str) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls", {"state": "open"})

    def list_closed_prs(self, owner: str, repo: str) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls", {"state": "closed"})

    def find_pr_by_head(self, owner: str, repo: str, head_ref: str) -> dict | None:
        # Forgejo's list endpoint has no `head=` filter; filter client-side over
        # the exhaustive read.
        for pr in self._paginate(f"/repos/{owner}/{repo}/pulls", {"state": "open"}):
            if ((pr.get("head") or {}).get("ref")) == head_ref:
                return pr
        return None

    def get_mergeable(self, owner: str, repo: str, pr_number: int) -> bool | None:
        return self.get_pr(owner, repo, pr_number).get("mergeable")

    def update_pr_description(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        return self._request(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", json={"body": body}
        ).json()

    def create_and_merge(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str = "",
        merge_method: str = "squash",
    ) -> str:
        """Create a PR, merge it, then delete the head branch. Returns the PR html_url."""
        pr = self.create_pr(owner, repo, head=head, base=base, title=title, body=body)
        pr_number = pr["number"]
        pr_url = pr["html_url"]
        self.merge_pr(owner, repo, pr_number, merge_method=merge_method)
        try:
            self.delete_branch(owner, repo, head)
        except httpx.HTTPStatusError:
            _logger.warning("create_and_merge: could not delete branch %r", head)
        return pr_url

    # ── diffs and files ──────────────────────────────────────────────────────

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> list[str]:
        files = self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
        return [f.get("filename", "") for f in files if f.get("filename")]

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}.diff").text

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str
    ) -> tuple[str, str] | None:
        try:
            resp = self._request(
                "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        data = resp.json()
        text = base64.b64decode(data.get("content", "") or "").decode("utf-8")
        return text, data.get("sha", "")

    def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        new_text: str,
        message: str,
        branch: str,
        blob_sha: str,
    ) -> bool:
        resp = self._request(
            "PUT",
            f"/repos/{owner}/{repo}/contents/{path}",
            json={
                "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
                "message": message,
                "branch": branch,
                "sha": blob_sha,
            },
        )
        return resp.status_code in (200, 201)

    # ── review ───────────────────────────────────────────────────────────────

    def list_pr_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        # Issues and PRs share a number space, so PR discussion comments live on
        # the issue endpoint — same as GitHub.
        return self._paginate(f"/repos/{owner}/{repo}/issues/{pr_number}/comments")

    def list_pr_review_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        # No flat review-comments endpoint; flatten across reviews.
        out: list[dict] = []
        for review in self.list_pr_reviews(owner, repo, pr_number):
            rid = review.get("id")
            if rid is None:
                continue
            out.extend(
                self._paginate(
                    f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{rid}/comments"
                )
            )
        return out

    def list_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews")

    def pr_has_changes_requested(self, owner: str, repo: str, pr_number: int) -> bool:
        # Forgejo spells it REQUEST_CHANGES; GitHub spells it CHANGES_REQUESTED.
        # Accept both so the caller's question, not the forge's dialect, decides.
        return any(
            review.get("state") in ("REQUEST_CHANGES", "CHANGES_REQUESTED")
            for review in self.list_pr_reviews(owner, repo, pr_number)
        )

    def post_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments", json={"body": body}
        ).json()

    def update_comment(self, owner: str, repo: str, comment_id: int, body: str) -> dict:
        return self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": body}
        ).json()

    def get_pr_reactions(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/issues/{pr_number}/reactions")

    def get_comment_reactions(self, owner: str, repo: str, comment_id: int) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions")

    # ── CI signal ────────────────────────────────────────────────────────────

    def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str = "",
        target_url: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "state": state,
            "context": context,
            "description": description,
        }
        if target_url is not None:
            payload["target_url"] = target_url
        return self._request(
            "POST", f"/repos/{owner}/{repo}/statuses/{sha}", json=payload
        ).json()

    def get_check_runs(self, owner: str, repo: str, ref: str) -> list[dict]:
        """Commit statuses, shaped like check runs.

        The endpoint returns the full posting *history*; consumers dedupe to
        latest-per-name by ``id`` exactly as they do on GitHub, which Forgejo's
        monotonically increasing status ids support. The status word the
        instance actually reported survives in ``output.title``.
        """
        statuses = self._paginate(f"/repos/{owner}/{repo}/commits/{ref}/statuses")
        runs = []
        for st in statuses:
            word = str(st.get("status", ""))
            run_status, conclusion = STATUS_TO_CHECK.get(word, ("completed", "failure"))
            runs.append(
                {
                    "id": st.get("id", 0),
                    "name": st.get("context", "unknown"),
                    "status": run_status,
                    "conclusion": conclusion,
                    "output": {"title": word},
                }
            )
        return runs

    def _latest_runs(
        self, owner: str, repo: str, pr_number: int, pr_data: dict | None
    ) -> list[dict]:
        if pr_data is None:
            pr_data = self.get_pr(owner, repo, pr_number)
        head_sha = (pr_data.get("head") or {}).get("sha", "")
        if not head_sha:
            return []
        try:
            check_runs = self.get_check_runs(owner, repo, head_sha)
        except Exception:
            return []
        latest: dict[str, dict] = {}
        for cr in check_runs:
            name = cr.get("name", "unknown")
            if cr.get("id", 0) > latest.get(name, {}).get("id", 0):
                latest[name] = cr
        return list(latest.values())

    @staticmethod
    def _kept(name: str, ignored_checks: list[str] | None) -> bool:
        ignored = [s.lower() for s in (ignored_checks or [])]
        return not (ignored and any(pat in name.lower() for pat in ignored))

    def get_failed_checks(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        pr_data: dict | None = None,
        ignored_checks: list[str] | None = None,
    ) -> list[str]:
        """Human-readable descriptions of failing checks — GitHub-format strings."""
        failed = []
        for cr in self._latest_runs(owner, repo, pr_number, pr_data):
            if cr.get("conclusion") in ("failure", "timed_out", "cancelled"):
                name = cr.get("name", "unknown")
                if not self._kept(name, ignored_checks):
                    continue
                summary = (cr.get("output") or {}).get("title") or cr.get("conclusion", "failed")
                failed.append(f"{name}: {summary}")
        return failed

    def get_incomplete_checks(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        pr_data: dict | None = None,
        ignored_checks: list[str] | None = None,
    ) -> list[str]:
        """Names of checks not yet terminal. Non-empty means "not green yet"."""
        return [
            cr.get("name", "unknown")
            for cr in self._latest_runs(owner, repo, pr_number, pr_data)
            if cr.get("status") != "completed"
            and self._kept(cr.get("name", "unknown"), ignored_checks)
        ]

    def get_completed_checks(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        pr_data: dict | None = None,
        ignored_checks: list[str] | None = None,
    ) -> list[str]:
        """Names of checks in a terminal state.

        Same contract as the GitHub client: a gate must require this NON-EMPTY,
        or the no-CI-yet window reads as green.
        """
        return [
            cr.get("name", "unknown")
            for cr in self._latest_runs(owner, repo, pr_number, pr_data)
            if cr.get("status") == "completed"
            and self._kept(cr.get("name", "unknown"), ignored_checks)
        ]

    # ── branches ─────────────────────────────────────────────────────────────

    def get_branch_head(self, owner: str, repo: str, branch: str) -> str | None:
        try:
            resp = self._request("GET", f"/repos/{owner}/{repo}/branches/{branch}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return ((resp.json().get("commit") or {}).get("id")) or None

    def get_branch_protection(self, owner: str, repo: str, branch: str) -> dict | None:
        """The protection rule, translated to the two paths the gate reads.

        ``required_status_checks.contexts`` ← ``status_check_contexts`` when
        ``enable_status_check`` is on; ``enforce_admins.enabled`` ←
        ``apply_to_admins``, which is the same guarantee under Forgejo's name:
        repository admins cannot bypass the rule. The untranslated rule rides
        along under ``_forgejo`` so a reader can always check what the instance
        actually said.
        """
        try:
            resp = self._request(
                "GET", f"/repos/{owner}/{repo}/branch_protections/{branch}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        rule = resp.json()
        contexts = (
            list(rule.get("status_check_contexts") or [])
            if rule.get("enable_status_check")
            else []
        )
        return {
            "required_status_checks": {"contexts": contexts},
            "enforce_admins": {"enabled": bool(rule.get("apply_to_admins"))},
            "_forgejo": rule,
        }

    def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        self._request("DELETE", f"/repos/{owner}/{repo}/branches/{branch}")
