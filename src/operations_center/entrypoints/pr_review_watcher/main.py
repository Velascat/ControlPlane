# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""PR Review Watcher — two-phase autonomous state machine for goal-lane PRs.

Phase 0 (ci_fix): when CI is failing on an autonomy PR, auto-fix the branch.
  - Runs ruff --fix + format on the local checkout and pushes.
  - Retries up to max_ci_fix_attempts; then falls through to self_review.

Phase 1 (self_review): executor reviews the diff and emits LGTM or CONCERNS.
  - LGTM → merge. This is the ONLY merge path on the self-review track
    (verdict-gated — a PR is never merged while concerns are unresolved).
  - CONCERNS → dispatch a fix pass that resolves the concerns on the PR's own
    branch (updating the open PR), then re-review next cycle. After
    max_fix_attempts without reaching LGTM, the PR is CLOSED and the issue is
    re-queued for a fresh attempt — a half-finished PR is never shipped.
  - No verdict (pipeline crash/timeout/rate-limit) → retry; after
    max_self_review_loops with no parseable verdict the PR is left OPEN and
    flagged needs-human (a reviewer outage must not destroy a good PR), and
    polling continues so a recovered backend reviews it later.

Green CI is a precondition for merge, not a trigger: a red-CI PR defers; a
green-CI PR still must pass the verdict gate. Re-queuing is bounded by
_MAX_REQUEUES (its own label); once exhausted the issue is left Blocked for a
human. There is no human_review phase — autonomy PRs reach LGTM and merge, are
closed + re-queued (concerns unresolvable), or are left open for a human
(unreviewable).

State per PR persisted in state/pr_reviews/<repo_key>-<pr_number>.json.
The state file is the single source of truth; Plane is updated after state is written.

CLI matches the reviewer role contract used by operations-center.sh:
    --config              path to operations_center.local.yaml
    --watch               run as a daemon (loop forever)
    --poll-interval-seconds N
    --status-dir          directory for heartbeat_review.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from operations_center.close_invariants import (
    branch_delete_allowed_after_close,
    close_without_receipt_allowed,
)
from operations_center.entrypoints.board_worker._subprocess import (
    build_allowlist_env,
    git_token_passthrough,
    harden_git_token,
)
from operations_center.entrypoints.board_worker.netns import (
    EgressContainmentRequiredError,
    maybe_netns,
    netns_enabled,
)
from operations_center.entrypoints.board_worker.sandbox import (
    ContainmentRequiredError,
    _resolve_egress_proxy,
    maybe_sandbox,
    sandbox_enabled,
    verify_containment,
)
from operations_center.entrypoints.heartbeat import write_heartbeat
from operations_center.reviewer.instrumentation import (
    get_instrumenter,
    record_decision_outcome,
    record_ci_gate_defer,
    record_escalation,
)
from operations_center.entrypoints.pr_review_watcher.inj import (
    UNTRUSTED_PREAMBLE,
    fence,
    make_nonce,
    sanitize_for_comment,
)
from operations_center.entrypoints.pr_review_watcher.member_runner import (
    build_member_argv as _build_member_argv,
)
from operations_center.entrypoints.pr_review_watcher.verdict import (
    _COUNCIL_PANEL,
    CONCERNS,
    aggregate_council,
    compute_verdict,
    council_lens_fragment,
    failing_summary,
    last_json_object,
    sensitive_paths_in_diff,
    verdict_schema_prompt,
)
from operations_center.policy.defaults import sensitive_path_patterns
from operations_center.adapters.board import board_project_id

logger = logging.getLogger(__name__)

_STATE_SUBDIR = Path("state") / "pr_reviews"
# C1 — per-PR council audit record (per-member backend/model/lens/verdict +
# the aggregate). Separate from _STATE_SUBDIR (the review state machine's own
# bookkeeping) so the council record is a pure, append-free audit artifact.
_COUNCIL_SUBDIR = Path("state") / "council"


# ── State file helpers ────────────────────────────────────────────────────────


def _state_key(repo_key: str, pr_number: int) -> str:
    return f"{repo_key}-{pr_number}"


def _state_path(oc_root: Path, repo_key: str, pr_number: int) -> Path:
    return oc_root / _STATE_SUBDIR / f"{_state_key(repo_key, pr_number)}.json"


def _council_state_path(oc_root: Path, repo_key: str, pr_number: int) -> Path:
    return oc_root / _COUNCIL_SUBDIR / f"{_state_key(repo_key, pr_number)}.json"


def _prune_orphan_state_files(oc_root: Path, repo_key: str, open_numbers: set[int]) -> None:
    """Delete review-state files for PRs that are no longer open.

    The per-merge/close unlinks in _merge_and_done / _close_and_requeue only fire
    when THIS watcher terminates a PR. PRs merged or closed by any other means
    (a manual ``gh pr merge``, another host, or while this watcher was down/stale)
    leave their state/pr_reviews/<repo>-<n>.json behind, and they accumulate
    forever. Callers MUST pass a set built from a SUCCESSFUL list_open_prs — any
    state file for this repo whose PR is not in that set is for a terminated PR.
    A false prune (PR open but missing from a partial fetch) is self-healing: the
    next poll re-discovers the open PR and re-creates its state.
    """
    state_dir = oc_root / _STATE_SUBDIR
    if not state_dir.is_dir():
        return
    prefix = f"{repo_key}-"
    for f in state_dir.glob(f"{repo_key}-*.json"):
        if not f.stem.startswith(prefix):
            continue
        num_part = f.stem[len(prefix) :]
        if not num_part.isdigit() or int(num_part) in open_numbers:
            continue
        # A state file surviving to prune time means THIS watcher did not
        # terminate the PR — its own merge/close paths unlink first. A plain
        # orphan conflates several causes (a human gh pr merge, another host,
        # this watcher being down), so it is NOT a clean intervention signal.
        # But an orphan that was *escalated for human attention* is: the worker
        # explicitly handed the PR to a human, and the PR has now left the open
        # set, so a human resolved the escalation. That is a genuine operator
        # intervention — capture an (unjudged) ledger candidate before pruning.
        orphan = _load_state(f)
        if orphan.get("escalated_needs_human"):
            _capture_human_intervention(
                "worker-escalation-resolved-by-human", f"{repo_key}#{num_part}"
            )
        try:
            f.unlink(missing_ok=True)
            logger.info("pr_review_watcher: pruned orphan review-state %s (PR not open)", f.name)
        except Exception as exc:
            logger.debug("pr_review_watcher: prune failed for %s — %s", f.name, exc)


def _capture_human_intervention(signal: str, context: str) -> None:
    """Best-effort: append a candidate to the operator-interventions ledger.

    Calls ``cl ledger capture`` (ContextLifecycle), which appends an *unjudged*
    candidate to the ledger in the private manifest and dedups on signal+context.
    Fail-soft by design: if ``cl`` is not on PATH or no private manifest resolves,
    this is a silent no-op — capture must never wedge, slow, or fail the poll
    loop. Promotion of the candidate (the judgment line) stays manual.
    """
    try:
        subprocess.run(
            ["cl", "ledger", "capture", signal, context],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — capture is best-effort telemetry
        logger.debug("pr_review_watcher: ledger capture failed (%s) — %s", signal, exc)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    state = dict(state)
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_provenance() -> None:
    """Record which code is actually running, at startup.

    An editable install resolves imports through a .pth, and any PYTHONPATH
    entry precedes it, so the tree that executes is decided by an environment
    variable that nothing records. A fix was applied to the wrong checkout and
    believed live for six hours because the logs could not answer "what code is
    this?". Four facts settle it: module path, commit, dirty flag, and the
    merge method actually in force.
    """
    module_path = Path(__file__).resolve()
    sha, dirty = "unknown", "unknown"
    try:
        root = module_path.parents[4]
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
        porcelain = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        dirty = "DIRTY" if porcelain.strip() else "clean"
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "pr_review_watcher: provenance — module=%s commit=%s tree=%s "
        "merge_method=%s(%s) PYTHONPATH=%s",
        module_path,
        sha,
        dirty,
        os.environ.get("OC_MERGE_METHOD", "auto"),
        "override" if os.environ.get("OC_MERGE_METHOD") else "by parent count",
        os.environ.get("PYTHONPATH") or "(unset)",
    )
    if dirty == "DIRTY":
        logger.warning(
            "pr_review_watcher: running from a DIRTY tree at %s — uncommitted "
            "changes are executing and will vanish on any checkout",
            module_path.parents[4],
        )


def _merge_method_for(gh_client: Any, owner: str, repo: str, head_sha: str) -> str:
    """Pick the merge method for this PR.

    The repo convention is ``squash`` (``subject (#N)``) and that stays the
    default. The exception is a head that is itself a merge commit: squashing
    collapses it to a single parent, so any ancestry the PR existed to
    establish is silently discarded. That is exactly how the reconciliation in
    PR #14 was undone -- the merge succeeded, returned 200, and destroyed the
    property it delivered.

    Deciding from the head's parent count rather than an environment variable
    means the safe choice needs no operator to remember it. ``OC_MERGE_METHOD``
    remains as an explicit override.
    """
    override = os.environ.get("OC_MERGE_METHOD")
    if override:
        return override

    counter = getattr(gh_client, "commit_parent_count", None)
    parents: int | None = None
    if counter is not None:
        try:
            candidate = counter(owner, repo, head_sha)
            # Guard the TYPE as well as the call. A client that answers with
            # something unexpected — a stub, a test double, an error payload —
            # would otherwise raise on the comparison below, and this function
            # is on the merge path. bool is excluded because it is an int.
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                parents = candidate
            elif candidate is not None:
                logger.warning(
                    "pr_review_watcher: parent count for %s came back as %s, not an int",
                    head_sha[:10],
                    type(candidate).__name__,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pr_review_watcher: could not read parent count for %s — %s", head_sha[:10], exc
            )

    if parents is None:
        # Say so rather than silently assuming. Squash keeps the convention, but
        # an undetected merge head would be flattened, so this must be visible.
        logger.warning(
            "pr_review_watcher: parent count for %s is unknown — using squash; "
            "an ancestry PR merged now would be flattened",
            head_sha[:10],
        )
        return "squash"

    if parents > 1:
        logger.info(
            "pr_review_watcher: head %s has %d parents — merging rather than squashing "
            "so the ancestry survives",
            head_sha[:10],
            parents,
        )
        return "merge"
    return "squash"


def _pr_head_sha(pr_data: dict[str, Any]) -> str:
    """Return the PR head SHA when GitHub provided one, else empty string."""
    return str(((pr_data.get("head") or {}).get("sha") or "")).strip()


def _normalize_concerns_summary(summary: str) -> str:
    """Normalize a reviewer summary for stable no-progress comparisons."""
    return " ".join(str(summary).split())


# A line that begins an enumerated review concern: a bullet (-, *, +) or an
# "N." / "N)" ordinal marker. Used to split a reviewer summary into individually
# addressable concerns. See docs/design/SELF_HEAL_LADDER.md.
_CONCERN_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def _structure_concerns(summary: str) -> list[str]:
    """Split a freeform reviewer summary into individually-addressable concerns.

    Most reviewer summaries enumerate concerns as a bulleted or numbered list.
    Returning one string per concern lets the fix pass be told to resolve EACH
    (and, at the decompose rung, be dispatched one pass per concern). Falls back
    to paragraph splitting, then to the whole summary as a single concern —
    never returns an empty list for non-empty input."""
    text = str(summary or "").strip()
    if not text:
        return []
    items: list[str] = []
    current: list[str] = []
    saw_bullet = False
    for line in text.splitlines():
        m = _CONCERN_BULLET_RE.match(line)
        if m:
            saw_bullet = True
            if current:
                items.append("\n".join(current).strip())
            current = [m.group(1)]
        elif current:
            current.append(line.strip())  # continuation of the current item
    if current:
        items.append("\n".join(current).strip())
    if saw_bullet:
        out = [it for it in items if it]
        if out:
            return out
    # No list markers — split on blank-line paragraphs, else the whole text.
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras if len(paras) > 1 else [text]


# The acceptance bar handed to EVERY fix pass. "Tests pass" is necessary but NOT
# sufficient: #313 shipped a symbol that was unit-tested in isolation and never
# wired into production. The bar is RESOLVING the concern — proven by the
# production call path — not by another green test. See SELF_HEAL_LADDER.md.
_FIX_ACCEPTANCE_BAR = (
    "## How each concern must be resolved\n\n"
    "- Resolve EVERY concern listed above. A pass that changes nothing, or that "
    "only addresses the concerns you find easy, is a failed pass.\n"
    "- Passing tests and linters is NECESSARY BUT NOT SUFFICIENT. If a concern is "
    "that something is defined, declared, or tested but never called/wired in "
    "production, you MUST connect it to its production call path and point to "
    "where it is invoked. Do NOT resolve such a concern by adding another test — "
    "that is exactly the defect the reviewer flagged.\n"
    "- Before finishing, run the repository's incomplete-integration gate and "
    "clear anything it reports:\n"
    "    custodian-multi --repos . --only D12,DC10 --include-deprecated --fail-on-findings\n"
    "  A D12 finding means a public symbol is tested but never wired into "
    "production; a DC10 finding means a doc claims an integration is complete "
    "while the wiring is deferred. Either means a concern is not actually "
    "resolved — fix the code/doc until the gate is clean.\n"
    "- Push to the existing branch (do NOT open a new pull request) so the open "
    "PR updates in place."
)


def _build_fix_goal(concerns: str, *, extra_context: str = "") -> str:
    """Construct the fix-pass goal: enumerated concerns + the anti-no-op
    acceptance bar, plus optional ladder enrichment (``extra_context``)."""
    items = _structure_concerns(concerns)
    if len(items) > 1:
        enumerated = "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))
        concern_block = (
            "A self-review of the currently open pull request raised "
            f"{len(items)} concerns:\n\n{enumerated}"
        )
    else:
        body = items[0] if items else str(concerns)
        concern_block = (
            "A self-review of the currently open pull request raised the "
            f"following concern:\n\n{body}"
        )
    parts = [concern_block, _FIX_ACCEPTANCE_BAR]
    if extra_context.strip():
        parts.append(extra_context.strip())
    return "\n\n".join(parts)


# How much of the PR diff to fold into the L1 enrichment for orientation. The
# fix worker clones the branch and can run `git diff` itself, so this is a
# pointer, not the source of truth — keep it bounded so the prompt stays small.
_LADDER_DIFF_CAP = 8_000


def _ladder_enrichment(level: int, *, pr_diff: str = "") -> str:
    """Per-rung enrichment handed to ``_run_fix_pass`` as ``extra_context``.

    Level 0 is the standard pass (no enrichment). Each higher rung adds
    resolving power instead of conceding to a human:

    - **L1** — the previous pass changed nothing; do not repeat it. Fold in a
      bounded slice of the PR diff for orientation.
    - **L2+** — decompose: resolve ONE concern per pass (narrower scope, higher
      resolve rate); the rest are picked up on following passes.

    See docs/design/SELF_HEAL_LADDER.md."""
    if level <= 0:
        return ""
    parts = [
        "## Earlier passes did not resolve these concerns",
        "A previous automated fix pass on this same branch changed nothing (or "
        "left the concerns above unresolved). Do NOT repeat the same approach — "
        "read the actual code paths involved and take a concretely different one.",
    ]
    if level >= 2:
        parts.append(
            "Resolving every concern at once has failed. This pass, pick the "
            "SINGLE most important still-unresolved concern and fully resolve "
            "just that one — wire it end to end and show the call path. The "
            "remaining concerns are handled on the following passes."
        )
    diff = (pr_diff or "").strip()
    if diff:
        if len(diff) > _LADDER_DIFF_CAP:
            diff = diff[:_LADDER_DIFF_CAP] + "\n...[diff truncated for orientation]"
        # The PR diff is UNTRUSTED repo content. The reviewer fences it for the
        # review pass; the fix pass (which has push capability) must too — a
        # markdown ```diff``` block is attacker-breakable (the diff can contain a
        # ``` line), so use the nonce fence + preamble instead (INJ G-3).
        nonce = make_nonce()
        parts.append(
            "## The PR diff under review (for orientation — UNTRUSTED DATA)\n\n"
            + UNTRUSTED_PREAMBLE
            + "\n\n"
            + fence("pr_diff", diff, nonce)
        )
    return "\n\n".join(parts)


def _new_state(repo_key: str, pr_number: int) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "state_key": _state_key(repo_key, pr_number),
        "pr_number": pr_number,
        "repo_key": repo_key,
        "phase": "ci_fix",
        "ci_fix_attempts": 0,
        "ci_fix_last_push_at": None,
        "self_review_loops": 0,
        "human_review_loops": 0,
        "processed_comment_ids": [],
        "plane_task_id": None,
        "phase2_entered_at": None,
        "created_at": now,
        "updated_at": now,
    }


# ── Settings / adapter helpers ────────────────────────────────────────────────


def _load_settings(config_path: Path):
    from operations_center.config import load_settings

    return load_settings(config_path)


def _github_client(settings):
    from operations_center.adapters.pr import make_pr_client

    return make_pr_client(settings)


def _plane_client(settings):
    from operations_center.adapters.board import make_board_client

    return make_board_client(settings)


def _owner_repo(clone_url: str) -> tuple[str, str]:
    from operations_center.adapters.pr import owner_repo_from_clone_url

    return owner_repo_from_clone_url(clone_url)


def _venv_python(oc_root: Path) -> str:
    p = oc_root / ".venv" / "bin" / "python"
    return str(p) if p.exists() else "python3"


def _build_env(oc_root: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(oc_root / "src")
    return env


def _sandbox_enabled() -> bool:
    """True when the bwrap sandbox is enabled for worker subprocesses. Delegates
    to board_worker's single gate (default-on, audit Track A3) so the reviewer's
    executor is contained on exactly the same switch as board_worker dispatch."""
    return sandbox_enabled()


class OCSourceTreeUncleanError(RuntimeError):
    """The OC source tree used to RUN the reviewer is broken — e.g. a concurrent
    session (watchdog merge, fix pass) left git conflict markers in a tracked
    source file. This is an ENVIRONMENT failure, not a PR-quality failure: the
    planning subprocess imports the ``operations_center`` package from
    ``oc_root/src`` and would crash with SyntaxError at import time *for every
    PR*, regardless of the diff under review. Surfaced distinctly so it never
    burns a PR's review budget or reads as "no verdict"."""


class ReviewerBackendError(RuntimeError):
    """The reviewer backend (``claude`` CLI) crashed, was killed, or timed out.

    This is an INFRA failure, not a PR-quality failure.  The PR may be perfectly
    good; the backend just can't review it right now (e.g. a transient rate-limit,
    OOM kill, or process timeout).  Surfaced distinctly so crashes never burn the
    PR's ``no_verdict_passes`` budget — only a clean exit with no verdict.json
    written counts against that budget."""


def _oc_source_conflict_markers(oc_root: Path) -> list[str]:
    """Tracked ``src/`` Python files containing git conflict markers.

    Empty list means the import path is clean. A non-empty result means the
    reviewer cannot run until the tree is repaired — see OCSourceTreeUncleanError.
    Cheap (single ``git grep``); fail-open (returns [] if git is unavailable)
    so this guard can never itself wedge the reviewer."""
    try:
        out = subprocess.run(
            ["git", "grep", "-lE", r"^(<<<<<<< |>>>>>>> |=======$)", "--", "src/"],
            cwd=oc_root,
            capture_output=True,
            text=True,
        )
    except Exception:  # noqa: BLE001 — guard must never raise from detection
        return []
    if out.returncode not in (0, 1):  # 1 = no matches (clean); >1 = git error
        return []
    return [line for line in out.stdout.splitlines() if line.strip().endswith(".py")]


def _label_value(labels: list, prefix: str) -> str:
    for lab in labels:
        name = (lab.get("name", "") if isinstance(lab, dict) else str(lab)).strip()
        if name.lower().startswith(prefix.lower() + ":"):
            return name.split(":", 1)[1].strip()
    return ""


def _durable_pr_head_ref(pr_number: int) -> str:
    return f"refs/pull/{pr_number}/head"


def _spec_file_from_plane_issue(issue: dict[str, Any]) -> str:
    labels = issue.get("labels", []) or []
    desc = str(issue.get("description") or issue.get("description_stripped") or "").strip()
    if desc:
        try:
            from operations_center.application.task_parser import TaskParser

            parsed = TaskParser().parse(desc, labels=_label_names(labels))
            spec_file = str(parsed.execution_metadata.get("spec_file") or "").strip()
            if spec_file:
                return spec_file
        except Exception:
            pass

    campaign_id = _label_value(labels, "campaign-id")
    if not campaign_id:
        return ""
    try:
        from operations_center.spec_author.state import CampaignStateManager

        campaigns_state = CampaignStateManager().load()
        for campaign in campaigns_state.campaigns:
            if campaign.campaign_id == campaign_id:
                return str(campaign.spec_file).strip()
    except Exception:
        pass
    return ""


def _record_close_receipt(
    settings,
    plane_task_id: str,
    *,
    pr_number: int,
    pr_data: dict[str, Any],
    reason: str,
) -> str:
    """Record a durable salvage receipt on the Plane task before closing."""
    client = _plane_client(settings)
    try:
        issue = client.fetch_issue(plane_task_id)
        spec_file = _spec_file_from_plane_issue(issue)
        if not spec_file:
            return ""
        branch_ref = str(((pr_data.get("head") or {}).get("ref") or "")).strip()
        lines = [
            f"Close receipt for PR #{pr_number} (`{reason}`)",
            f"durable_head_ref: `{_durable_pr_head_ref(pr_number)}`",
            f"spec_file: `{spec_file}`",
        ]
        if branch_ref:
            lines.append(f"closed_branch: `{branch_ref}`")
        client.comment_issue(plane_task_id, "\n".join(lines))
        return spec_file
    finally:
        client.close()


# ── pr review pipeline ────────────────────────────────────────────────────────


def _select_review_backend(settings, *, usage_store=None, now=None):
    """Pick the review backend via the shared fleet ladder (audit D1).

    The reviewer is part of the fleet, so it must respect the same claude→codex
    cooldown/budget ladder the controller uses instead of burning claude
    unconditionally. Returns the ``WorkerBackendSelection`` (``selected_backend``
    is ``claude_code`` when claude is runnable, another backend / ``None`` when
    claude is cooled or over the 25% budget reserve), or ``None`` if selection
    couldn't run — in which case the caller proceeds on claude (today's
    behavior). The merge-gatekeeper must never crash here.
    """
    try:
        from operations_center.backends.worker_backend_selector import select_worker_backend
        from operations_center.execution.usage_store import UsageStore

        team = getattr(settings, "team_executor", None)
        return select_worker_backend(
            preferred_backend="claude_code",
            usage_store=usage_store or UsageStore(),
            dynamic_enabled=bool(getattr(team, "dynamic_worker_backend_selection", True)),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — never block the gate on a store read
        logger.warning("pr_review_watcher: backend selection failed, proceeding on claude: %s", exc)
        return None


def _run_member_review(
    oc_root: Path,
    goal_text: str,
    state_key: str,
    *,
    backend: str = "claude_code",
    model: str = "haiku",
) -> dict | None:
    """Run one review-panel member (the self-review's sole member, or one
    council seat) via a direct CLI call in an empty temp directory.

    Bypasses the TeamExecutor pipeline so the workspace's CLAUDE.md cannot
    override the review goal. The diff is already embedded in ``goal_text`` so
    no repo clone is needed. Returns the parsed verdict dict or None.

    The member is expected to write ``verdict.json`` to its cwd (the typed
    schema from ``verdict_schema_prompt()``). Some backends (codex, observed
    not to reliably honor the file-write contract) may instead print their
    answer to stdout — in that case the last balanced JSON object on stdout is
    used as a fallback (:func:`verdict.last_json_object`) before this is
    treated as a genuine no-verdict.
    """
    conflicted = _oc_source_conflict_markers(oc_root)
    if conflicted:
        raise OCSourceTreeUncleanError(
            f"OC source tree at {oc_root} has git conflict markers in "
            f"{len(conflicted)} tracked file(s) "
            f"({', '.join(conflicted[:3])}{'…' if len(conflicted) > 3 else ''}) "
            "— a concurrent session left the shared checkout dirty; refusing "
            "to run the reviewer (it would crash at import)."
        )

    argv = _build_member_argv(backend, model, goal_text)
    if argv is None:
        raise ReviewerBackendError(
            f"no CLI invocation known for backend={backend!r} model={model!r} "
            f"(state_key={state_key})"
        )

    with tempfile.TemporaryDirectory(prefix="oc-review-direct-") as tmpdir:
        tmp = Path(tmpdir)
        verdict_path = tmp / "verdict.json"
        try:
            proc = subprocess.run(
                argv,
                cwd=str(tmp),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise ReviewerBackendError(
                f"member review timed out (300s) for state_key={state_key} "
                f"backend={backend} model={model}"
            )

        raw: dict | None = None
        if verdict_path.exists():
            try:
                raw = json.loads(verdict_path.read_text(encoding="utf-8"))
            except Exception:
                raw = None
        if raw is None:
            # Fallback for a backend that answered on stdout instead of writing
            # the file (observed with codex) — scan for the last balanced JSON
            # object before concluding there is genuinely no verdict.
            raw = last_json_object(proc.stdout)

        if raw is not None:
            # INJ Phase 1 (D-INJ-1): the merge decision is COMPUTED BY CODE from
            # the model's typed per-check statuses — any model-authored "result"
            # in `raw` is ignored. This is the trust boundary: an injected diff
            # can flip a check's enum (re-checkable) but cannot author the verdict.
            checks = raw.get("checks") if isinstance(raw, dict) else None
            result, failing = compute_verdict(checks)
            # Surface each failing check's evidence so the fix-pass + PR comment are
            # ACTIONABLE (not an opaque check-id that no-op-loops to exhaustion).
            # The decision is still code-computed above; this is context only.
            if result == CONCERNS:
                summary = failing_summary(checks, failing)
            else:
                summary = (raw.get("summary") if isinstance(raw, dict) else None) or ""
            return {"result": result, "failing_checks": failing, "summary": summary}

        # No parseable verdict anywhere (file absent/malformed, stdout not JSON).
        if proc.returncode != 0:
            # Non-zero exit = crash, signal kill, or rate-limit — infra failure,
            # not a PR quality problem.  Don't charge the no_verdict budget.
            stdout_tail = (proc.stdout or "").strip()[-500:]
            raise ReviewerBackendError(
                f"reviewer process exited with rc={proc.returncode} "
                f"for state_key={state_key} backend={backend} model={model} "
                f"(stdout_tail={stdout_tail!r})"
            )
        # returncode == 0, no verdict anywhere — the reviewer ran cleanly but
        # produced nothing usable.  Genuine no-verdict; charge the budget.
        stdout_tail = (proc.stdout or "").strip()[-500:]
        logger.warning(
            "pr_review_watcher: no verdict from member review for %s "
            "backend=%s model=%s (rc=0, stdout_tail=%r)",
            state_key,
            backend,
            model,
            stdout_tail,
        )
        return None


def _run_direct_review(
    oc_root: Path,
    goal_text: str,
    state_key: str,
) -> dict | None:
    """Back-compat name for the single self-review member (claude/haiku).

    Kept as a thin alias — rather than renaming this call site inline — so the
    existing test suite (which patches ``_run_direct_review`` directly) keeps
    working unchanged. Prefer :func:`_run_member_review` for any new caller
    that needs a specific backend/model (e.g. the C1 council).
    """
    return _run_member_review(oc_root, goal_text, state_key, backend="claude_code", model="haiku")


# Ordinary-review fallback pairings (D1), keyed by worker backend.
#
# Deliberately INDEPENDENT of ``verdict._COUNCIL_PANEL``. D1 originally derived
# this by scanning the panel, to reuse a pairing already proven safe as a live
# council seat. That coupling is subtly wrong: the panel answers "who adjudicates
# guardrail PRs" (a review-policy choice), while this answers "which model
# reviews when claude is cooled" (a capacity fallback). Deriving one from the
# other means reseating the council SILENTLY disables the fallback — exactly what
# the 2026-08-13 all-Opus panel did, since it carries no codex seat, turning
# every claude-cooled ordinary review into a park on hosts that do have codex.
# The ``codex_cli`` → ``codex`` pairing below is the same one D1 validated; it is
# now stated directly instead of inferred, so the two can vary independently.
_REVIEW_FALLBACK_MODELS: dict[str, str] = {"codex_cli": "codex"}


def _review_model_for_backend(backend: str) -> str | None:
    """Model to pair with a fallback review backend for the ordinary review (D1).

    Returns ``None`` when the backend has no known review pairing (caller then
    parks rather than guess a model). Not consulted for ``claude_code`` (that
    path keeps its own ``_run_direct_review`` haiku default).
    """
    return _REVIEW_FALLBACK_MODELS.get(backend)


def _member_on_cooldown(usage_store, backend: str, model: str, *, now: datetime) -> bool:
    """True when this council member's ``(backend, model)`` is currently cooled.

    Model-aware (unlike ``_select_review_backend``, which is backend-grained
    and can't tell a cooled sonnet from a runnable opus): a lone
    ``model_weekly`` cooldown for a DIFFERENT model does not cool this member —
    the backend's other models remain runnable. An account-wide cooldown
    (``session_5h`` / ``global_weekly`` / unattributed) cools every model of
    the backend, including this one.

    Compared at FAMILY grain. A panel seat may name a pinned version
    (``claude-opus-5``), but the usage store only ever records the limit
    classifier's four-token vocabulary (sonnet/opus/haiku/codex — see
    ``WORKER_BACKEND_MODELS``), because that is all ``detect_model`` can parse
    out of a CLI limit message. Comparing raw strings would make an ``opus``
    cooldown match NO Opus-version seat, so a rate-limited council would look
    fully available and burn its quota dispatching three doomed reviews. Both
    sides are normalized through ``detect_model`` instead; it is the identity
    for bare tokens, so alias-style seats keep their existing behavior.

    Fails OPEN (not cooled) on a store-read error: a broken usage store must
    never itself deadlock the council quorum check (degrade-never-halt).
    """
    try:
        details = usage_store.worker_backend_cooldown_details(backend, now=now)
    except Exception as exc:  # noqa: BLE001 — never block the gate on a store read
        logger.warning(
            "pr_review_watcher: council cooldown check failed for %s/%s — "
            "treating as available: %s",
            backend,
            model,
            exc,
        )
        return False
    from operations_center.backends.limit_classifier import MODEL_WEEKLY, detect_model

    seat_family = detect_model(model, default=model)
    for entry in details:
        if entry.get("limit_kind") == MODEL_WEEKLY:
            entry_model = entry.get("model")
            entry_family = (
                detect_model(entry_model, default=entry_model)
                if isinstance(entry_model, str)
                else entry_model
            )
            if entry_family == seat_family:
                return True
            continue
        return True  # account-wide / unattributed — blocks every model of the backend
    return False


# Branch prefixes that are exempt from the council and fall back to single
# review. Deliberately NARROW: only the reviewer's OWN fix-pass branches
# (``review/<suffix>``, created by _run_fix_pass when a PR has no task branch).
# It must NOT include the fleet's primary feature branches (goal/, test/,
# improve/, spec-author/): the council's PRIMARY purpose (COUNCIL_VERDICT.md
# threat table, row 1) is to stop the FLEET from merging a guardrail change on
# a single-reviewer LGTM — and nearly every PR here is fleet-authored on a
# goal/ branch, so exempting those would make the council inert against its own
# reason to exist. Genuine deadlock (a guardrail PR parked because a family is
# cooled) is bounded instead by the park-cap escalation (F14) + degraded quorum;
# spec C1.4 explicitly permits guardrail parks to wait for a human.
_COUNCIL_EXEMPT_BRANCH_PREFIXES = ("review/",)


def _council_exempt_self_fix(state: dict, pr_data: dict) -> bool:
    """F14 bootstrap-deadlock guard: True only when this PR is the reviewer's
    OWN fix-pass branch (``review/…``), so the reviewer's self-heal fixes are
    never gated by the very council they might be fixing — the narrowest escape
    that keeps the control plane self-healing.

    NOTE (operator policy): this exemption is intentionally scoped to the
    reviewer's own ``review/`` branches, NOT the fleet's primary feature
    branches. A fleet ``goal/`` PR that touches a guardrail surface DOES get the
    full council — that is the control's primary threat (fleet quietly merges a
    guardrail change). Deadlock avoidance for a legitimately-parked guardrail PR
    is handled by the park-cap escalation + degraded quorum, not by widening
    this exemption. Widening it (e.g. to exempt goal/ PRs) is a trust-model
    change for the operator to decide before ``guardrail_paths`` is populated.
    """
    del state  # unused — branch identity alone determines the exemption
    head_ref = str(((pr_data or {}).get("head") or {}).get("ref") or "").strip().lower()
    return bool(head_ref) and head_ref.startswith(_COUNCIL_EXEMPT_BRANCH_PREFIXES)


def _run_pipeline(
    oc_root: Path,
    config_path: Path,
    repo_key: str,
    goal_text: str,
    settings,
    *,
    source: str,
    state_key: str,
    branch_suffix: str,
    task_branch: str | None = None,
    return_result: bool = False,
) -> dict | None:
    """Run worker.main → execute.main.

    By default returns the parsed ``verdict.json`` (review pass). When
    ``return_result`` is True, returns the parsed ``result.json`` execution
    outcome instead (fix pass — no verdict is produced). ``task_branch``
    overrides the branch the executor commits to; when None a throwaway
    ``review/<suffix>`` branch is used. Returns None on failure."""
    python = _venv_python(oc_root)
    env = _build_env(oc_root)
    repo_cfg = settings.repos.get(repo_key)
    if not repo_cfg:
        logger.error("pr_review_watcher: unknown repo_key=%s", repo_key)
        return None

    with tempfile.TemporaryDirectory(prefix="oc-review-") as tmpdir:
        tmp = Path(tmpdir)

        plan_cmd = [
            python,
            "-m",
            "operations_center.entrypoints.worker.main",
            "--goal",
            goal_text,
            "--task-type",
            "chore",
            "--execution-mode",
            "goal",
            "--repo-key",
            repo_key,
            "--clone-url",
            repo_cfg.clone_url,
            "--base-branch",
            repo_cfg.default_branch,
            "--project-id",
            board_project_id(settings),
            "--task-id",
            state_key,
        ]
        # Pre-flight: the planning subprocess imports operations_center from
        # oc_root/src. If a concurrent session left conflict markers there, the
        # import crashes with SyntaxError → "produced no JSON" for every PR.
        # Detect it here and surface it as a distinct ENVIRONMENT failure so the
        # caller skips the review (retry next sweep) instead of charging it to
        # the PR's no-verdict budget. (Root cause of the 2026-06-07 reviewer
        # outage: a marker in cxrp_mapper.py blocked all verdicts for ~4h.)
        conflicted = _oc_source_conflict_markers(oc_root)
        if conflicted:
            raise OCSourceTreeUncleanError(
                f"OC source tree at {oc_root} has git conflict markers in "
                f"{len(conflicted)} tracked file(s) "
                f"({', '.join(conflicted[:3])}{'…' if len(conflicted) > 3 else ''}) "
                "— a concurrent session left the shared checkout dirty; refusing "
                "to run the reviewer (it would crash at import)."
            )

        plan_proc = subprocess.run(plan_cmd, cwd=oc_root, env=env, capture_output=True, text=True)

        try:
            bundle = json.loads(plan_proc.stdout)
        except Exception:
            logger.error(
                "pr_review_watcher: planning produced no JSON for state_key=%s\n%s",
                state_key,
                (plan_proc.stderr or plan_proc.stdout).strip(),
            )
            return None

        if plan_proc.returncode != 0:
            logger.error(
                "pr_review_watcher: planning failed state_key=%s — %s",
                state_key,
                bundle.get("message", "unknown"),
            )
            return None

        bundle_file = tmp / "bundle.json"
        config_copy = tmp / "ops.yaml"
        workspace = tmp / "workspace"
        result_file = tmp / "result.json"

        bundle_file.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        shutil.copy(config_path, config_copy)
        workspace.mkdir()

        exec_cmd = [
            python,
            "-m",
            "operations_center.entrypoints.execute.main",
            "--config",
            str(config_copy),
            "--bundle",
            str(bundle_file),
            "--workspace-path",
            str(workspace),
            "--task-branch",
            task_branch or f"review/{branch_suffix}",
            "--output",
            str(result_file),
            "--source",
            source,
        ]
        # SBX Phase 2: wrap the executor in the bwrap sandbox. The reviewer runs
        # the LEAST-trusted code on the trusted host — arbitrary PR branches, incl.
        # the fix-pass loop's own output — and shells out to claude, so it is the
        # highest-value executor to contain, yet board_worker dispatch sandboxed
        # it while this path did not. Gated on OC_BWRAP_SANDBOX: when OFF the exec
        # is byte-for-byte unchanged (full env, no bwrap) so the critical merge
        # path cannot regress; when ON it runs in bwrap with board_worker's
        # battle-tested MINIMIZED env (build_allowlist_env strips the Plane token,
        # sibling-repo tokens and host secrets — the reviewer's _build_env leaks
        # the full os.environ, so without this the sandbox would just --setenv the
        # crown-jewel tokens back in). Fail-CLOSED since audit Track A3: a missing
        # bwrap makes maybe_sandbox RAISE ContainmentRequiredError (unless
        # OC_SANDBOX_REQUIRED=0), which fails this task with a visible fault
        # rather than running it un-contained. §0.1 holds at fleet level, not at
        # this call: the fleet keeps serving, this dispatch does not.
        if _sandbox_enabled():
            exec_env = build_allowlist_env(
                oc_root, passthrough=git_token_passthrough(settings, repo_cfg)
            )
            # Track A6: per-task App installation token instead of the
            # long-lived credential — the reviewer executor runs the
            # least-trusted input of all.
            exec_env = harden_git_token(
                exec_env, settings=settings, clone_url=getattr(repo_cfg, "clone_url", "")
            )
            run_exec_cmd = maybe_sandbox(
                exec_cmd,
                oc_root=oc_root,
                rw_root=tmp,
                env=exec_env,
                enabled=True,
                chdir=workspace,
            )
            # Structural egress confinement for the LEAST-trusted (reviewer)
            # executor — the board_worker path applies this but the reviewer
            # historically did not, so OC_EGRESS_NETNS was a no-op here. Opt-in +
            # fail-open like the sandbox; honors OC_EGRESS_REQUIRED.
            run_exec_cmd = maybe_netns(
                run_exec_cmd,
                proxy_url=_resolve_egress_proxy(exec_env),
                enabled=netns_enabled(),
            )
        else:
            exec_env, run_exec_cmd = env, exec_cmd
        # Use Popen + start_new_session so the entire process group (including
        # grandchildren like pytest-spawned claude subprocesses) can be killed on
        # timeout.  subprocess.run with capture_output=True only kills the direct
        # child on TimeoutExpired; grandchildren keep the pipe open and
        # communicate() blocks indefinitely.
        _exec_popen = subprocess.Popen(
            run_exec_cmd,
            cwd=oc_root,
            env=exec_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _stdout, _stderr = _exec_popen.communicate(
                timeout=1800,  # 30 min hard cap — prevents hung executor blocking the watcher
            )
            exec_proc = subprocess.CompletedProcess(
                exec_cmd, _exec_popen.returncode, stdout=_stdout, stderr=_stderr
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_exec_popen.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            _exec_popen.wait()
            logger.warning(
                "pr_review_watcher: execute pipeline timed out after 30m for state_key=%s",
                state_key,
            )
            return None
        if exec_proc.returncode != 0:
            logger.warning(
                "pr_review_watcher: execute pipeline exited rc=%d for state_key=%s\nstderr: %s",
                exec_proc.returncode,
                state_key,
                (exec_proc.stderr or exec_proc.stdout or "").strip()[-2000:],
            )

        if return_result:
            if result_file.exists():
                try:
                    return json.loads(result_file.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning(
                        "pr_review_watcher: malformed result.json for state_key=%s", state_key
                    )
            return None

        verdict_path = workspace / "verdict.json"
        if verdict_path.exists():
            try:
                return json.loads(verdict_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(
                    "pr_review_watcher: malformed verdict.json for state_key=%s", state_key
                )
        else:
            logger.warning(
                "pr_review_watcher: no verdict.json produced for state_key=%s (rc=%d)",
                state_key,
                exec_proc.returncode,
            )
        return None


# ── GitHub helpers ─────────────────────────────────────────────────────────────


# ── Merge + Plane done ────────────────────────────────────────────────────────


@contextlib.contextmanager
def _isolated_repo_checkout(local_path: Path, head_ref: str, git_env: dict):
    """Yield (cwd, _git) for a DISPOSABLE checkout of ``head_ref``, isolated from
    the primary ``local_path`` working tree.

    The reviewer's mutating git passes (ruff auto-fix, auto-rebase) must NEVER
    stash/checkout/pull/reset/commit/push in ``local_path``. For OC's OWN repo
    ``local_path`` is the LIVE running checkout (== oc_root): operating there
    stashes the fleet's in-flight work and moves the deployed branch onto an
    untrusted PR head, breaking deploys and landing PR code in the import path.
    (2026-06: the reflog showed `pull --ff-only origin <pr-branch>` in the live
    tree while reviewing an OC PR.)

    A throwaway ``git worktree`` is added off ``local_path``'s existing git dir
    into a private tempdir, checked out at ``origin/<head_ref>``. It shares the
    object store (cheap — no re-clone, no extra fetch of history) but has its own
    HEAD/index/working tree, so nothing here can perturb the primary checkout.
    The worktree is force-removed and the tempdir deleted on exit, even on error.

    Yields:
        (cwd: Path, _git: Callable[..., subprocess.CompletedProcess]) where
        ``_git(*args)`` runs git in the isolated worktree. The first fetch of
        ``head_ref`` happens through the primary git dir (so the new branch ref
        exists) before the worktree is created; if that fetch or the worktree
        add fails the context manager raises and adds nothing.
    """

    def _primary_git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=local_path, env=git_env, capture_output=True, text=True
        )

    # Fetch the PR head into the shared object store via the PRIMARY git dir.
    # This updates remote-tracking refs only (origin/<head_ref>) — it does NOT
    # touch the primary HEAD/index/working tree/stash.
    _primary_git("fetch", "origin", head_ref)

    tmpdir = tempfile.mkdtemp(prefix="oc-review-iso-")
    worktree = Path(tmpdir) / "wt"
    added = False
    try:
        add = _primary_git(
            "worktree", "add", "--detach", "--force", str(worktree), f"origin/{head_ref}"
        )
        if add.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for {head_ref}: {add.stderr.strip() or add.stdout.strip()}"
            )
        added = True

        def _git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *args], cwd=worktree, env=git_env, capture_output=True, text=True
            )

        # Land on a local branch named <head_ref> so commit/push semantics match
        # the old in-place flow (push origin <head_ref> pushes the right branch).
        _git("checkout", "-B", head_ref, f"origin/{head_ref}")
        yield worktree, _git
    finally:
        if added:
            # Remove the worktree registration from the primary git dir, then the
            # tempdir. --force tolerates a dirty worktree (uncommitted ruff edits).
            _primary_git("worktree", "remove", "--force", str(worktree))
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Best-effort prune in case the dir was already gone (e.g. tmp reaped).
        _primary_git("worktree", "prune")


# How many auto-rebase attempts before a CONFLICTING PR is escalated for a human.
# Orthogonal to fix_attempts — a rebase is infrastructure work, not a fix, and must
# never consume the fix budget (that would wrongly close a good PR).
_MAX_REBASE_ATTEMPTS = 3
# Grace window after a rebase push: main moves constantly, so re-rebasing within
# this window would thrash (rebase → push → main moves → rebase …). Defer instead.
_REBASE_GRACE_SECONDS = 120


def _attempt_auto_rebase(repo_cfg, head_ref: str, settings, pr_number: int) -> str:
    """Merge the base branch into a CONFLICTING PR's branch and push, in an
    ISOLATED throwaway worktree (never the primary local_path working tree).

    Returns one of:
      "clean"         — base merged with no real conflict; merge commit pushed.
      "conflict"      — real (non-log) conflict remained; merge aborted, nothing pushed.
      "push_rejected" — push lost a race (branch moved); reset, nothing landed.
      "noop"          — branch already current with base; nothing to do.
      "unavailable"   — no local clone / token configured; cannot rebase here.
      "error"         — anything else; defensive, never raises.

    Safety: only ever creates a *merge commit* (branch moves forward only — no
    force-push, no history rewrite). The merge runs in a disposable worktree off
    local_path's git dir, so even for OC's own repo (where local_path IS the live
    checkout) the primary HEAD/index/working tree/stash are never touched.
    `.console/log.md` auto-resolves via a union driver injected through the shared
    .git/info/attributes (works even when the PR branch predates the committed
    .gitattributes). A textually-clean-but-wrong merge is NOT trusted here — the
    caller does not merge the result this cycle; CI re-runs on the pushed commit
    and the next review re-validates it."""
    local_path = getattr(repo_cfg, "local_path", None) if repo_cfg else None
    if not local_path or not Path(local_path).exists():
        return "unavailable"
    local_path = Path(local_path)
    default_branch = getattr(repo_cfg, "default_branch", "main") or "main"

    git_env = dict(os.environ)
    git_token = settings.git_token()
    author_name = getattr(settings.git, "author_name", "Operations Center Bot")
    author_email = getattr(settings.git, "author_email", "operations-center-bot@example.com")
    git_env["GIT_AUTHOR_NAME"] = author_name
    git_env["GIT_AUTHOR_EMAIL"] = author_email
    git_env["GIT_COMMITTER_NAME"] = author_name
    git_env["GIT_COMMITTER_EMAIL"] = author_email
    if git_token:
        git_env["GH_TOKEN"] = git_token

    try:
        # Inject the union driver for the append-only journal so concurrent log
        # entries auto-keep-both instead of conflicting. .git/info/attributes is
        # local and shared with linked worktrees, so this applies inside the
        # isolated worktree too — and writing it touches no working tree/HEAD.
        info_dir = local_path / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        # Both append-only journals need the union driver. backlog.md was
        # missing, and it is the file that actually conflicted in practice --
        # three PRs stuck on it at once, none of which the watcher could resolve.
        (info_dir / "attributes").write_text(
            ".console/log.md merge=union\n.console/backlog.md merge=union\n",
            encoding="utf-8",
        )

        # Fetch base too (the worktree CM fetches head); harmless on the primary
        # git dir — updates remote-tracking refs only.
        subprocess.run(
            ["git", "fetch", "origin", default_branch],
            cwd=local_path,
            env=git_env,
            capture_output=True,
            text=True,
        )

        with _isolated_repo_checkout(local_path, head_ref, git_env) as (_cwd, _git):
            merge = _git("merge", "--no-edit", f"origin/{default_branch}")
            if merge.returncode == 0:
                if "Already up to date" in (merge.stdout or ""):
                    return "noop"
                # Merged cleanly per git — but a real (non-log) conflict path would
                # have left the merge unfinished; double-check there are none.
                if _git("diff", "--diff-filter=U", "--name-only").stdout.strip():
                    _git("merge", "--abort")
                    return "conflict"
                push = _git("push", "origin", f"HEAD:{head_ref}")
                if push.returncode == 0:
                    return "clean"
                _git("reset", "--hard", f"origin/{head_ref}")
                return "push_rejected"

            # Non-zero merge: conflicts. If every unmerged path is the log (union
            # should have handled it but be defensive), there are none here → real
            # conflict. Abort; never force a resolution.
            _git("merge", "--abort")
            return "conflict"
    except Exception as exc:  # noqa: BLE001 — rebase must never crash the watcher
        logger.warning("pr_review_watcher: auto-rebase PR #%d errored — %s", pr_number, exc)
        return "error"


_DOC_PATH_SUFFIXES = (".md", ".markdown", ".rst", ".txt")


def _is_doc_path(path: str) -> bool:
    """True for documentation files — any doc extension, or anything under docs/."""
    p = path.strip().lower()
    return bool(p) and (p.endswith(_DOC_PATH_SUFFIXES) or p.startswith("docs/") or "/docs/" in p)


def _files_from_diff(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff's ``diff --git a/X b/Y`` headers."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2 and parts[1].strip():
                files.append(parts[1].strip())
    return files


def _diff_is_docs_only(files) -> bool:
    """True when every changed file is documentation (and there is at least one).

    A docs-only diff gets a review rubric that does NOT demand in-diff proof of
    facts a document legitimately references but cannot contain (CI runs, secrets,
    sibling/other-repo PRs) — the over-flagging that looped #334.
    """
    fs = [f for f in (files or []) if f]
    return bool(fs) and all(_is_doc_path(f) for f in fs)


# Review rubric injected when the diff is documentation-only — see _diff_is_docs_only.
_DOC_ONLY_REVIEW_RUBRIC = (
    "\n\n## This diff is DOCUMENTATION-ONLY — apply the docs rubric\n"
    "Every changed file is documentation. Review it for **internal consistency, "
    "accuracy against the repository's actual state, broken cross-references, and "
    "clarity**. Documentation legitimately summarizes and points to work that lives "
    "OUTSIDE this diff (CI runs, secrets, sibling PRs, other repos). Therefore you "
    "MUST NOT raise CONCERNS of the form 'unverifiable in the diff', 'lacks CI "
    "output / test evidence', 'claims changes not shown here', or 'references work "
    "outside this diff' — demanding in-diff proof of an external fact is NOT a valid "
    "concern for a docs PR. Raise CONCERNS only for statements that CONTRADICT the "
    "repository's actual state, broken/dead references, or genuinely incoherent prose."
)


_REVIEWER_VERDICT_STATUS_CONTEXT = "reviewer-verdict"


def _branch_protection_ok(gh_client, owner: str, repo: str, base_branch: str, settings) -> bool:
    """Self-merge gate (surface 3): verify the fleet's own merge is actually
    constrained, instead of trusting an out-of-repo GitHub setting it can't see.

    Returns True (allow merge) unless ``reviewer.require_branch_protection`` is
    set AND protection is missing/misconfigured. Requires the reviewer-verdict
    context to be a required status check and admin enforcement to be on — so the
    self-issued verdict is not the ONLY thing standing between an attacker-pushed
    head and main. Fail-CLOSED here is intentional: when the operator opts in, an
    unverifiable protection state must refuse the merge, not wave it through.
    """
    reviewer = getattr(settings, "reviewer", None)
    if reviewer is None or not getattr(reviewer, "require_branch_protection", False):
        return True
    try:
        protection = gh_client.get_branch_protection(owner, repo, base_branch)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "pr_review_watcher: branch-protection check failed repo=%s/%s branch=%s — %s; "
            "refusing self-merge (require_branch_protection=True)",
            owner,
            repo,
            base_branch,
            exc,
        )
        return False
    if not protection:
        logger.error(
            "pr_review_watcher: %s/%s branch %r has NO protection but "
            "require_branch_protection=True — refusing self-merge",
            owner,
            repo,
            base_branch,
        )
        return False
    rsc = protection.get("required_status_checks") or {}
    contexts = set(rsc.get("contexts") or [])
    for check in rsc.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            contexts.add(check["context"])
    enforce_admins = bool((protection.get("enforce_admins") or {}).get("enabled"))
    missing = []
    if _REVIEWER_VERDICT_STATUS_CONTEXT not in contexts:
        missing.append(f"required check {_REVIEWER_VERDICT_STATUS_CONTEXT!r}")
    if not enforce_admins:
        missing.append("enforce_admins")
    if missing:
        logger.error(
            "pr_review_watcher: %s/%s branch %r protection insufficient (%s) — refusing self-merge",
            owner,
            repo,
            base_branch,
            ", ".join(missing),
        )
        return False
    return True


def _sensitive_path_ack_ok(
    gh_client,
    owner: str,
    repo: str,
    pr_number: int,
    pr_data: dict,
    settings,
) -> bool:
    """Opt-in blast-radius gate (mirrors ``_branch_protection_ok``'s shape).

    Returns True (allow merge) unless ``reviewer.require_sensitive_path_ack`` is
    set AND the PR's diff touches a sensitive path without an operator
    'risk-reviewed' ack label. The ack is an encode-once root action (label the
    PR), never a per-correction approval — the fleet still produced the LGTM; this
    only adds a merge precondition it cannot satisfy by editing code, so a
    sensitive PR is left for the operator instead of looping a fix.

    Fail-OPEN on its own errors (cannot list files): a gate that can't evaluate
    must not wedge a clean LGTM merge on a GitHub API hiccup — the operator opted
    into scrutiny, not into a brittle block.
    """
    reviewer = getattr(settings, "reviewer", None)
    if reviewer is None or not getattr(reviewer, "require_sensitive_path_ack", False):
        return True
    for lab in pr_data.get("labels") or []:
        name = lab.get("name", "") if isinstance(lab, dict) else str(lab)
        if name.startswith("risk-reviewed"):
            return True  # operator acked once at the root
    try:
        files = gh_client.list_pr_files(owner, repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pr_review_watcher: sensitive-path gate could not list files for PR #%d "
            "— %s; allowing merge (fail-open)",
            pr_number,
            exc,
        )
        return True
    hits = sensitive_paths_in_diff(files, sensitive_path_patterns())
    if hits:
        logger.error(
            "pr_review_watcher: PR #%d touches sensitive paths without a 'risk-reviewed' ack: %s",
            pr_number,
            ", ".join(sorted(hits)[:10]),
        )
        return False
    return True


def _publish_reviewer_verdict(
    gh_client,
    owner: str,
    repo: str,
    sha: str | None,
    *,
    result: str,
    description: str,
) -> None:
    """Publish the reviewer's verdict as a commit status on the PR head SHA.

    This makes the (otherwise comment-only) verdict a first-class status check
    so it can be marked *required* in branch protection — closing the gap where
    a manual ``gh pr merge`` bypasses an unresolved CONCERNS verdict. Until the
    reviewer posts ``success`` (LGTM), the context is ``failure``/absent and the
    merge is blocked, for the fleet and humans alike.

    Best-effort: a status-post failure must never crash the review loop.
    """
    if not sha:
        return
    try:
        gh_client.set_commit_status(
            owner,
            repo,
            sha,
            state=result,
            context=_REVIEWER_VERDICT_STATUS_CONTEXT,
            description=description,
        )
    except Exception as exc:  # noqa: BLE001 — status publishing is best-effort
        logger.warning(
            "pr_review_watcher: failed to publish %s status on %s — %s",
            _REVIEWER_VERDICT_STATUS_CONTEXT,
            sha[:8],
            exc,
        )


def _merge_and_done(
    state: dict,
    state_path: Path,
    _pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    settings,
    *,
    reason: str,
) -> None:
    pr_number = state["pr_number"]
    # get_mergeable() returns None while GitHub is still computing; treat that as
    # "unknown, try anyway" so we don't hold up clean PRs during GitHub's lazy eval.
    # False means a real conflict with the base — LAZY auto-rebase fires here, and
    # ONLY here (verdict is already LGTM): never eagerly per-poll, which would storm
    # every conflicting PR each time main moves.
    if gh_client.get_mergeable(owner, repo, pr_number) is False:
        _auto_rebase_or_escalate(state, state_path, gh_client, owner, repo, settings, reason)
        return
    state["rebase_attempts"] = 0  # mergeable — clear any rebase bookkeeping
    # Self-merge gate (surface 3): refuse to self-issue the verdict + REST-merge
    # unless branch protection actually constrains the fleet's own merge. No-op
    # unless reviewer.require_branch_protection is set.
    base_branch = (_pr_data.get("base") or {}).get("ref") or "main"
    if not _branch_protection_ok(gh_client, owner, repo, base_branch, settings):
        logger.error(
            "pr_review_watcher: PR #%d not self-merged — branch protection gate "
            "failed; leaving for operator",
            pr_number,
        )
        return  # leave state file — operator must inspect
    # Sensitive-path ack gate (opt-in; blast-radius scrutiny). No-op unless
    # reviewer.require_sensitive_path_ack is set. Keeps the human at the
    # encode-once root (a 'risk-reviewed' label), never the per-correction loop.
    if not _sensitive_path_ack_ok(gh_client, owner, repo, pr_number, _pr_data, settings):
        logger.error(
            "pr_review_watcher: PR #%d not self-merged — sensitive-path ack gate "
            "failed; leaving for operator",
            pr_number,
        )
        return  # leave state file — operator must inspect/ack
    # CI-GREEN PRECONDITION. The fleet self-merges via the REST merge API plus the
    # self-issued reviewer-verdict published just below, which together clear branch
    # protection WITHOUT GitHub enforcing the OTHER required checks — so the reviewer
    # must verify CI itself. A PR is green only when nothing has FAILED *and* nothing
    # is still PENDING: a queued/in_progress run has no conclusion yet, so
    # get_failed_checks alone cannot see it. Refusing here is what stops the fleet
    # from self-merging red (the hole that landed #405 and #406 with red pytest/perf).
    _repo_cfg_ci = (
        settings.repos.get(state["repo_key"]) if getattr(settings, "repos", None) else None
    )
    _ci_ignored = list(getattr(_repo_cfg_ci, "ci_ignored_checks", []) or [])
    _ci_failed = (
        gh_client.get_failed_checks(
            owner, repo, pr_number, pr_data=_pr_data, ignored_checks=_ci_ignored
        )
        or []
    )
    _ci_pending = (
        gh_client.get_incomplete_checks(
            owner, repo, pr_number, pr_data=_pr_data, ignored_checks=_ci_ignored
        )
        or []
    )
    if _ci_failed or _ci_pending:
        logger.warning(
            "pr_review_watcher: PR #%d NOT merged — CI not green (failed=%s pending=%s); "
            "re-evaluated next poll (reason=%s)",
            pr_number,
            _ci_failed,
            _ci_pending,
            reason,
        )
        return  # leave state file — re-checked next poll once CI settles
    # Bless this head with reviewer-verdict=success BEFORE merging, so the
    # required status check is satisfied for the fleet's own merge — and so the
    # non-LGTM merge paths (e.g. ci_validated_after_retraction) also clear the
    # gate. GitHub records the status synchronously; a brief propagation lag at
    # most causes one retry on the next poll.
    _publish_reviewer_verdict(
        gh_client,
        owner,
        repo,
        _pr_head_sha(_pr_data),
        result="success",
        description=f"reviewer approved ({reason})",
    )
    # Choose the method BEFORE the try that guards merging. Inside it, any
    # failure while *selecting* a method aborted the *merge* — the PR silently
    # did not merge and only logged an error. Selection must never be able to
    # block merging: a wrong-but-safe method is recoverable, a merge that never
    # happens is not.
    _method = _merge_method_for(gh_client, owner, repo, _pr_head_sha(_pr_data))
    try:
        gh_client.merge_pr(owner, repo, pr_number, merge_method=_method)
        logger.info(
            "pr_review_watcher: merged PR #%d repo=%s reason=%s",
            pr_number,
            state["repo_key"],
            reason,
        )
    except Exception as exc:
        logger.error("pr_review_watcher: merge failed PR #%d — %s", pr_number, exc)
        return  # leave state file — operator must inspect

    _retract_flag(state, gh_client, owner, repo, resolution="PR merged")

    plane_task_id = state.get("plane_task_id")
    if plane_task_id:
        try:
            client = _plane_client(settings)
            try:
                client.transition_issue(plane_task_id, "Done")
                client.comment_issue(plane_task_id, f"PR #{pr_number} merged ({reason})")
            finally:
                client.close()
        except Exception as exc:
            logger.warning(
                "pr_review_watcher: Plane Done failed task_id=%s — %s", plane_task_id, exc
            )

    state_path.unlink(missing_ok=True)


def _auto_rebase_or_escalate(
    state: dict,
    state_path: Path,
    gh_client,
    owner: str,
    repo: str,
    settings,
    reason: str,
) -> None:
    """LGTM PR is CONFLICTING — try one bounded, grace-gated auto-rebase.

    On a clean rebase we push the merge commit and STOP for this cycle: CI
    re-runs on the merged tree and the next review re-validates it before any
    merge to main. This is the backstop for a textually-clean-but-semantically
    -wrong merge (broken import, budget overflow, silent hunk loss) that the
    bot's ephemeral clone would not catch via local pre-push hooks. A real
    conflict escalates for a human; we never force a resolution."""
    pr_number = state["pr_number"]
    state.setdefault("rebase_attempts", 0)

    # Grace: main moves constantly; re-rebasing within the window thrashes.
    last = state.get("last_rebase_at")
    if last:
        try:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < _REBASE_GRACE_SECONDS:
                logger.info(
                    "pr_review_watcher: PR #%d CONFLICTING but rebased %ds ago — "
                    "deferring (main may be moving)",
                    pr_number,
                    int(elapsed),
                )
                return
        except ValueError:
            pass

    if state["rebase_attempts"] >= _MAX_REBASE_ATTEMPTS:
        _escalate_needs_human(
            state,
            state_path,
            gh_client,
            owner,
            repo,
            settings,
            reason="rebase_attempts_exhausted",
            detail=(
                f"PR is CONFLICTING and {_MAX_REBASE_ATTEMPTS} auto-rebase attempts did "
                "not yield a mergeable branch (base may be moving faster than CI/review, "
                "or the conflict recurs). Needs a manual rebase."
            ),
        )
        return

    head_ref = (state.get("head_ref") or "").strip()
    if not head_ref:
        logger.warning(
            "pr_review_watcher: PR #%d CONFLICTING but no head_ref recorded — cannot rebase",
            pr_number,
        )
        return

    repo_cfg = settings.repos.get(state["repo_key"])
    outcome = _attempt_auto_rebase(repo_cfg, head_ref, settings, pr_number)
    # last_rebase_at gates the grace window on every *attempt* (success or not),
    # so a fast-moving base cannot trigger back-to-back rebases.
    state["last_rebase_at"] = datetime.now(UTC).isoformat()

    if outcome == "clean":
        state["rebase_attempts"] += 1
        logger.info(
            "pr_review_watcher: PR #%d auto-rebased onto base (attempt %d/%d) — pushed; "
            "CI will re-run and review re-validates next cycle before merge",
            pr_number,
            state["rebase_attempts"],
            _MAX_REBASE_ATTEMPTS,
        )
        _save_state(state_path, state)
    elif outcome == "conflict":
        _escalate_needs_human(
            state,
            state_path,
            gh_client,
            owner,
            repo,
            settings,
            reason="rebase_conflict",
            detail=(
                "Auto-rebase onto the base branch hit a real code conflict "
                "(beyond the union-merged journal). Manual rebase required."
            ),
        )
    else:
        # noop / push_rejected / unavailable / error — log and retry next cycle
        # (grace window throttles). Not charged against rebase_attempts: no merge
        # commit landed, so it is not a consumed attempt.
        logger.info(
            "pr_review_watcher: PR #%d auto-rebase outcome=%s — will retry next cycle",
            pr_number,
            outcome,
        )
        _save_state(state_path, state)


# ── Fix pass + close/re-queue ─────────────────────────────────────────────────

# How many times an issue may be re-queued for a fresh attempt before it is
# left Blocked for a human. Bounds the close→re-queue→new-PR cycle so an
# unfixable issue can't loop forever, while still never merging half-finished.
_MAX_REQUEUES = 3

# How many polls a PR may wait for red CI to go green before it is escalated to
# a human (rather than deferring forever — a persistently-red required check
# must not silently stall the loop, nor merge on red).
_MAX_CI_WAIT_CYCLES = 20

# WO-3: when a PR is escalated (same head, no new push) but CI is fully green,
# retract the escalation and allow the reviewer to re-evaluate. Bounded to
# prevent infinite escalation→retraction loops on PRs whose concerns cannot be
# resolved by automation alone (e.g. diff-truncation false positives).
# 3 allows recovery from: rebase_conflict + ci_never_settled + one genuine
# concern cycle, without enabling runaway loops.
_MAX_CI_GREEN_RETRACTIONS = 3
_DIFF_LIMIT = 60_000

# A reviewer_backend_unavailable escalation is TRANSIENT infra (session limit,
# crash) — the backend recovers on its own, so the park auto-expires after this
# cooldown and autonomous review resumes without a human or a new push.
_BACKEND_UNAVAILABLE_RESUME_S = 3600


def _run_fix_pass(
    oc_root: Path,
    config_path: Path,
    repo_key: str,
    head_ref: str,
    concerns: str,
    settings,
    *,
    state_key: str,
    extra_context: str = "",
) -> bool:
    """Dispatch a worker pass that resolves review concerns on the PR's own
    branch and pushes (updating the open PR). Returns True only if the pass
    actually pushed changes to the branch — a no-op pass (worker couldn't
    resolve anything) returns False so the caller can log it; the next review
    cycle re-evaluates the actual diff regardless.

    The goal enumerates the concerns and carries the anti-no-op acceptance bar
    (tests passing is necessary but not sufficient; a tested-but-unwired symbol
    must be wired, not re-tested). ``extra_context`` carries the Self-Heal
    Ladder's per-rung enrichment (e.g. the PR diff, or "the previous pass
    changed nothing — take a different approach")."""
    goal_text = _build_fix_goal(concerns, extra_context=extra_context)
    try:
        outcome = _run_pipeline(
            oc_root,
            config_path,
            repo_key,
            goal_text,
            settings,
            source="reviewer_fix",
            state_key=state_key,
            branch_suffix=f"{state_key[:12]}",
            task_branch=head_ref,
            return_result=True,
        )
    except OCSourceTreeUncleanError as exc:
        # Environment problem, not a fix-pass failure — skip this sweep, no churn.
        logger.error("pr_review_watcher: fix pass skipped — %s", exc)
        return False
    if not isinstance(outcome, dict):
        return False
    result = outcome.get("result")
    if not isinstance(result, dict):
        result = outcome
    # Only "branch_pushed" proves the diff changed. result.success is True even
    # for a no-op pass (executor ran cleanly but committed nothing), which would
    # mask a worker that resolved nothing — don't count that as a push.
    return bool(result.get("branch_pushed"))


# Dedicated label for reviewer re-queues — kept separate from board_worker's
# `retry-count` (executor-kill/transient retries) so the two budgets don't
# consume each other.
_REQUEUE_LABEL_PREFIX = "reviewer-requeue-count"


def _label_names(labels: list) -> list[str]:
    out = []
    for lab in labels or []:
        name = (lab.get("name", "") if isinstance(lab, dict) else str(lab)).strip()
        if name:
            out.append(name)
    return out


def _requeue_count(labels: list) -> int:
    raw = _label_value(labels, _REQUEUE_LABEL_PREFIX)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _labels_with_requeue_count(
    labels: list, count: int, *, extra: list[str] | None = None
) -> list[str]:
    """Full replacement label set: existing names minus the old requeue-count
    (and any in *extra*, to avoid dupes), plus the new count and *extra*."""
    drop = {_REQUEUE_LABEL_PREFIX.lower()} | {e.lower() for e in (extra or [])}
    kept = [n for n in _label_names(labels) if n.split(":", 1)[0].strip().lower() not in drop]
    return kept + [f"{_REQUEUE_LABEL_PREFIX}: {count}", *(extra or [])]


def _retract_flag(
    state: dict,
    gh_client,
    owner: str,
    repo: str,
    *,
    resolution: str,
) -> None:
    """Strike-through any open escalation or self-review flag comments.

    Edits the stored comment in-place so operators can see the flag was
    automatically cleared and why, rather than it silently persisting on a
    merged or resumed PR.
    """
    pr_number = state["pr_number"]
    for key in ("escalation_comment_id", "concerns_comment_id"):
        comment_id = state.pop(key, None)
        if not comment_id:
            continue
        try:
            comments = gh_client.list_pr_comments(owner, repo, pr_number)
            body = next((c["body"] for c in comments if c.get("id") == comment_id), None)
            if body is None:
                continue
            new_body = re.sub(
                r"\*\*(Needs human attention|Self-review concerns)\*\*",
                r"~~**\1**~~",
                body,
                count=1,
            )
            new_body = f"> **Resolved**: {resolution}\n\n" + new_body
            gh_client.update_comment(owner, repo, comment_id, new_body)
            logger.info(
                "pr_review_watcher: retracted flag comment %d for PR #%d (%s)",
                comment_id,
                pr_number,
                resolution,
            )
        except Exception as exc:
            logger.warning(
                "pr_review_watcher: failed to retract flag comment %d for PR #%d — %s",
                comment_id,
                pr_number,
                exc,
            )


def _escalate_needs_human(
    state: dict,
    state_path: Path,
    gh_client,
    owner: str,
    repo: str,
    settings,
    *,
    reason: str,
    detail: str,
    current_head_sha: str | None = None,
) -> None:
    """Leave the PR OPEN and flag it for a human. Used when the PR must not be
    merged (unresolved) but also must not be closed (work would be lost) — e.g.
    the review pipeline is persistently unavailable, or there is no Plane task
    to re-queue. Comments exactly once, then keeps polling."""
    pr_number = state["pr_number"]
    if current_head_sha:
        state["escalated_head_sha"] = current_head_sha
    state["escalated_reason"] = reason
    state["escalated_at_utc"] = datetime.now(UTC).isoformat()
    if not state.get("escalated_needs_human"):
        marker = settings.reviewer.bot_comment_marker
        try:
            resp = gh_client.post_comment(
                owner,
                repo,
                pr_number,
                f"{marker}\n**Needs human attention** (reason=`{reason}`). Left open — "
                f"not merged (unresolved) and not closed (work preserved).\n\n"
                f"{sanitize_for_comment(detail)}",
            )
            state["escalation_comment_id"] = (resp or {}).get("id")
        except Exception as exc:
            logger.warning(
                "pr_review_watcher: failed to post needs-human comment PR #%d — %s", pr_number, exc
            )
        state["escalated_needs_human"] = True
        logger.warning(
            "pr_review_watcher: PR #%d escalated for human attention (reason=%s)", pr_number, reason
        )
    _save_state(state_path, state)


def _close_and_requeue(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    settings,
    *,
    reason: str,
    detail: str,
    concerns: str = "",
) -> None:
    """Close a PR WITHOUT merging and re-queue its issue for a fresh attempt.

    The verdict gate's escape hatch: when a PR cannot reach LGTM it is never
    merged half-finished. Re-queue happens FIRST — the PR is only closed once
    the issue is safely back in the queue, so a Plane outage can't lose the
    work. With no Plane task to re-queue, the PR is left open + escalated rather
    than closed into the void.

    ``concerns`` carries the still-unresolved review concerns onto the re-queued
    task so the fresh attempt is scoped to what actually remained (Self-Heal
    Ladder Phase 3) instead of starting blind."""
    pr_number = state["pr_number"]
    plane_task_id = state.get("plane_task_id")

    if not plane_task_id:
        logger.warning(
            "pr_review_watcher: PR #%d has no Plane task — escalating instead of closing "
            "(closing would lose the work)",
            pr_number,
        )
        _escalate_needs_human(
            state,
            state_path,
            gh_client,
            owner,
            repo,
            settings,
            reason=f"{reason}:no_task",
            detail=detail,
        )
        return

    # Re-queue first; only close if it succeeded.
    if not _requeue_plane_task(
        settings, plane_task_id, pr_number=pr_number, reason=reason, concerns=concerns
    ):
        logger.warning(
            "pr_review_watcher: re-queue failed for PR #%d — leaving PR open, will retry",
            pr_number,
        )
        _save_state(state_path, state)
        return

    try:
        spec_file = _record_close_receipt(
            settings,
            plane_task_id,
            pr_number=pr_number,
            pr_data=pr_data,
            reason=reason,
        )
    except Exception as exc:
        logger.warning(
            "pr_review_watcher: failed to record close receipt for PR #%d — %s",
            pr_number,
            exc,
        )
        _save_state(state_path, state)
        return
    if not spec_file:
        logger.warning(
            "pr_review_watcher: PR #%d missing spec linkage for close receipt — leaving PR open",
            pr_number,
        )
        _save_state(state_path, state)
        return

    marker = settings.reviewer.bot_comment_marker
    close_comment = (
        f"{marker}\n**Closing without merge** (reason=`{reason}`). A PR is never "
        f"merged with unresolved review concerns — the issue has been re-queued for "
        f"a fresh attempt. Durable receipt recorded on Plane task `{plane_task_id}` "
        f"for `{_durable_pr_head_ref(pr_number)}` and `{spec_file}`.\n\n"
        f"{sanitize_for_comment(detail)}"
    )
    if not close_without_receipt_allowed(comment=close_comment, durable_receipt_recorded=True):
        logger.error(
            "pr_review_watcher: invariant rejected close for PR #%d despite recorded receipt",
            pr_number,
        )
        _save_state(state_path, state)
        return
    try:
        gh_client.post_comment(owner, repo, pr_number, close_comment)
    except Exception as exc:
        logger.warning(
            "pr_review_watcher: failed to comment before close PR #%d — %s", pr_number, exc
        )
    try:
        gh_client.close_pr(owner, repo, pr_number)
        logger.info("pr_review_watcher: closed PR #%d without merge (reason=%s)", pr_number, reason)
    except Exception as exc:
        # Issue is already re-queued; the open PR is gated from double-claim by
        # OPEN_PR_GATE. Keep state so the close is retried next cycle.
        logger.error("pr_review_watcher: failed to close PR #%d — %s", pr_number, exc)
        _save_state(state_path, state)
        return

    # Delete the head branch so closed-PR branches don't accumulate as orphans.
    head_ref = (pr_data.get("head") or {}).get("ref") or ""
    if head_ref and branch_delete_allowed_after_close(
        comment=close_comment,
        durable_receipt_recorded=True,
    ):
        try:
            gh_client.delete_branch(owner, repo, head_ref)
        except Exception as exc:
            logger.warning(
                "pr_review_watcher: failed to delete branch %s for PR #%d — %s",
                head_ref,
                pr_number,
                exc,
            )
    elif head_ref:
        logger.warning(
            "pr_review_watcher: retained branch %s for PR #%d because close comment "
            "still claims preserved work",
            head_ref,
            pr_number,
        )

    _retract_flag(state, gh_client, owner, repo, resolution="PR closed and re-queued")
    state_path.unlink(missing_ok=True)


def _requeue_plane_task(
    settings, plane_task_id: str, *, pr_number: int, reason: str, concerns: str = ""
) -> bool:
    """Send the issue back to the queue for a fresh attempt, bounded by
    ``_MAX_REQUEUES`` (its own dedicated label); once exhausted, leave it
    Blocked for a human. Returns True if the issue was handled (re-queued or
    blocked), False on failure (e.g. Plane unreachable) so the caller can keep
    the PR open and retry.

    ``concerns`` (the still-unresolved review concerns) is appended to the
    re-queue/blocked comment so the next attempt is scoped to what actually
    remained — the closed PR's branch is gone, but its lesson is not."""
    from operations_center.entrypoints.board_worker.labels import STATE_BLOCKED, STATE_READY

    # Structured, enumerated concerns for the next attempt to address — the same
    # parse the fix pass uses, so the carry-forward reads consistently.
    scope_block = ""
    items = _structure_concerns(concerns)
    if items:
        # The concerns are model-authored and may carry attacker text (a quoted
        # evidence_span). Sanitize before reflecting to the Plane comment (INJ G-1).
        enumerated = sanitize_for_comment("\n".join(f"{i}. {c}" for i, c in enumerate(items, 1)))
        scope_block = (
            "\n\n**Unresolved review concerns to address in the next attempt** "
            "(the previous PR could not resolve these — scope the fresh attempt to "
            f"them, do not start blind):\n\n{enumerated}"
        )

    try:
        client = _plane_client(settings)
    except Exception as exc:
        logger.warning(
            "pr_review_watcher: cannot open Plane client to re-queue task=%s — %s",
            plane_task_id,
            exc,
        )
        return False
    try:
        issue = client.fetch_issue(plane_task_id)
        labels = issue.get("labels", []) or []
        attempts = _requeue_count(labels)
        if attempts >= _MAX_REQUEUES:
            client.update_issue_labels(
                plane_task_id, _labels_with_requeue_count(labels, attempts, extra=["needs-human"])
            )
            client.transition_issue(plane_task_id, STATE_BLOCKED)
            client.comment_issue(
                plane_task_id,
                f"PR #{pr_number} closed ({reason}); re-queue limit "
                f"({_MAX_REQUEUES}) reached — blocked for human review.{scope_block}",
            )
            logger.warning("pr_review_watcher: task=%s hit re-queue limit — Blocked", plane_task_id)
        else:
            client.update_issue_labels(
                plane_task_id, _labels_with_requeue_count(labels, attempts + 1)
            )
            client.transition_issue(plane_task_id, STATE_READY)
            client.comment_issue(
                plane_task_id,
                f"PR #{pr_number} closed ({reason}); re-queued for a fresh attempt "
                f"(#{attempts + 1} of {_MAX_REQUEUES}).{scope_block}",
            )
            logger.info(
                "pr_review_watcher: re-queued task=%s to Ready (attempt %d)",
                plane_task_id,
                attempts + 1,
            )
        return True
    except Exception as exc:
        logger.warning("pr_review_watcher: re-queue failed task=%s — %s", plane_task_id, exc)
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


# ── Spec + Custodian context helpers ─────────────────────────────────────────


def _load_campaign_spec(oc_root: Path, settings, plane_task_id: str | None) -> str:
    """Return the campaign spec text for a Plane task, or '' if unavailable."""
    if not plane_task_id:
        return ""
    try:
        client = _plane_client(settings)
        try:
            issue = client.fetch_issue(plane_task_id)
        finally:
            client.close()
        labels = issue.get("labels", []) or []
        campaign_id = _label_value(labels, "campaign-id")
        if not campaign_id:
            return ""
        from operations_center.spec_author.state import CampaignStateManager

        campaigns_state = CampaignStateManager().load()
        for campaign in campaigns_state.active_campaigns():
            if campaign.campaign_id == campaign_id:
                spec_path = Path(campaign.spec_file)
                if not spec_path.is_absolute():
                    spec_path = oc_root / spec_path
                if spec_path.exists():
                    return spec_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("pr_review_watcher: spec lookup failed — %s", exc)
    return ""


def _custodian_findings(oc_root: Path, repo_key: str, settings) -> str:
    """Run custodian-multi on the repo's local path and return findings text.

    Silently returns '' when Custodian is unavailable or the repo has no local
    clone configured — so the review falls back to diff-only assessment.
    """
    repo_cfg = settings.repos.get(repo_key)
    local_path = getattr(repo_cfg, "local_path", None) if repo_cfg else None
    if not local_path or not Path(local_path).exists():
        return ""
    custodian_bin = oc_root / ".venv" / "bin" / "custodian-multi"
    if not custodian_bin.exists():
        return ""
    try:
        proc = subprocess.run(
            [str(custodian_bin), "--repos", str(local_path), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (proc.stdout or "").strip()
        if not output:
            return ""
        results = json.loads(output)
        # INJ Phase 1 (D-INJ-3): emit only {detector_id, count} — NEVER the raw
        # path/line/`message`, which are attacker-authored repo content laundered
        # through a *trusted* channel (the single strongest attack found). The
        # count is deterministic tool output (trusted); the reviewer re-derives
        # which lines from the diff, which is itself fenced as untrusted.
        counts: dict[str, int] = {}
        for repo_result in results:
            for det in repo_result.get("detectors") or []:
                fs = det.get("findings") or []
                if fs:
                    code = str(det.get("code", "?"))
                    counts[code] = counts.get(code, 0) + len(fs)
        if counts:
            summary = ", ".join(f"{code}×{n}" for code, n in sorted(counts.items()))
            return (
                "Custodian finding counts on current branch (verify each is "
                f"resolved by the diff): {summary}"
            )
    except Exception as exc:
        logger.debug("pr_review_watcher: custodian check failed — %s", exc)
    return ""


# ── Phase 0: ci_fix ──────────────────────────────────────────────────────────

_MAX_CI_FIX_ATTEMPTS = 3
_CI_FIX_WAIT_SECONDS = 120  # wait after pushing before re-checking CI

# How many polls phase 0 waits for CI to SETTLE (nothing failing, nothing
# running, something reported) before giving up and reviewing anyway. Bounded
# because "no check has reported" is indistinguishable from "this repo has no
# CI" without a configured required_checks list, and a repo without CI must not
# park forever. Advancing costs a review pass, never a merge: the merge gate
# re-checks CI independently.
_MAX_CI_SETTLE_CYCLES = 10

# Checks whose failure we fix with a local ruff codemod (deterministic, no agent).
_AUTOFIX_CHECK_NAMES = {"lint (ruff)", "ruff", "lint"}

# The `audit` CI check runs `custodian-multi --fail-on-findings`. Its failures are
# NOT codemod-fixable (a custodian T2 finding — a test with no assert — can't be
# fixed by `custodian fix`; it needs an agent to add a real assertion). So `audit`
# is routed to the agent-based fix pass (the SAME machinery as self_review
# concerns), kept DISTINCT from the ruff codemod path above. Match by check-name
# prefix the way GitHub reports it (e.g. "audit", "audit (custodian): failure").
_AUDIT_CHECK_NAMES = {"audit"}


def _is_audit_check(check_name: str) -> bool:
    cn = str(check_name).lower().split(":")[0].strip()
    return cn in _AUDIT_CHECK_NAMES or any(cn.startswith(k) for k in _AUDIT_CHECK_NAMES)


def _custodian_audit_findings(repo_cfg, oc_root: Path) -> list[str]:
    """Per-finding custodian detail for the repo's local clone, as a list of
    ``"<code>: <path:line: message>"`` strings — the exact lines the `audit` CI
    check (``custodian-multi --fail-on-findings``) would report.

    These are DETERMINISTIC tool output (the structured ``findings[].sample``
    field of ``custodian-multi --json``), not free-form model text, so they are
    safe to hand to the fix pass as concrete fix instructions. Returns ``[]``
    (NOT an error) when custodian is unavailable, the repo has no local clone, or
    anything fails — the caller treats an empty list as "can't enumerate" and
    falls back to self_review, so this never makes the reviewer worse than today.
    """
    local_path = getattr(repo_cfg, "local_path", None) if repo_cfg else None
    if not local_path or not Path(local_path).exists():
        return []
    custodian_bin = oc_root / ".venv" / "bin" / "custodian-multi"
    if not custodian_bin.exists():
        return []
    try:
        proc = subprocess.run(
            [str(custodian_bin), "--repos", str(local_path), "--json", "--fail-on-findings"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (proc.stdout or "").strip()
        if not output:
            return []
        results = json.loads(output)
        findings: list[str] = []
        for repo_result in results:
            for finding in repo_result.get("findings") or []:
                code = str(finding.get("code", "?"))
                sample = str(finding.get("sample", "")).strip()
                findings.append(f"{code}: {sample}" if sample else code)
        return findings
    except Exception as exc:  # noqa: BLE001 — enumeration must never raise into the loop
        logger.warning("pr_review_watcher: custodian audit enumeration failed — %s", exc)
        return []


def _format_audit_concerns(findings: list[str]) -> str:
    """Render custodian findings as an enumerated concern list for the fix pass.

    The leading directive frames them as the `audit` (custodian) CI gate so the
    agent resolves the ROOT cause (e.g. adds a real assertion for a T2 finding,
    not a `# noqa`). Output flows through ``_build_fix_goal`` →
    ``_structure_concerns`` which splits this bulleted list back into one
    addressable concern per finding.
    """
    bullets = "\n".join(f"- {f}" for f in findings)
    return (
        "The `audit` CI check (custodian-multi --fail-on-findings) is failing on "
        "this PR's branch with the following findings. Resolve the ROOT CAUSE of "
        "each in the code (for example, a `T2` finding means a test function has "
        "no assert — add a real, meaningful assertion that exercises the behavior "
        "under test; do NOT silence the detector with a noqa/ignore). After "
        "fixing, re-run `custodian-multi --repos . --fail-on-findings` and confirm "
        "it is clean:\n\n" + bullets
    )


class _CIStatus(NamedTuple):
    """The CI picture for one PR head, and the single definition of "green".

    Four automated decisions turn on whether CI is green: advancing out of
    ``ci_fix``, retracting a CI-green escalation, the self-review
    precondition,
    and the no-progress merge. Each had re-derived its own answer, and the same
    defect — treating "nothing has failed" as "everything has passed" — was
    fixed three times in the self-review gate (#269, #405/#406, #503) without
    reaching the other three. This type is that gate's rule, extracted, so
    there is one place to be right.

    A head is green only when all five hold:

    * ``failed`` empty — no check reported a failure.
    * ``pending`` empty — nothing is still queued or running. No failure YET
      is not a pass.
    * ``completed`` non-empty — CI has actually reported on THIS head. A
      freshly pushed or auto-rebased head has no results at all, and
      empty-failed plus empty-pending would otherwise read as green on a
      commit CI never saw.
    * ``missing_required`` empty — every configured required check
      registered. A required job in a separate workflow that has not started
      yet is invisible to both ``failed`` and ``pending``.
    * ``error`` is None — the query itself succeeded.

    ``error`` is a field rather than an empty ``failed`` list because those
    two were indistinguishable: the old helper returned an empty list from its
    except branch, so an API outage was reported as a green build.
    """

    failed: list[str]
    pending: list[str]
    completed: list[str]
    missing_required: list[str]
    error: str | None

    @property
    def green(self) -> bool:
        """True only when CI has SETTLED on this head with everything passing."""
        return not (self.error or self.failed or self.pending or self.missing_required) and bool(
            self.completed
        )

    @property
    def why_not_green(self) -> str:
        """A short, specific reason — for logs that must not say "CI green" vaguely."""
        if self.error:
            return f"CI query failed ({self.error}) — unknown, not green"
        if self.failed:
            return f"{len(self.failed)} failing: {', '.join(self.failed[:5])}"
        if self.pending:
            return f"{len(self.pending)} still running: {', '.join(self.pending[:5])}"
        if not self.completed:
            return "no checks have reported on the current head yet"
        if self.missing_required:
            return f"required checks not registered: {', '.join(self.missing_required)}"
        return "green"


def _ci_status(
    gh_client,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    pr_data: dict | None = None,
    ignored: list[str] | None = None,
    required: list[str] | None = None,
) -> _CIStatus:
    """Fetch failed/pending/completed checks for a PR head as one settled answer.

    Fails CLOSED: any exception from the client is captured in error and the
    result is never green. Callers must not treat an errored status as red
    either — there is nothing to fix — they should wait and ask again.
    """
    ignored = list(ignored or [])
    required = list(required or [])
    try:
        failed = list(
            gh_client.get_failed_checks(
                owner, repo, pr_number, pr_data=pr_data, ignored_checks=ignored
            )
            or []
        )
        pending = list(
            gh_client.get_incomplete_checks(
                owner, repo, pr_number, pr_data=pr_data, ignored_checks=ignored
            )
            or []
        )
        completed = list(
            gh_client.get_completed_checks(
                owner, repo, pr_number, pr_data=pr_data, ignored_checks=ignored
            )
            or []
        )
    except Exception as exc:  # noqa: BLE001 - any client error means "unknown"
        return _CIStatus(
            failed=[],
            pending=[],
            completed=[],
            missing_required=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    missing_required = [
        rc for rc in required if not any(rc.lower() in name.lower() for name in completed)
    ]
    return _CIStatus(
        failed=failed,
        pending=pending,
        completed=completed,
        missing_required=missing_required,
        error=None,
    )


def _phase0_audit_autofix(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    oc_root: Path,
    config_path: Path,
    settings,
    *,
    failed: list[str],
    attempts: int,
) -> None:
    """Fix a PR failing the `audit` (custodian) CI check via the agent-based fix
    pass — the SAME machinery (`_run_fix_pass`) the reviewer uses for self_review
    concerns, which clones the PR branch into a throwaway executor workspace (it
    never touches the live ``local_path`` checkout), has the agent edit the code,
    and re-pushes the PR branch.

    Bounds (this can NEVER loop forever):
      - Shares the ``ci_fix_attempts`` budget capped at ``_MAX_CI_FIX_ATTEMPTS``.
        On exhaustion → advance to self_review (today's fall-through) AND post a
        PR comment listing the unresolved custodian findings (escalation /
        visibility), so a stuck custodian PR is never silently abandoned.
      - The post-push ``_CI_FIX_WAIT_SECONDS`` gate (enforced by the caller)
        spaces attempts out so CI can re-run between them.

    Fail-safe: on ANY error (custodian can't enumerate, dispatch raises, no head
    ref) → log a WARNING and advance to self_review. Never worse than today.
    """
    pr_number = int(state["pr_number"])
    repo_key = state["repo_key"]
    repo_cfg = settings.repos.get(repo_key)
    head_ref = ((pr_data.get("head") or {}).get("ref") or "").strip()

    # Enumerate the actual findings up front — both the dispatch (fix instructions)
    # and the exhaustion comment (escalation) need them. Empty means we couldn't
    # enumerate (custodian unavailable / errored): there is nothing concrete to
    # hand the agent, so fall back rather than dispatch a blind pass.
    findings = _custodian_audit_findings(repo_cfg, oc_root)

    if attempts >= _MAX_CI_FIX_ATTEMPTS:
        # Exhausted: advance to self_review (today's behavior) + post escalation.
        logger.info(
            "pr_review_watcher: PR #%d exhausted %d audit fix attempts — advancing to "
            "self_review and posting unresolved-findings comment",
            pr_number,
            attempts,
        )
        if findings:
            try:
                marker = getattr(settings.reviewer, "bot_comment_marker", "")
                # findings are deterministic custodian tool output, but defang
                # belt-and-suspenders before reflecting to GitHub (a `sample` line
                # echoes a repo path/symbol name that is ultimately repo content).
                body = (
                    f"{marker}\n"
                    f"**Audit auto-fix exhausted** — the `audit` (custodian) check is still "
                    f"failing after {attempts} automated fix attempt(s). These findings "
                    f"remain unresolved and need a human:\n\n"
                    + sanitize_for_comment("\n".join(f"- {f}" for f in findings))
                )
                gh_client.post_comment(owner, repo, pr_number, body)
            except Exception as exc:  # noqa: BLE001 — comment is best-effort visibility
                logger.warning(
                    "pr_review_watcher: PR #%d failed to post audit-exhausted comment — %s",
                    pr_number,
                    exc,
                )
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    if not head_ref or not findings:
        # No head ref (can't push a fix) or nothing enumerable → fall back safely.
        logger.warning(
            "pr_review_watcher: PR #%d audit auto-fix unavailable (head_ref=%r, "
            "%d findings) — advancing to self_review",
            pr_number,
            head_ref,
            len(findings),
        )
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    # Pre-save the attempt counter BEFORE the (potentially long) agent pass so the
    # budget survives a restart mid-dispatch — exactly like the ruff path records
    # its attempt only on a successful push, but here we charge the attempt up
    # front because the agent pass is expensive and a crash mid-pass must still
    # count toward the cap (otherwise a repeatedly-crashing pass loops forever).
    state["ci_fix_attempts"] = attempts + 1
    state["ci_fix_last_push_at"] = datetime.now(UTC).isoformat()
    _save_state(state_path, state)

    concerns = _format_audit_concerns(findings)
    logger.info(
        "pr_review_watcher: PR #%d audit failing (%s) — dispatching agent fix pass "
        "%d/%d on branch %s (%d findings)",
        pr_number,
        [c for c in failed if _is_audit_check(c)],
        attempts + 1,
        _MAX_CI_FIX_ATTEMPTS,
        head_ref,
        len(findings),
    )
    try:
        pushed = _run_fix_pass(
            oc_root,
            config_path,
            repo_key,
            head_ref,
            concerns,
            settings,
            state_key=state["state_key"],
        )
    except Exception as exc:  # noqa: BLE001 — dispatch failure must never wedge the loop
        logger.warning("pr_review_watcher: PR #%d audit fix dispatch error — %s", pr_number, exc)
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    if not pushed:
        logger.warning(
            "pr_review_watcher: PR #%d audit fix pass pushed no changes (attempt %d/%d)",
            pr_number,
            attempts + 1,
            _MAX_CI_FIX_ATTEMPTS,
        )
    # Stay in ci_fix: the caller's _CI_FIX_WAIT_SECONDS gate defers the next sweep
    # so CI re-runs on the pushed commit; the next pass re-enumerates findings and
    # either sees green (→ self_review) or charges the next bounded attempt.


def _phase0_ci_fix(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    oc_root: Path,
    settings,
    config_path: Path | None = None,
) -> None:
    """Phase 0: if CI is failing on an autonomy PR, push an auto-fix and wait.

    Two fix paths, both bounded by ``ci_fix_attempts``/``_MAX_CI_FIX_ATTEMPTS``:
      - ruff lint failures → a deterministic local ``ruff --fix`` codemod.
      - ``audit`` (custodian) failures → the agent-based fix pass (same machinery
        as self_review concerns), since custodian findings aren't codemod-fixable.

    Transitions to self_review when CI is green or when attempts are exhausted.
    ``config_path`` is required for the audit agent-fix path (it dispatches the
    executor pipeline); when None that path degrades to the prior behavior
    (audit failures fall through to self_review).
    """
    pr_number = int(state["pr_number"])
    repo_key = state["repo_key"]
    repo_cfg = settings.repos.get(repo_key)
    if not repo_cfg:
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    ignored = list(getattr(repo_cfg, "ci_ignored_checks", []) or [])
    required = list(getattr(repo_cfg, "required_checks", []) or [])
    ci = _ci_status(
        gh_client,
        owner,
        repo,
        pr_number,
        pr_data=pr_data,
        ignored=ignored,
        required=required,
    )

    if ci.green:
        logger.info(
            "pr_review_watcher: PR #%d CI settled green on this head, advancing to self_review",
            pr_number,
        )
        state["phase"] = "self_review"
        state.pop("ci_settle_cycles", None)
        _save_state(state_path, state)
        return

    if not ci.failed:
        # Not green, but nothing has FAILED either: the query errored, checks are
        # still running, nothing has reported on this head, or a required check
        # has not registered. None of those is fixable by a fix pass, and none of
        # them is evidence of green — which is exactly what this branch used to
        # claim. Wait, bounded, and log the actual reason.
        cycles = int(state.get("ci_settle_cycles", 0)) + 1
        state["ci_settle_cycles"] = cycles
        if cycles < _MAX_CI_SETTLE_CYCLES:
            logger.info(
                "pr_review_watcher: PR #%d CI not settled-green (%s, wait %d/%d) — "
                "deferring self_review",
                pr_number,
                ci.why_not_green,
                cycles,
                _MAX_CI_SETTLE_CYCLES,
            )
            _save_state(state_path, state)
            return
        logger.warning(
            "pr_review_watcher: PR #%d CI never settled after %d polls (%s) — advancing to "
            "self_review anyway; the merge gate still re-checks CI, so this cannot merge red",
            pr_number,
            cycles,
            ci.why_not_green,
        )
        state["phase"] = "self_review"
        state.pop("ci_settle_cycles", None)
        _save_state(state_path, state)
        return

    state.pop("ci_settle_cycles", None)
    failed = ci.failed

    # If we pushed a fix recently, wait for CI to re-run before acting again.
    last_push = state.get("ci_fix_last_push_at")
    if last_push:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(last_push)).total_seconds()
        if elapsed < _CI_FIX_WAIT_SECONDS:
            logger.debug(
                "pr_review_watcher: PR #%d CI fix pushed %.0fs ago — waiting for CI rerun",
                pr_number,
                elapsed,
            )
            return

    attempts = state.get("ci_fix_attempts", 0)

    # --- audit (custodian) failures → agent-based fix pass ---------------------
    # Routed BEFORE the generic attempt-cap and the ruff codemod path: custodian
    # findings (e.g. a T2 "test with no assert") are not codemod-fixable, so they
    # go to the SAME agent-fix machinery the reviewer uses for self_review
    # concerns. The branch owns its own exhaustion handling (escalation comment),
    # so it must run before the shared cap check below. Gated on the opt-out
    # setting and on having a config_path to dispatch the pipeline; either absent
    # degrades to the prior fall-through-to-self_review behavior.
    audit_failing = [c for c in failed if _is_audit_check(c)]
    if (
        audit_failing
        and getattr(settings, "reviewer_autofix_audit", False)
        and config_path is not None
    ):
        _phase0_audit_autofix(
            state,
            state_path,
            pr_data,
            gh_client,
            owner,
            repo,
            oc_root,
            config_path,
            settings,
            failed=failed,
            attempts=attempts,
        )
        return

    if attempts >= _MAX_CI_FIX_ATTEMPTS:
        logger.info(
            "pr_review_watcher: PR #%d exhausted %d CI fix attempts (%s still failing) "
            "— advancing to self_review",
            pr_number,
            attempts,
            failed,
        )
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    # Only auto-fix checks we know how to handle; skip if unknown failures dominate.
    # get_failed_checks may return names like "Lint (ruff): failure" — match by prefix.
    def _is_fixable(check_name: str) -> bool:
        cn = check_name.lower().split(":")[0].strip()
        return cn in _AUTOFIX_CHECK_NAMES or any(cn.startswith(k) for k in _AUTOFIX_CHECK_NAMES)

    fixable_failing = [c for c in failed if _is_fixable(c)]
    unfixable = [c for c in failed if not _is_fixable(c)]
    if unfixable and not fixable_failing:
        logger.info(
            "pr_review_watcher: PR #%d CI failing on non-auto-fixable checks %s "
            "— advancing to self_review",
            pr_number,
            unfixable,
        )
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    # --- Attempt auto-fix on the local repo checkout ---
    local_path = getattr(repo_cfg, "local_path", None)
    head_ref = ((pr_data.get("head") or {}).get("ref") or "").strip()
    if not local_path or not head_ref:
        state["phase"] = "self_review"
        _save_state(state_path, state)
        return

    local_path = Path(local_path)
    venv_bin = local_path / (getattr(repo_cfg, "venv_dir", ".venv") or ".venv") / "bin"
    ruff_bin = venv_bin / "ruff"
    if not ruff_bin.exists():
        system_ruff = shutil.which("ruff")
        if system_ruff:
            ruff_bin = Path(system_ruff)
        else:
            oc_ruff = oc_root / ".venv" / "bin" / "ruff"
            ruff_bin = oc_ruff if oc_ruff.exists() else Path("ruff")

    git_env = dict(os.environ)
    git_token = settings.git_token()
    author_name = getattr(settings.git, "author_name", "Operations Center Bot")
    author_email = getattr(settings.git, "author_email", "operations-center-bot@example.com")
    git_env["GIT_AUTHOR_NAME"] = author_name
    git_env["GIT_AUTHOR_EMAIL"] = author_email
    git_env["GIT_COMMITTER_NAME"] = author_name
    git_env["GIT_COMMITTER_EMAIL"] = author_email
    if git_token:
        git_env["GH_TOKEN"] = git_token

    try:
        # ISOLATION (security): never stash/checkout/pull/commit/push in
        # local_path — for OC's own repo that is the LIVE running checkout. All
        # mutating git work happens in a throwaway worktree at the PR head; the
        # primary checkout's HEAD/index/working tree/stash are never touched.
        with _isolated_repo_checkout(local_path, head_ref, git_env) as (cwd, _git):
            # Run ruff auto-fix in the isolated worktree (ruff_bin stays absolute
            # to the primary venv; only the working directory is the worktree).
            subprocess.run(
                [str(ruff_bin), "check", "--fix", "."], cwd=cwd, capture_output=True, text=True
            )
            subprocess.run([str(ruff_bin), "format", "."], cwd=cwd, capture_output=True, text=True)

            # Check if anything changed.
            status = _git("status", "--porcelain")
            if not status.stdout.strip():
                logger.info(
                    "pr_review_watcher: PR #%d ruff fix produced no changes "
                    "— advancing to self_review",
                    pr_number,
                )
                state["phase"] = "self_review"
                _save_state(state_path, state)
                return

            _git("add", "-A")
            _git("commit", "-m", f"fix(ci): auto-fix ruff lint violations on {head_ref}")
            push = _git("push", "origin", head_ref)
            if push.returncode != 0:
                logger.warning(
                    "pr_review_watcher: PR #%d ci-fix push failed — %s",
                    pr_number,
                    push.stderr.strip(),
                )
                state["phase"] = "self_review"
                _save_state(state_path, state)
                return

            state["ci_fix_attempts"] = attempts + 1
            state["ci_fix_last_push_at"] = datetime.now(UTC).isoformat()
            logger.info(
                "pr_review_watcher: PR #%d ci-fix attempt %d pushed to %s — waiting for CI",
                pr_number,
                attempts + 1,
                head_ref,
            )
            _save_state(state_path, state)

    except Exception as exc:
        logger.warning("pr_review_watcher: PR #%d ci_fix error — %s", pr_number, exc)
        state["phase"] = "self_review"
        _save_state(state_path, state)


# ── Phase 1: self-review ──────────────────────────────────────────────────────

# Helper functions for adaptive CI wait logic (Stage 2: prevent false human-parks)


def _compute_backoff_interval(backoff_level: int) -> int:
    """Compute exponential backoff interval in seconds.

    Level 0: 5s, Level 1: 10s, Level 2: 20s, Level 3+: 20s (max)
    """
    intervals = [5, 10, 20, 20, 20]
    return intervals[min(backoff_level, len(intervals) - 1)]


def _update_check_history(
    state: dict,
    failed_checks: list[str],
    completed_checks: list[str],
    current_head_sha: str,
) -> None:
    """Track check outcomes to distinguish transient from stuck checks.

    Updates state["ci_check_history"] with per-check tracking:
    - last_seen_sha: last head where this check reported
    - first_registration_at: when first seen (ISO timestamp)
    - times_passed: cumulative pass count
    - times_failed: cumulative fail count
    """
    history = state.setdefault("ci_check_history", {})
    now = datetime.now(UTC).isoformat()

    for check in completed_checks:
        if check not in history:
            history[check] = {
                "last_seen_sha": current_head_sha,
                "first_registration_at": now,
                "times_passed": 0,
                "times_failed": 0,
            }
        history[check]["last_seen_sha"] = current_head_sha
        history[check]["last_seen_time"] = now

        if check not in failed_checks:
            history[check]["times_passed"] += 1
        else:
            history[check]["times_failed"] += 1


def _should_escalate_ci_wait(
    state: dict,
    missing_required: list[str],
    failed_checks: list[str],
    ci_wait_cycles_first_registration: int = 60,
    ci_wait_cycles_already_seen: int = 40,
    ci_flakiness_threshold_pct: int = 30,
    required_checks_configured: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Determine if CI wait should escalate to human.

    Applies decision criteria to distinguish transient failures from unresolvable.
    Returns: (should_escalate, reason_code)
    """
    history = state.get("ci_check_history", {})
    wait_cycles = state.get("ci_wait_cycles", 0)
    required_configured = required_checks_configured or []

    # Criterion 1: Has this check ever completed?
    never_seen = [c for c in missing_required if c not in history]
    already_seen = [c for c in missing_required if c in history]

    if never_seen:
        # Distinguish between "misconfigured" and "late-registering"
        misconfigured = [c for c in never_seen if c not in required_configured]
        late_registering = [c for c in never_seen if c in required_configured]

        # Misconfigured checks: escalate quickly (old behavior, ~20 cycles)
        if misconfigured and wait_cycles >= 20:
            return (True, "ci_misconfigured_check")

        # Late-registering checks: use longer timeout
        if late_registering and wait_cycles >= ci_wait_cycles_first_registration:
            return (True, "ci_never_settled_late_registration")

        return (False, None)

    # Criterion 3: Is the failure pattern sparse or dense?
    for check in already_seen:
        ch = history[check]
        total_attempts = ch["times_passed"] + ch["times_failed"]
        failure_rate = ch["times_failed"] / total_attempts if total_attempts > 0 else 0

        if failure_rate >= ci_flakiness_threshold_pct / 100:
            # Dense failure pattern = stuck check, not transient
            if wait_cycles < ci_wait_cycles_already_seen:
                continue  # Give it more time
            else:
                return (True, "ci_persistently_red_dense_failure")

    # All checks either passed or are below escalation threshold
    if wait_cycles >= ci_wait_cycles_already_seen:
        return (True, "ci_never_settled_threshold_exceeded")

    return (False, None)


def _classify_missing_checks(
    state: dict,
    missing_required: list[str],
    required_checks_configured: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Classify missing required checks into three categories.

    Returns: (never_registered, late_registering, stuck)
    - never_registered: not in configured required checks (config error)
    - late_registering: configured but not yet seen (will eventually appear)
    - stuck: seen before but missing now (flaky runner or timeout)
    """
    history = state.get("ci_check_history", {})

    never_registered = [
        c for c in missing_required if c not in history and c not in required_checks_configured
    ]
    late_registering = [
        c for c in missing_required if c not in history and c in required_checks_configured
    ]
    stuck_checks = [c for c in missing_required if c in history]

    return (never_registered, late_registering, stuck_checks)


def _normalize_concerns_signature(summary: str) -> str:
    """Create a normalized signature for concern deduplication.

    Removes timestamps, specific line numbers, variable names to identify
    the same logical concern across multiple review passes.
    """
    import hashlib

    text = str(summary or "").strip()
    if not text:
        return ""
    # Replace numbers with N to normalize variable names, line numbers, etc.
    normalized = re.sub(r"\d+", "N", text)
    # Use hash for compact signature
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _track_concern_raised(
    state: dict,
    summary: str,
    current_head_sha: str,
) -> None:
    """Record that a concern was raised on this head.

    Tracks concern history for the improved retraction guard: tracks when
    concerns were first raised, to prevent retraction if unfixed concerns exist.
    """
    history = state.setdefault("concern_history", {})
    signature = _normalize_concerns_signature(summary)

    if signature and (
        signature not in history or history[signature].get("head_sha") != current_head_sha
    ):
        history[signature] = {
            "head_sha": current_head_sha,
            "summary": summary,
            "raised_at": datetime.now(UTC).isoformat(),
            "fix_attempts_on_this_concern": 0,
        }
    state["last_concerns_head_sha"] = current_head_sha
    state["last_concerns_summary"] = summary


def _can_escalate_concern(
    state: dict,
    summary: str,
    current_head_sha: str,
) -> bool:
    """Prevent escalation of the same concern multiple times without real fix attempt.

    Returns False if same concern signature escalated ≥ 2 times without new head.
    """
    history = state.get("concern_history", {})
    signature = _normalize_concerns_signature(summary)

    if signature and signature in history:
        prev_rec = history[signature]
        # If same concern on same head → already escalated
        if prev_rec.get("head_sha") == current_head_sha:
            prev_count = prev_rec.get("fix_attempts_on_this_concern", 0)
            if prev_count >= 2:  # Already tried fixing twice on this concern
                return False  # Don't escalate again

    return True


def _phase1(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    oc_root: Path,
    config_path: Path,
    settings,
) -> None:
    pr_number = int(state["pr_number"])
    repo_key = state["repo_key"]
    state_key = state["state_key"]
    reviewer = settings.reviewer
    current_head_sha = _pr_head_sha(pr_data)

    previous_concerns_head_sha = str(state.get("last_concerns_head_sha") or "").strip()
    # Only reset the fix/escalation budget when the head moved because of an
    # EXTERNAL push (a human, or another host) — that is genuinely new work to
    # review fresh. When the head moved because OUR OWN fix pass pushed it, the
    # budget must keep accumulating, otherwise every self-pushed fix resets the
    # counter and the PR loops forever instead of escalating to a human (the
    # #334 non-convergence: 7 self-pushes, fix_attempts stuck at 1, piling on
    # evidence files). last_fix_push_sha is the head our last fix pass produced.
    last_fix_push_sha = str(state.get("last_fix_push_sha") or "").strip()
    # Restart-safe recognition of our own fix-push. In steady state the head
    # matches the SHA we recorded after the pass (last_fix_push_sha). But if the
    # watcher was interrupted BETWEEN the fix-push and that recording — e.g. a
    # long fix pass kills the process (seen on #337) — last_fix_push_sha is lost
    # and a naive guard would mistake our own push for an external one and reset
    # the budget (re-opening the #334 loop). Recover from durable state that
    # survives the pre-fix save: when we have an active fix cycle
    # (`fix_attempts > 0`) but the pass outcome was never recorded
    # (`last_fix_pass_pushed` is popped at dispatch start and only re-set when the
    # pass completes), a head move is almost certainly that interrupted pass's
    # push — treat it as ours. (A poll never observes this mid-dispatch: the
    # dispatch is synchronous within one poll, so the unrecorded state is only
    # ever seen after a restart.)
    _fix_dispatch_unrecorded = (
        state.get("fix_attempts", 0) > 0 and "last_fix_pass_pushed" not in state
    )
    _is_our_fix_push = (
        bool(last_fix_push_sha) and current_head_sha == last_fix_push_sha
    ) or _fix_dispatch_unrecorded
    if (
        state.get("concerns_comment_id")
        and current_head_sha
        and previous_concerns_head_sha
        and current_head_sha != previous_concerns_head_sha
        and not _is_our_fix_push
    ):
        _retract_flag(
            state, gh_client, owner, repo, resolution="superseded by new push — re-review resumed"
        )
        state["fix_attempts"] = 0
        state.pop("last_concerns_summary", None)
        state.pop("last_concerns_head_sha", None)
        state.pop("last_fix_pass_pushed", None)
        state.pop("last_fix_push_sha", None)
        state.pop("fix_strategy_level", None)  # new code → start back at L0
        logger.info(
            "pr_review_watcher: PR #%d head changed after concerns (external push); "
            "resetting fix state",
            pr_number,
        )
        _save_state(state_path, state)

    # Transient-infra escalations auto-expire: a backend session limit resets
    # on its own, so resume autonomous review after a cooldown instead of
    # parking the PR until a human or a new push arrives.
    if (
        state.get("escalated_needs_human")
        and state.get("escalated_reason") == "reviewer_backend_unavailable"
    ):
        raw_ts = str(state.get("escalated_at_utc") or "")
        try:
            escalated_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            escalated_at = None

        # F14 park-cap: a COUNCIL park (unmet cross-family quorum) is capped
        # independently of the generic 1h backend-unavailable resume above.
        # Without this, the 1h auto-resume clears the escalation, _run_council
        # immediately re-parks on the same unmet quorum (the general resume
        # window is far shorter than a real capacity outage), and the pair
        # repeats forever with no operator-visible signal. council_park_started_at
        # is set ONCE by _run_council on first park and is NOT reset by this
        # block's resume/re-park cycling, so the cap tracks total time parked,
        # not time since the most recent re-park.
        _council_capped_hold = False
        if state.get("council_park"):
            cap_hours = getattr(settings.reviewer.council, "max_council_park_hours", 24)
            started_raw = str(state.get("council_park_started_at") or raw_ts)
            try:
                started_at = datetime.fromisoformat(started_raw)
            except ValueError:
                started_at = escalated_at
            if (
                started_at is not None
                and (datetime.now(UTC) - started_at).total_seconds() >= cap_hours * 3600
            ):
                _council_capped_hold = True
                if state.get("escalated_reason") != "council_unavailable_capped":
                    detail = (
                        f"Reviewer council has been parked (insufficient cross-family "
                        f"quorum) for over {cap_hours}h — this is no longer a transient "
                        "backend cooldown. Surfacing distinctly instead of continuing to "
                        "silently auto-resume and re-park."
                    )
                    state["escalated_needs_human"] = False  # force a fresh, distinct comment
                    _escalate_needs_human(
                        state,
                        state_path,
                        gh_client,
                        owner,
                        repo,
                        settings,
                        reason="council_unavailable_capped",
                        detail=detail,
                        current_head_sha=current_head_sha,
                    )
                    logger.error(
                        "pr_review_watcher: PR #%d council park capped at %dh; escalated "
                        "distinctly (council_unavailable_capped)",
                        pr_number,
                        cap_hours,
                    )

        if not _council_capped_hold and (
            escalated_at is None
            or (datetime.now(UTC) - escalated_at).total_seconds() >= _BACKEND_UNAVAILABLE_RESUME_S
        ):
            _retract_flag(
                state,
                gh_client,
                owner,
                repo,
                resolution="backend cooldown elapsed — automated review resumed",
            )
            state["escalated_needs_human"] = False
            state.pop("escalated_head_sha", None)
            state.pop("escalated_reason", None)
            state.pop("escalated_at_utc", None)
            state["backend_error_passes"] = 0
            state.pop("council_park", None)
            state.pop("council_park_started_at", None)
            logger.info(
                "pr_review_watcher: PR #%d backend-unavailable park expired; "
                "resuming automated review",
                pr_number,
            )
            _save_state(state_path, state)

    # Once a PR is escalated for human attention, do not keep burning review
    # passes on the same unchanged head. Resume autonomous review only after a
    # new push changes the PR head SHA.
    if state.get("escalated_needs_human"):
        escalated_head_sha = str(state.get("escalated_head_sha") or "").strip()
        if not escalated_head_sha:
            if current_head_sha:
                # Self-heal older/corrupted state files by pinning the
                # escalation to the current head instead of re-posting the same
                # needs-human comment every sweep.
                state["escalated_head_sha"] = current_head_sha
                logger.info(
                    "pr_review_watcher: PR #%d escalated with no recorded SHA; "
                    "backfilled current head and will await change",
                    pr_number,
                )
                _save_state(state_path, state)
                return
            else:
                # No head SHA means we cannot tell whether anything changed.
                # Clear and retry once fresh PR data is available.
                state["escalated_needs_human"] = False
                state["no_verdict_passes"] = 0
                logger.info(
                    "pr_review_watcher: PR #%d escalated with no recorded SHA; clearing for retry",
                    pr_number,
                )
                _save_state(state_path, state)
        elif current_head_sha and current_head_sha != escalated_head_sha:
            _retract_flag(
                state, gh_client, owner, repo, resolution="new push — automated review resumed"
            )
            state["escalated_needs_human"] = False
            state.pop("escalated_head_sha", None)
            state["no_verdict_passes"] = 0
            logger.info(
                "pr_review_watcher: PR #%d head changed after escalation; resuming automated review",
                pr_number,
            )
            _save_state(state_path, state)
        else:
            # WO-3: if CI is green on the escalated head, the test suite has
            # validated the implementation. Retract the escalation once so the
            # reviewer can re-evaluate without a diff-truncation blind spot.
            # Bounded by _MAX_CI_GREEN_RETRACTIONS to prevent loops.
            #
            # EXCEPT when the escalation carries unresolved review concerns on
            # THIS exact (unchanged) head: CI was ALREADY green when those
            # concerns were raised, so green CI is not new information and must
            # not silently clear them. Retracting here shipped #313 — a
            # fix_pass_no_progress escalation got retracted on green CI, the
            # concerns were forgotten (last_concerns_* popped below), and a fresh
            # pass LGTM'd the same broken, CI-invisible integration. A
            # concern-based escalation waits for a real new push (changed head,
            # handled above) or a human — never for "CI is still green".
            #
            # Improved guard (Stage 2): check for unfixed concerns on ANY recent head,
            # not just the current head. This prevents retraction when a fix pass
            # pushes a new commit but the same concern still applies.
            # Also check old last_concerns_head_sha field for backward compatibility.
            _concerns_on_this_head_old = (
                bool(current_head_sha)
                and current_head_sha == str(state.get("last_concerns_head_sha") or "").strip()
            )
            _unfixed_concerns = _concerns_on_this_head_old  # Start with old check

            # Enhanced check using new concern_history for improved guard
            concern_history = state.get("concern_history", {})
            escalated_head = str(state.get("escalated_head_sha") or "").strip()

            if not _unfixed_concerns and concern_history and escalated_head:
                for concern_sig, concern_rec in concern_history.items():
                    concern_head = concern_rec.get("head_sha", "")
                    concern_raised = concern_rec.get("raised_at", "")

                    if not concern_head or not concern_raised:
                        continue

                    # Concern is "unfixed" if raised on escalated head (or any recent head within 24h)
                    try:
                        raised_time = datetime.fromisoformat(concern_raised)
                        concern_age = (datetime.now(UTC) - raised_time).total_seconds()
                        # Consider concerns raised on the escalated head or within the last 24h as unfixed
                        if concern_head == escalated_head or concern_age < 86400:
                            _unfixed_concerns = True
                            break
                    except (ValueError, TypeError):
                        pass

            _ci_green_retracted = state.get("ci_green_retraction_count", 0)
            _did_ci_green_retract = False
            if not _unfixed_concerns and _ci_green_retracted < _MAX_CI_GREEN_RETRACTIONS:
                _rcfg = settings.repos.get(repo_key)
                if _rcfg and getattr(_rcfg, "auto_merge_on_ci_green", False):
                    _rhead = ((pr_data.get("head") or {}).get("ref") or "").lower()
                    if _rhead.startswith(("goal/", "test/", "improve/", "spec-author/")):
                        _rignored = list(getattr(_rcfg, "ci_ignored_checks", []) or [])
                        _rrequired = list(getattr(_rcfg, "required_checks", []) or [])
                        try:
                            # Settled-and-green only, via the shared definition. The
                            # old test here was 'no failures and nothing pending',
                            # which a head with NO reported checks satisfies —
                            # retracting a human escalation on a build that never
                            # ran.
                            _rci = _ci_status(
                                gh_client,
                                owner,
                                repo,
                                pr_number,
                                pr_data=pr_data,
                                ignored=_rignored,
                                required=_rrequired,
                            )
                            if _rci.green:
                                _retract_flag(
                                    state,
                                    gh_client,
                                    owner,
                                    repo,
                                    resolution=(
                                        "CI green on unchanged head — test suite validates "
                                        "implementation; automated review resumed"
                                    ),
                                )
                                state["escalated_needs_human"] = False
                                state.pop("escalated_head_sha", None)
                                state["no_verdict_passes"] = 0
                                state["fix_attempts"] = 0
                                state.pop("last_concerns_summary", None)
                                state.pop("last_concerns_head_sha", None)
                                state.pop("last_fix_pass_pushed", None)
                                state["ci_green_retraction_count"] = _ci_green_retracted + 1
                                logger.info(
                                    "pr_review_watcher: PR #%d CI green on escalated head; "
                                    "retracting escalation for automated review retry "
                                    "(retraction %d/%d)",
                                    pr_number,
                                    _ci_green_retracted + 1,
                                    _MAX_CI_GREEN_RETRACTIONS,
                                )
                                _save_state(state_path, state)
                                _did_ci_green_retract = True
                        except Exception:
                            pass  # CI check failed — fall through to skip
            if not _did_ci_green_retract:
                logger.info(
                    "pr_review_watcher: PR #%d awaiting human attention or new push; "
                    "skipping automated self-review",
                    pr_number,
                )
                return

    # ── CI-green precondition ────────────────────────────────────────────────
    # For autonomy PRs on repos that opt in, green CI is a PRECONDITION for
    # merge — not a merge trigger. While CI is red we defer (ci_fix / CI will
    # resolve it) rather than burning an expensive self-review. Once CI is
    # green we fall through to the verdict-gated self-review below: LGTM is the
    # only thing that merges, so a PR is never shipped on green CI alone (green
    # CI does not prove the issue is complete — e.g. missing docs pass CI).
    repo_cfg = settings.repos.get(repo_key)
    if repo_cfg and getattr(repo_cfg, "auto_merge_on_ci_green", False):
        head_ref = ((pr_data.get("head") or {}).get("ref") or "").lower()
        is_autonomy = head_ref.startswith(("goal/", "test/", "improve/", "spec-author/"))
        if is_autonomy:
            try:
                ignored = list(getattr(repo_cfg, "ci_ignored_checks", []) or [])
                failed = gh_client.get_failed_checks(
                    owner,
                    repo,
                    pr_number,
                    pr_data=pr_data,
                    ignored_checks=ignored,
                )
                if failed:
                    # Defer while CI is red so ci_fix / CI can resolve it — but
                    # BOUND the wait. If CI never goes green (e.g. a persistently
                    # failing check), don't defer forever (that would silently
                    # stall the loop) and don't merge red — escalate for a human.
                    state["ci_wait_cycles"] = state.get("ci_wait_cycles", 0) + 1

                    # Track check history to distinguish transient failures from stuck checks
                    completed = gh_client.get_completed_checks(
                        owner,
                        repo,
                        pr_number,
                        pr_data=pr_data,
                        ignored_checks=ignored,
                    )
                    _update_check_history(state, failed, completed, current_head_sha or "")

                    # Get configured required checks for this repo
                    repo_required = (
                        list(getattr(repo_cfg, "required_checks", []) or []) if repo_cfg else []
                    )

                    # Apply adaptive thresholds based on check history
                    should_escalate, reason_code = _should_escalate_ci_wait(
                        state,
                        missing_required=failed,  # Failed checks as missing required
                        failed_checks=failed,
                        ci_wait_cycles_first_registration=60,
                        ci_wait_cycles_already_seen=40,
                        ci_flakiness_threshold_pct=30,
                        required_checks_configured=repo_required,
                    )

                    if should_escalate:
                        detail = (
                            f"CI has not gone green after {state['ci_wait_cycles']} "
                            f"checks ({len(failed)} failing: "
                            f"{', '.join(failed[:5])}). Not merged (red CI) and not "
                            f"closed (work preserved) — needs a human to fix CI."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason=reason_code or "ci_persistently_red",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason=reason_code or "ci_persistently_red",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return
                    logger.info(
                        "pr_review_watcher: PR #%d CI not green (%d failed, wait %d/40) — "
                        "deferring self-review until CI is green",
                        pr_number,
                        len(failed),
                        state["ci_wait_cycles"],
                    )
                    record_ci_gate_defer(
                        pr_number=pr_number,
                        repo_key=repo_key,
                        wait_cycle=state["ci_wait_cycles"],
                        max_cycles=40,  # Adaptive threshold for already-seen checks
                        failed_checks=failed,
                    )
                    _save_state(state_path, state)
                    return
                # No check has FAILED — but an empty failure list only means
                # "nothing has failed yet". While any check is still queued/running
                # its conclusion is None, invisible to get_failed_checks. Declaring
                # green here would let a self-review LGTM merge the PR before CI
                # finishes, turning the base branch red (this is how #269 merged red).
                # Require CI to have SETTLED (no pending checks) before green.
                pending = gh_client.get_incomplete_checks(
                    owner,
                    repo,
                    pr_number,
                    pr_data=pr_data,
                    ignored_checks=ignored,
                )
                # Guard C: the gating green must belong to the CURRENT head. An empty
                # completed-checks list means CI has produced no result on this head
                # yet (just pushed / auto-rebased) — failed==[] and pending==[] would
                # otherwise read as green on a head that has no CI at all, so a stale
                # pre-rebase green could carry a self-review LGTM straight to merge.
                completed = gh_client.get_completed_checks(
                    owner,
                    repo,
                    pr_number,
                    pr_data=pr_data,
                    ignored_checks=ignored,
                )
                # Guard D: every configured required check must be PRESENT and passing
                # on the current head. `failed` is already empty here (the failed-checks
                # branch above returned), so a required check is satisfied iff it appears
                # in `completed`. This closes the late-registering-check hole: a required
                # check living in a separate workflow that has not registered yet is
                # invisible to both failed and pending, so without this a PR could merge
                # before that check (e.g. the `audit` job) ever runs.
                required = list(getattr(repo_cfg, "required_checks", []) or [])
                missing_required = [
                    rc
                    for rc in required
                    if not any(rc.lower() in name.lower() for name in completed)
                ]
                if pending or not completed or missing_required:
                    state["ci_wait_cycles"] = state.get("ci_wait_cycles", 0) + 1

                    # Track check history for classification
                    _update_check_history(state, [], completed, current_head_sha or "")

                    if pending:
                        _why = f"{len(pending)} still running: {', '.join(pending[:5])}"
                        # Pending checks need time — use standard threshold
                        if state["ci_wait_cycles"] < 40:
                            logger.info(
                                "pr_review_watcher: PR #%d CI not settled-green on current head "
                                "(%s, wait %d/40) — deferring self-review",
                                pr_number,
                                _why,
                                state["ci_wait_cycles"],
                            )
                            _save_state(state_path, state)
                            return
                        detail = (
                            f"CI checks still pending after {state['ci_wait_cycles']} cycles: "
                            f"{_why}. Not merged (CI incomplete) and not closed (work preserved) "
                            f"— needs a human to investigate stuck CI."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason="ci_never_settled_pending",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_never_settled_pending",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return

                    if not completed:
                        _why = "no checks have reported on the current head yet"
                        # No checks yet — may just be slow to start, use longer timeout
                        if state["ci_wait_cycles"] < 60:
                            logger.info(
                                "pr_review_watcher: PR #%d CI not settled-green on current head "
                                "(%s, wait %d/60) — deferring self-review",
                                pr_number,
                                _why,
                                state["ci_wait_cycles"],
                            )
                            _save_state(state_path, state)
                            return
                        detail = (
                            f"No CI checks have reported on this head after {state['ci_wait_cycles']} "
                            f"cycles. Not merged (CI incomplete) and not closed (work preserved) "
                            f"— needs a human to investigate stuck CI."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason="ci_never_settled_no_checks",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_never_settled_no_checks",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return

                    # Classify missing required checks
                    never_reg, late_reg, stuck = _classify_missing_checks(
                        state, missing_required, required
                    )

                    if never_reg:
                        _why = f"misconfigured required checks: {', '.join(never_reg)}"
                        detail = (
                            f"Required checks not found in branch protection: {never_reg}. "
                            f"This indicates a CI configuration error. Not merged (CI incomplete) "
                            f"and not closed (work preserved) — needs a human to fix CI configuration."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason="ci_misconfigured_check",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_misconfigured_check",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return

                    if late_reg:
                        _why = f"late-registering checks: {', '.join(late_reg)}"
                        # Late-registering checks need longer timeout
                        if state["ci_wait_cycles"] < 60:
                            logger.info(
                                "pr_review_watcher: PR #%d waiting for late-registering checks "
                                "(%s, wait %d/60) — deferring self-review",
                                pr_number,
                                _why,
                                state["ci_wait_cycles"],
                            )
                            _save_state(state_path, state)
                            return
                        detail = (
                            f"Required checks have not registered after {state['ci_wait_cycles']} "
                            f"cycles: {late_reg}. Likely a workflow that requires manual trigger or "
                            f"configuration. Not merged (CI incomplete) and not closed (work preserved) "
                            f"— needs a human to investigate."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason="ci_never_settled_late_registration",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_never_settled_late_registration",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return

                    if stuck:
                        _why = f"stuck checks (previously seen): {', '.join(stuck)}"
                        # Stuck checks use standard threshold
                        if state["ci_wait_cycles"] < 40:
                            logger.info(
                                "pr_review_watcher: PR #%d CI not settled-green on current head "
                                "(%s, wait %d/40) — deferring self-review",
                                pr_number,
                                _why,
                                state["ci_wait_cycles"],
                            )
                            _save_state(state_path, state)
                            return
                        detail = (
                            f"Checks reported before but not on this head after {state['ci_wait_cycles']} "
                            f"cycles: {stuck}. Likely a flaky runner or stuck workflow. Not merged (CI "
                            f"incomplete) and not closed (work preserved) — needs a human to investigate."
                        )
                        record_escalation(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            reason="ci_never_settled_stuck_check",
                            detail=detail,
                        )
                        _escalate_needs_human(
                            state,
                            state_path,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_never_settled_stuck_check",
                            detail=detail,
                            current_head_sha=current_head_sha,
                        )
                        return

                    # Fallback (should not reach here)
                    logger.info(
                        "pr_review_watcher: PR #%d CI not settled-green on current head "
                        "(%s, wait %d/40) — deferring self-review",
                        pr_number,
                        "unknown reason",
                        state["ci_wait_cycles"],
                    )
                    _save_state(state_path, state)
                    return
                state["ci_wait_cycles"] = 0  # CI settled and green — reset the wait counter
                logger.info(
                    "pr_review_watcher: PR #%d CI green — proceeding to verdict-gated "
                    "self-review (LGTM required to merge)",
                    pr_number,
                )
            except Exception as exc:
                logger.debug("pr_review_watcher: CI check failed PR #%d — %s", pr_number, exc)

    diff = gh_client.get_pr_diff(owner, repo, pr_number)
    if not diff:
        # A pull request can be correct AND change no files: an ancestry
        # reconciliation merges an already-mirrored history back in, moving no
        # content by design. Returning here made such a PR structurally
        # unmergeable -- reviewer-verdict is a REQUIRED status and this watcher
        # is its only producer, so "skip" means "never merges" while the PR
        # looks healthy in the UI. Review it on its metadata instead.
        logger.info(
            "pr_review_watcher: PR #%d changes no files — reviewing as a no-content "
            "change rather than skipping (a skip would leave it unmergeable)",
            pr_number,
        )
        diff = (
            "(This pull request changes no files.\n"
            "It is a no-content change -- typically a merge that reconciles history\n"
            "without moving content. Judge it on its title, description and commit\n"
            "structure rather than on a diff.)\n"
        )
    if diff.startswith("[DIFF_TOO_LARGE"):
        logger.warning(
            "pr_review_watcher: PR #%d diff exceeds GitHub API limit — reviewing file list only",
            pr_number,
        )

    _pr_files = _files_from_diff(diff)
    if len(diff) > _DIFF_LIMIT:
        # Fetch the complete file list so the reviewer can verify implementation
        # completeness even when the diff body is truncated. Without this, the
        # reviewer sees only documentation changes and wrongly concludes that
        # implementation files (which sort later alphabetically) are absent.
        _pr_files = gh_client.list_pr_files(owner, repo, pr_number)
        _file_lines = (
            "\n".join(f"  {f}" for f in sorted(_pr_files))
            if _pr_files
            else "  (file list unavailable)"
        )
        diff_excerpt = (
            diff[:_DIFF_LIMIT] + f"\n\n...[diff truncated at {_DIFF_LIMIT} chars]\n\n"
            "IMPORTANT — complete list of ALL files changed in this PR "
            "(files listed here ARE modified even if their diffs are not shown above; "
            "do NOT raise 'missing implementation' concerns for files that appear here):\n"
            + _file_lines
        )
    else:
        diff_excerpt = diff
    # A documentation-only diff gets a rubric that doesn't demand in-diff proof of
    # facts a doc legitimately references but can't contain (the #334 over-flagging).
    docs_only = _diff_is_docs_only(_pr_files)
    title = pr_data.get("title", "")

    # Load optional campaign spec and Custodian findings for spec-aware review
    spec_text = _load_campaign_spec(oc_root, settings, state.get("plane_task_id"))
    custodian_text = _custodian_findings(oc_root, repo_key, settings)

    # INJ Phase 1 (outer, §2.2.5): wrap every untrusted span — PR title, diff,
    # campaign spec — in a per-run nonce fence with a system preamble. Defense-in-
    # depth only; the load-bearing control is the code-computed verdict. Custodian
    # output is already reduced to {detector_id, count} (trusted), so it is unfenced.
    nonce = make_nonce()
    spec_section = (
        "\n\n## Campaign spec (review against this — violations are CONCERNS)\n\n"
        + fence("campaign-spec", spec_text, nonce)
        if spec_text
        else ""
    )
    custodian_section = (
        f"\n\n## Custodian static analysis\n\n{custodian_text}" if custodian_text else ""
    )
    doc_rubric = _DOC_ONLY_REVIEW_RUBRIC if docs_only else ""

    # These budgets are read by BOTH review modes' post-verdict tail
    # (_dispatch_verdict_outcome) — must be set before the C1 fork below, since
    # a guardrail PR may return via _run_council without ever reaching the
    # single-review goal_text/setdefault block further down.
    state.setdefault("fix_attempts", 0)
    state.setdefault("no_verdict_passes", 0)
    state.setdefault("env_unclean_passes", 0)
    state.setdefault("backend_error_passes", 0)
    state.setdefault("no_verdict_escalation_count", 0)

    # C1 — cross-family council fork (COUNCIL_VERDICT.md G1/C1). guardrail_paths
    # defaults to [] (not a list on a bare MagicMock in older/ad-hoc settings
    # either) — isinstance-gated so the feature stays OFF unless a REAL glob
    # list is configured, never accidentally on via a truthy mock/sentinel.
    council = getattr(reviewer, "council", None)
    _raw_guardrail_paths = getattr(council, "guardrail_paths", None)
    guardrail_paths = _raw_guardrail_paths if isinstance(_raw_guardrail_paths, list) else []
    guardrail_hits = sensitive_paths_in_diff(_pr_files, guardrail_paths) if guardrail_paths else []
    if guardrail_hits and not _council_exempt_self_fix(state, pr_data):
        _run_council(
            state,
            state_path,
            pr_data,
            gh_client,
            owner,
            repo,
            oc_root,
            config_path,
            settings,
            current_head_sha=current_head_sha,
            diff=diff,
            diff_excerpt=diff_excerpt,
            title=title,
            spec_section=spec_section,
            custodian_section=custodian_section,
            guardrail_hits=guardrail_hits,
            nonce=nonce,
        )
        return

    goal_text = (
        "## TASK TYPE: Read-only code review\n"
        "## SINGLE REQUIRED ACTION: Write verdict.json — no other file changes allowed\n\n"
        f"{UNTRUSTED_PREAMBLE}\n\n"
        f"Review the following pull-request diff for correctness, style, and spec compliance.\n\n"
        f"PR #{pr_number} title: {fence('pr-title', title, nonce)}\n\n"
        f"{fence('pr-diff', diff_excerpt, nonce)}"
        f"{spec_section}"
        f"{custodian_section}"
        f"{doc_rubric}\n\n"
        f"**Review checklist** — report a status for each:\n"
        f"1. spec_compliance: if a campaign spec is provided above, the diff implements EXACTLY what\n"
        f"   it requires — correct filenames, member names, member count, exports, tests, version bumps.\n"
        f"2. custodian_findings: if Custodian findings are listed above, each is resolved by the diff.\n"
        f"3. code_quality: correctness, style, potential bugs.\n"
        f"4. no_tooling_artifacts: no .baseline-validation.json, run-status.md, etc. in the diff.\n\n"
        f"{verdict_schema_prompt()}\n\n"
        "CRITICAL: Do NOT modify any source files in the repository. "
        "Do NOT run tests, build, or push. "
        "Your ONLY permitted action is writing verdict.json to the current directory."
    )

    logger.info(
        "pr_review_watcher: self-review PR #%d repo=%s loop=%d",
        pr_number,
        repo_key,
        state["self_review_loops"],
    )

    # D1: consult the shared fleet ladder BEFORE spawning the review. The
    # reviewer is part of the fleet, so it runs the controller's claude→codex
    # LADDER instead of burning claude unconditionally:
    #   * claude runnable          → review on claude/haiku (today's path);
    #   * claude cooled, fallback  → DIVERT this ordinary review to the fallback
    #     backend (e.g. codex_cli/codex) so review keeps flowing — charges the
    #     fallback's budget, not claude's (the codex reviewer was validated safe
    #     as the ordinary single reviewer by the 2026-07-15 spike, and runs live
    #     as council seat C3);
    #   * whole ladder exhausted   → PARK (defer+return, no burn); the #446
    #     1h auto-resume retries when a backend returns.
    # Selection failure (``None``) proceeds on claude — today's fail-open behavior.
    # This is the ORDINARY single-reviewer path only; guardrail PRs already
    # forked to _run_council above and never reach here.
    _review_backend = "claude_code"
    _review_model = "haiku"
    _selection = _select_review_backend(settings)
    if _selection is not None and _selection.selected_backend != "claude_code":
        _resets = [r for r in _selection.cooldowns.values() if r is not None]
        _reset_at = max(_resets) if _resets else None
        _fallback_model = (
            _review_model_for_backend(_selection.selected_backend)
            if _selection.selected_backend is not None
            else None
        )
        if _fallback_model is None:
            # Whole ladder exhausted (no runnable backend), or the runnable
            # fallback has no known review pairing — PARK rather than burn or
            # guess a model. Not budget-charged; retries via #446 auto-resume.
            logger.info(
                "pr_review_watcher: PR #%d review DEFERRED — no runnable review "
                "backend (selected=%s, reset≈%s); not burning budget, will retry "
                "when a backend returns.",
                pr_number,
                _selection.selected_backend,
                _reset_at.isoformat() if _reset_at else "unknown",
            )
            return
        # claude cooled but a validated fallback is runnable — divert the review.
        _review_backend = _selection.selected_backend
        _review_model = _fallback_model
        logger.info(
            "pr_review_watcher: PR #%d claude cooled/over-reserve — running the "
            "ordinary review on fallback backend %s/%s (D1 ladder; charges %s "
            "budget, not claude).",
            pr_number,
            _review_backend,
            _review_model,
            _review_backend,
        )

    try:
        if _review_backend == "claude_code":
            # Claude path stays on _run_direct_review (the name the test suite
            # patches) so its back-compat contract is untouched.
            verdict = _run_direct_review(oc_root, goal_text, state_key)
        else:
            verdict = _run_member_review(
                oc_root,
                goal_text,
                state_key,
                backend=_review_backend,
                model=_review_model,
            )
    except OCSourceTreeUncleanError as exc:
        # The reviewer's own source tree is broken — this would crash for EVERY
        # PR, so it is not charged against this PR's review budget. Skip the
        # sweep loudly; a clean tree next cycle resumes review automatically.
        # Only after persistent uncleanliness do we escalate — and then with the
        # specific cause, not a misleading "no verdict / reviewer unavailable".
        state["env_unclean_passes"] += 1
        logger.error(
            "pr_review_watcher: PR #%d review SKIPPED (not budget-charged) — %s "
            "(env_unclean_pass=%d/%d)",
            pr_number,
            exc,
            state["env_unclean_passes"],
            reviewer.max_self_review_loops,
        )
        if state["env_unclean_passes"] >= reviewer.max_self_review_loops:
            _escalate_needs_human(
                state,
                state_path,
                gh_client,
                owner,
                repo,
                settings,
                reason="oc_source_tree_unclean",
                detail=str(exc),
                current_head_sha=current_head_sha,
            )
            state["env_unclean_passes"] = 0
        _save_state(state_path, state)
        return
    except ReviewerBackendError as exc:
        # The claude backend crashed, was killed, or timed out — INFRA failure,
        # not a PR quality problem.  Do NOT charge no_verdict_passes (that budget
        # measures genuine "reviewer ran but emitted no verdict", not crashes).
        state["backend_error_passes"] += 1
        logger.warning(
            "pr_review_watcher: PR #%d review SKIPPED (not budget-charged) — backend error: %s "
            "(backend_error_pass=%d/%d)",
            pr_number,
            exc,
            state["backend_error_passes"],
            reviewer.max_self_review_loops,
        )
        if state["backend_error_passes"] >= reviewer.max_self_review_loops:
            _escalate_needs_human(
                state,
                state_path,
                gh_client,
                owner,
                repo,
                settings,
                reason="reviewer_backend_unavailable",
                detail=str(exc),
                current_head_sha=current_head_sha,
            )
            state["backend_error_passes"] = 0
        _save_state(state_path, state)
        return

    state["self_review_loops"] += 1
    state["env_unclean_passes"] = 0  # a pipeline ran — tree is clean
    state["backend_error_passes"] = 0  # backend is reachable

    if verdict is None:
        # Reviewer ran cleanly (exit 0) but did not write verdict.json — a genuine
        # no-verdict (prompt or model failure).  Count against the budget.
        state["no_verdict_passes"] += 1

        # Apply exponential backoff on consecutive no-verdicts before escalating
        if state["no_verdict_passes"] < reviewer.max_self_review_loops:
            # Not yet at escalation threshold — use exponential backoff
            backoff_level = state.get("no_verdict_backoff_level", 0)
            backoff_secs = _compute_backoff_interval(backoff_level)
            state["no_verdict_backoff_level"] = min(4, backoff_level + 1)
            logger.warning(
                "pr_review_watcher: no verdict PR #%d (attempt %d/%d) — "
                "backing off %ds before retry (backoff level %d)",
                pr_number,
                state["no_verdict_passes"],
                reviewer.max_self_review_loops,
                backoff_secs,
                state["no_verdict_backoff_level"],
            )
            _save_state(state_path, state)
            return

        # Escalation threshold reached — escalate with no-verdict
        state["no_verdict_escalation_count"] += 1
        escalation_count = state["no_verdict_escalation_count"]
        state["no_verdict_backoff_level"] = 0  # Reset backoff on escalation
        logger.warning(
            "pr_review_watcher: PR #%d produced no verdict after %d passes — "
            "escalating (leaving open; reviewer unavailable, work preserved) "
            "[no_verdict_escalation_count=%d]",
            pr_number,
            state["no_verdict_passes"],
            escalation_count,
        )
        # Stuck-green detection: a PR that repeatedly reaches the no-verdict
        # escalation threshold without ever merging is likely stuck.  Emit a
        # distinct alarm so the operator (and watchdog) can see it clearly.
        _STUCK_GREEN_ESCALATION_THRESHOLD = 3
        if escalation_count >= _STUCK_GREEN_ESCALATION_THRESHOLD:
            logger.error(
                "pr_review_watcher: STUCK-GREEN PR #%d repo=%s — green on CI but "
                "unmerged after %d no-verdict escalation cycles "
                "(reason=stuck_green_repeated_failures); human review required",
                pr_number,
                repo_key,
                escalation_count,
            )
        detail = (
            "Self-review produced no parseable verdict after repeated passes "
            "(reviewer ran but emitted no verdict.json — possible prompt or model "
            "issue). The PR is left open for human attention; automated "
            "review will retry."
        )
        if escalation_count >= _STUCK_GREEN_ESCALATION_THRESHOLD:
            detail += (
                f" WARNING: this is the {escalation_count}th no-verdict escalation "
                f"for this PR (stuck-green — green on CI but review never converges). "
                f"Reason code: stuck_green_repeated_failures."
            )
        record_escalation(
            pr_number=pr_number,
            repo_key=repo_key,
            reason="no_verdict_unreviewable",
            detail=detail,
        )
        state["escalated_head_sha"] = current_head_sha or state.get("escalated_head_sha")
        _escalate_needs_human(
            state,
            state_path,
            gh_client,
            owner,
            repo,
            settings,
            reason="no_verdict_unreviewable",
            detail=detail,
            current_head_sha=current_head_sha,
        )
        state["no_verdict_passes"] = 0  # keep retrying in case it was transient
        _save_state(state_path, state)
        return

    state["no_verdict_passes"] = 0  # a verdict was produced
    state["no_verdict_backoff_level"] = 0  # Reset backoff when verdict is produced
    # INJ Phase 1 (D-INJ-1): `result` was COMPUTED BY CODE in the review member
    # (_run_member_review, on whichever backend ran — claude or the D1 codex
    # fallback) from the model's typed per-check statuses — not a model-authored
    # field. The trust boundary is identical regardless of which backend reviewed.
    # Fail-safe: a missing/malformed verdict computed to CONCERNS upstream.
    result = (verdict.get("result") or CONCERNS).upper()
    failing_checks = verdict.get("failing_checks") or []
    # The free-text summary is informational only (human comment) — NEVER the
    # decision. Prefer the code-derived failing-check list so an injected summary
    # cannot misrepresent the outcome.
    if result == CONCERNS and failing_checks:
        summary = "Failed checks: " + ", ".join(str(c) for c in failing_checks)
    else:
        summary = str(verdict.get("summary") or "(no summary)")

    _dispatch_verdict_outcome(
        state,
        state_path,
        pr_data,
        gh_client,
        owner,
        repo,
        oc_root,
        config_path,
        settings,
        result=result,
        failing_checks=failing_checks,
        summary=summary,
        current_head_sha=current_head_sha,
        diff=diff,
    )


def _dispatch_verdict_outcome(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    oc_root: Path,
    config_path: Path,
    settings,
    *,
    result: str,
    failing_checks: list,
    summary: str,
    current_head_sha: str,
    diff: str,
) -> None:
    """Shared post-verdict tail: LGTM merges; any CONCERNS feeds the existing
    fix ladder (post-once concern comment, Self-Heal Ladder, fix-pass dispatch,
    close-and-requeue on exhaustion).

    Both the single self-review path and the C1 council path call this once
    they have a final ``{result, failing_checks, summary}`` — the council
    aggregates its per-member verdicts into exactly this shape first
    (``verdict.aggregate_council``). There is ONE merge/fix-ladder/close
    implementation regardless of which review mode produced the verdict.
    """
    pr_number = int(state["pr_number"])
    repo_key = state["repo_key"]
    state_key = state["state_key"]
    reviewer = settings.reviewer
    normalized_summary = _normalize_concerns_summary(summary)

    # Track when concerns are raised (for improved retraction guard)
    if result == "CONCERNS" and current_head_sha:
        _track_concern_raised(state, summary, current_head_sha)

    logger.info("pr_review_watcher: PR #%d review verdict=%s", pr_number, result)

    # Surface the verdict as a required status check on the reviewed head, so an
    # unresolved CONCERNS verdict blocks merge for humans (manual gh pr merge)
    # too, not just the fleet's own verdict-gated path. _merge_and_done re-blesses
    # success right before merging, which also covers the non-LGTM merge paths.
    _publish_reviewer_verdict(
        gh_client,
        owner,
        repo,
        current_head_sha,
        result="success" if result == "LGTM" else "failure",
        description="reviewer LGTM" if result == "LGTM" else "reviewer concerns — auto-fixing",
    )

    if result == "LGTM":
        # The ONLY merge path on the self-review track — verdict-gated.
        record_decision_outcome(
            pr_number=pr_number,
            repo_key=repo_key,
            outcome="merge",
            reason="self_review_lgtm",
            lanes=1,
        )
        _merge_and_done(
            state, state_path, pr_data, gh_client, owner, repo, settings, reason="self_review_lgtm"
        )
        return

    # Trigger on the second no-change pass at the same head — no text comparison
    # needed.  LLM-generated summaries are not bit-identical across loops even
    # when the root issue is unchanged, so requiring text equality was
    # unreliable and caused the reviewer to spin through all max_fix_attempts
    # before escalating.
    repeated_no_progress = (
        state.get("fix_attempts", 0) > 0
        and state.get("last_fix_pass_pushed") is False
        and current_head_sha
        and current_head_sha == str(state.get("last_concerns_head_sha") or "").strip()
    )
    if repeated_no_progress:
        # WO-3 extension: when the CI-green retraction budget is exhausted and fix
        # passes still push nothing, the concerns are very likely diff-truncation
        # false positives — CI passing the full test suite is ground truth.
        # Merge rather than re-entering the escalation→retraction loop.
        if state.get("ci_green_retraction_count", 0) >= _MAX_CI_GREEN_RETRACTIONS:
            _rcfg_np = settings.repos.get(repo_key)
            _head_ref_np = ((pr_data.get("head") or {}).get("ref") or "").lower()
            if (
                _rcfg_np
                and getattr(_rcfg_np, "auto_merge_on_ci_green", False)
                and _head_ref_np.startswith(("goal/", "test/", "improve/", "spec-author/"))
            ):
                try:
                    _ign_np = list(getattr(_rcfg_np, "ci_ignored_checks", []) or [])
                    _required_np = list(getattr(_rcfg_np, "required_checks", []) or [])
                    # Only merge on CI that has SETTLED on THIS head with every
                    # required check present — the shared definition, so this path
                    # cannot drift from the self-review precondition again.
                    _ci_np = _ci_status(
                        gh_client,
                        owner,
                        repo,
                        pr_number,
                        pr_data=pr_data,
                        ignored=_ign_np,
                        required=_required_np,
                    )
                    if _ci_np.green:
                        logger.info(
                            "pr_review_watcher: PR #%d repeated no-progress after "
                            "CI-green retraction budget exhausted; CI still green — "
                            "trusting CI over truncated-diff false positives and merging",
                            pr_number,
                        )
                        record_decision_outcome(
                            pr_number=pr_number,
                            repo_key=repo_key,
                            outcome="merge",
                            reason="ci_validated_after_retraction",
                            lanes=1,
                        )
                        _merge_and_done(
                            state,
                            state_path,
                            pr_data,
                            gh_client,
                            owner,
                            repo,
                            settings,
                            reason="ci_validated_after_retraction",
                        )
                        return
                except Exception:
                    pass  # CI check failed — fall through to normal escalation

        # Self-Heal Ladder: a no-progress repeat does NOT immediately concede to
        # a human. Climb a rung — re-dispatch the fix pass with MORE resolving
        # power (L1 enriched context, L2 decompose to one concern per pass) — and
        # escalate only when the ladder tops out. Binding invariant holds: this
        # changes how hard the system TRIES, never what counts as resolved; LGTM
        # is still the only merge path. (max_fix_strategy_level=0 → old immediate
        # escalation.)
        current_level = int(state.get("fix_strategy_level", 0))
        next_level = current_level + 1
        if next_level <= reviewer.max_fix_strategy_level:
            state["fix_strategy_level"] = next_level
            logger.info(
                "pr_review_watcher: PR #%d no-progress — climbing Self-Heal Ladder to "
                "L%d/%d (re-dispatching with more resolving power, not escalating)",
                pr_number,
                next_level,
                reviewer.max_fix_strategy_level,
            )
            _save_state(state_path, state)
            # Fall through to the dispatch below, which reads fix_strategy_level.
        else:
            detail = (
                "The automated fix passes exhausted the Self-Heal Ladder (reached "
                f"L{current_level}/{reviewer.max_fix_strategy_level}) without changing "
                "the branch; a fresh self-review on the same PR head still finds "
                "concerns. Further autonomous retries would repeat without progress.\n\n"
                f"Latest concerns:\n\n{summary}"
            )
            state["escalated_head_sha"] = current_head_sha or state.get("escalated_head_sha")
            _escalate_needs_human(
                state,
                state_path,
                gh_client,
                owner,
                repo,
                settings,
                reason="fix_pass_no_progress",
                detail=detail,
                current_head_sha=current_head_sha,
            )
            return

    # CONCERNS — never merge. Dispatch a fix pass that resolves the concerns on
    # the PR's own branch (updating the PR), then re-review next cycle. After
    # max_fix_attempts without reaching LGTM, close the PR and re-queue the
    # issue for a fresh attempt — a half-finished PR is never shipped.
    if state["fix_attempts"] >= reviewer.max_fix_attempts:
        logger.warning(
            "pr_review_watcher: PR #%d still CONCERNS after %d fix attempts — "
            "closing and re-queuing",
            pr_number,
            state["fix_attempts"],
        )
        detail = (
            f"Could not resolve review concerns after {state['fix_attempts']} "
            f"fix attempts. Last concerns:\n\n{summary}"
        )
        record_decision_outcome(
            pr_number=pr_number,
            repo_key=repo_key,
            outcome="blocked",
            reason="fix_attempts_exhausted",
            lanes=1,
        )
        _close_and_requeue(
            state,
            state_path,
            pr_data,
            gh_client,
            owner,
            repo,
            settings,
            reason="fix_attempts_exhausted",
            detail=detail,
            concerns=summary,
        )
        return

    # Post the concerns once, on the first CONCERNS pass.
    if state["fix_attempts"] == 0:
        # INJ Phase 1 (§2.2.4): defang any model/untrusted text before reflecting
        # it to GitHub — no surprise @-pings or steering markdown for the next
        # reader/pass. (For the typed-verdict path `summary` is already the
        # code-derived failing-check list; sanitization is belt-and-suspenders.)
        concern_body = (
            f"{reviewer.bot_comment_marker}\n"
            f"**Self-review concerns** — auto-fixing (up to {reviewer.max_fix_attempts} "
            f"attempts; re-queued if still unresolved):\n\n{sanitize_for_comment(summary)}"
        )
        try:
            resp = gh_client.post_comment(owner, repo, pr_number, concern_body)
            state["concerns_comment_id"] = (resp or {}).get("id")
        except Exception as exc:
            logger.warning(
                "pr_review_watcher: failed to post concern comment PR #%d — %s", pr_number, exc
            )

    head_ref = (pr_data.get("head") or {}).get("ref") or ""
    if not head_ref:
        logger.error(
            "pr_review_watcher: PR #%d has no head ref — cannot dispatch fix pass; "
            "closing and re-queuing",
            pr_number,
        )
        record_decision_outcome(
            pr_number=pr_number,
            repo_key=repo_key,
            outcome="blocked",
            reason="no_head_ref",
            lanes=1,
        )
        _close_and_requeue(
            state,
            state_path,
            pr_data,
            gh_client,
            owner,
            repo,
            settings,
            reason="no_head_ref",
            detail="The PR head branch could not be determined, so concerns cannot be auto-fixed.",
        )
        return

    # Pre-save the attempt counter and head SHA before the (potentially long) fix
    # pass — if the watcher is restarted while the backend runs, the counter
    # survives. last_fix_pass_pushed is cleared until the pass completes, so
    # repeated_no_progress (which checks `is False`) won't fire on a missing key.
    state["fix_attempts"] += 1
    state["last_concerns_head_sha"] = current_head_sha
    state.pop("last_fix_pass_pushed", None)
    _save_state(state_path, state)

    # Self-Heal Ladder rung: L0 is the standard pass; higher rungs (set when a
    # no-progress repeat climbed the ladder above) enrich the dispatch with more
    # resolving power instead of conceding to a human.
    fix_level = int(state.get("fix_strategy_level", 0))
    extra_context = _ladder_enrichment(fix_level, pr_diff=diff)
    logger.info(
        "pr_review_watcher: PR #%d CONCERNS — dispatching fix pass %d/%d (ladder L%d) on branch %s",
        pr_number,
        state["fix_attempts"],
        reviewer.max_fix_attempts,
        fix_level,
        head_ref,
    )
    record_decision_outcome(
        pr_number=pr_number,
        repo_key=repo_key,
        outcome="retry",
        reason="mixed_verdicts",
        lanes=1,
    )
    pushed = _run_fix_pass(
        oc_root,
        config_path,
        repo_key,
        head_ref,
        summary,
        settings,
        state_key=state_key,
        extra_context=extra_context,
    )
    state["last_concerns_summary"] = normalized_summary
    state["last_fix_pass_pushed"] = pushed
    if pushed:
        # Record the head our own fix pass just produced, so the next poll does
        # NOT mistake it for an external push and reset the escalation budget.
        # Without this, fix_attempts never accumulates and the PR loops forever.
        try:
            state["last_fix_push_sha"] = _pr_head_sha(gh_client.get_pr(owner, repo, pr_number))
        except Exception as exc:  # noqa: BLE001 — best-effort; reset-guard degrades safe
            logger.warning(
                "pr_review_watcher: could not record fix-push head for PR #%d — %s",
                pr_number,
                exc,
            )
            state.pop("last_fix_push_sha", None)
    if not pushed:
        logger.warning(
            "pr_review_watcher: fix pass for PR #%d pushed no changes (attempt %d/%d)",
            pr_number,
            state["fix_attempts"],
            reviewer.max_fix_attempts,
        )
    # The fix pass executor can run for minutes.  An external process (e.g. the
    # watchdog) may have updated escalated_needs_human on disk while we waited.
    # Re-read the escalation flag so we don't overwrite it on save.
    try:
        disk = _load_state(state_path)
        if disk.get("escalated_needs_human") and not state.get("escalated_needs_human"):
            state["escalated_needs_human"] = True
            state["escalated_head_sha"] = disk.get("escalated_head_sha") or state.get(
                "escalated_head_sha"
            )
            logger.info(
                "pr_review_watcher: PR #%d external escalation detected after fix pass; "
                "preserving escalated_needs_human=True",
                pr_number,
            )
    except Exception:
        pass
    _save_state(state_path, state)


def _run_council(
    state: dict,
    state_path: Path,
    pr_data: dict,
    gh_client,
    owner: str,
    repo: str,
    oc_root: Path,
    config_path: Path,
    settings,
    *,
    current_head_sha: str,
    diff: str,
    diff_excerpt: str,
    title: str,
    spec_section: str,
    custodian_section: str,
    guardrail_hits: list[str],
    nonce: str,
    usage_store=None,
) -> None:
    """C1 — cross-family reviewer council for a guardrail-surface PR.

    Kept thin: the aggregation logic (unanimity, fail-safe empty-panel, union
    of failing checks, attributed summary) lives in ``verdict.aggregate_council``,
    pure and unit-tested. This orchestrator's job is only to (1) build the
    shared evidence bundle once, (2) work out which panel seats are actually
    runnable right now, (3) park on an unmet quorum instead of burning cooled
    backends, (4) dispatch the runnable seats, and (5) hand the aggregate off
    to the SAME post-verdict tail (``_dispatch_verdict_outcome``) the ordinary
    self-review path uses — one merge/fix-ladder implementation, not two.

    ``usage_store`` is injectable (mirroring ``_select_review_backend``) so
    tests can pre-load specific cooldowns; production callers omit it and get
    the real on-disk store.
    """
    pr_number = int(state["pr_number"])
    repo_key = state["repo_key"]
    state_key = state["state_key"]
    council = settings.reviewer.council
    # Defensive — _phase1 already sets these before forking here, but
    # _run_council must not assume a particular caller ordering.
    state.setdefault("fix_attempts", 0)
    state.setdefault("no_verdict_passes", 0)
    state.setdefault("env_unclean_passes", 0)
    state.setdefault("backend_error_passes", 0)
    state.setdefault("no_verdict_escalation_count", 0)

    if usage_store is None:
        from operations_center.execution.usage_store import UsageStore

        usage_store = UsageStore()
    now = datetime.now(UTC)

    available = [
        member
        for member in _COUNCIL_PANEL
        if not _member_on_cooldown(usage_store, *member[:2], now=now)
    ]
    min_members = getattr(council, "min_council_members", 3)

    if len(available) < min_members:
        cooled = sorted(
            f"{b}/{m}" for (b, m, _lens) in _COUNCIL_PANEL if (b, m, _lens) not in available
        )
        detail = (
            f"Guardrail-surface PR requires a {min_members}-member cross-family council "
            f"(COUNCIL_VERDICT.md C1); only {len(available)}/{len(_COUNCIL_PANEL)} panel seats "
            f"are runnable right now (cooled: {', '.join(cooled) or 'n/a'}). Parked — not merged "
            "(unresolved) and not closed (work preserved); automated review resumes once enough "
            "panel members recover, the PR head changes, or a human intervenes."
        )
        state["council_park"] = True
        state.setdefault("council_park_started_at", now.isoformat())
        state["council_available_members"] = len(available)
        logger.warning(
            "pr_review_watcher: PR #%d council quorum unmet (%d/%d available) — parking",
            pr_number,
            len(available),
            min_members,
        )
        _escalate_needs_human(
            state,
            state_path,
            gh_client,
            owner,
            repo,
            settings,
            reason="reviewer_backend_unavailable",
            detail=detail,
            current_head_sha=current_head_sha,
        )
        return

    # Quorum met — clear any prior park bookkeeping (F14 park-cap timer resets
    # only once the council actually runs again, not on every poll).
    state.pop("council_park", None)
    state.pop("council_park_started_at", None)
    degraded_quorum = len(available) < len(_COUNCIL_PANEL)

    base_goal_text = (
        "## TASK TYPE: Read-only code review (cross-family council seat)\n"
        "## SINGLE REQUIRED ACTION: Write verdict.json — no other file changes allowed\n\n"
        f"{UNTRUSTED_PREAMBLE}\n\n"
        "This PR touches guardrail-surface paths and is adjudicated by a cross-family "
        "review council (COUNCIL_VERDICT.md C1) instead of a single self-review — "
        "unanimous LGTM across all available seats is required to merge; any CONCERNS "
        "blocks the merge and feeds the existing auto-fix ladder.\n\n"
        f"Guardrail paths touched: {', '.join(sorted(guardrail_hits)[:10])}\n\n"
        "Review the following pull-request diff for correctness, style, and spec compliance.\n\n"
        f"PR #{pr_number} title: {fence('pr-title', title, nonce)}\n\n"
        f"{fence('pr-diff', diff_excerpt, nonce)}"
        f"{spec_section}"
        f"{custodian_section}\n\n"
        f"**Review checklist** — report a status for each:\n"
        f"1. spec_compliance: if a campaign spec is provided above, the diff implements EXACTLY what\n"
        f"   it requires — correct filenames, member names, member count, exports, tests, version bumps.\n"
        f"2. custodian_findings: if Custodian findings are listed above, each is resolved by the diff.\n"
        f"3. code_quality: correctness, style, potential bugs.\n"
        f"4. no_tooling_artifacts: no .baseline-validation.json, run-status.md, etc. in the diff.\n\n"
        f"{verdict_schema_prompt()}"
    )
    tail = (
        "\n\nCRITICAL: Do NOT modify any source files in the repository. "
        "Do NOT run tests, build, or push. "
        "Your ONLY permitted action is writing verdict.json to the current directory."
    )

    member_results: list[dict] = []
    try:
        for backend, model, lens in available:
            member_goal_text = base_goal_text + council_lens_fragment(lens) + tail
            member_state_key = f"{state_key}-council-{backend}-{model}"
            verdict = _run_member_review(
                oc_root, member_goal_text, member_state_key, backend=backend, model=model
            )
            if verdict is None:
                verdict = {
                    "result": CONCERNS,
                    "failing_checks": ["no_verdict"],
                    "summary": f"{backend}/{model} produced no parseable verdict",
                }
            member_results.append({**verdict, "backend": backend, "model": model, "lens": lens})
    except OCSourceTreeUncleanError as exc:
        state["env_unclean_passes"] = state.get("env_unclean_passes", 0) + 1
        logger.error(
            "pr_review_watcher: PR #%d council review SKIPPED (not budget-charged) — %s "
            "(env_unclean_pass=%d/%d)",
            pr_number,
            exc,
            state["env_unclean_passes"],
            settings.reviewer.max_self_review_loops,
        )
        if state["env_unclean_passes"] >= settings.reviewer.max_self_review_loops:
            _escalate_needs_human(
                state,
                state_path,
                gh_client,
                owner,
                repo,
                settings,
                reason="oc_source_tree_unclean",
                detail=str(exc),
                current_head_sha=current_head_sha,
            )
            state["env_unclean_passes"] = 0
        _save_state(state_path, state)
        return
    except ReviewerBackendError as exc:
        state["backend_error_passes"] = state.get("backend_error_passes", 0) + 1
        logger.warning(
            "pr_review_watcher: PR #%d council review SKIPPED (not budget-charged) — "
            "backend error: %s (backend_error_pass=%d/%d)",
            pr_number,
            exc,
            state["backend_error_passes"],
            settings.reviewer.max_self_review_loops,
        )
        if state["backend_error_passes"] >= settings.reviewer.max_self_review_loops:
            _escalate_needs_human(
                state,
                state_path,
                gh_client,
                owner,
                repo,
                settings,
                reason="reviewer_backend_unavailable",
                detail=str(exc),
                current_head_sha=current_head_sha,
            )
            state["backend_error_passes"] = 0
        _save_state(state_path, state)
        return

    state["env_unclean_passes"] = 0
    state["backend_error_passes"] = 0
    aggregate = aggregate_council(member_results)

    # F14 — record the audit trail: per-member backend/model/lens/verdict, the
    # aggregate, and the degraded-quorum flag. A pure audit artifact, separate
    # from the review state machine's own bookkeeping (_STATE_SUBDIR).
    _save_state(
        _council_state_path(oc_root, repo_key, pr_number),
        {
            "pr_number": pr_number,
            "repo_key": repo_key,
            "head_sha": current_head_sha,
            "panel_size": len(_COUNCIL_PANEL),
            "available_members": len(available),
            "degraded_quorum": degraded_quorum,
            "guardrail_hits": sorted(guardrail_hits),
            "result": aggregate["result"],
            "per_member": aggregate["per_member"],
            "summary": aggregate["summary"],
        },
    )

    # One PR comment summarizing every seat, attributed by backend/model/lens.
    member_lines = [
        f"- `{m['backend']}/{m['model']}` ({m['lens']}): **{m['result']}**"
        + (f" — {', '.join(m['failing_checks'])}" if m.get("failing_checks") else "")
        for m in aggregate["per_member"]
    ]
    degraded_note = (
        f"\n\n_Degraded quorum: {len(available)}/{len(_COUNCIL_PANEL)} seats available._"
        if degraded_quorum
        else ""
    )
    comment_body = (
        f"{settings.reviewer.bot_comment_marker}\n"
        f"**Council review: {aggregate['result']}** (cross-family panel, "
        f"guardrail paths: {', '.join(sorted(guardrail_hits)[:10])})\n\n"
        + "\n".join(member_lines)
        + degraded_note
        + f"\n\n{sanitize_for_comment(aggregate['summary'])}"
    )
    try:
        gh_client.post_comment(owner, repo, pr_number, comment_body)
    except Exception as exc:  # noqa: BLE001 — a comment failure must not block the merge decision
        logger.warning(
            "pr_review_watcher: failed to post council verdict comment PR #%d — %s", pr_number, exc
        )

    _dispatch_verdict_outcome(
        state,
        state_path,
        pr_data,
        gh_client,
        owner,
        repo,
        oc_root,
        config_path,
        settings,
        result=aggregate["result"],
        failing_checks=aggregate["failing_checks"],
        summary=aggregate["summary"],
        current_head_sha=current_head_sha,
        diff=diff,
    )


# ── Plane task lookup ─────────────────────────────────────────────────────────


def _find_plane_task_id(settings, repo_key: str, pr_number: int, _pr_data: dict) -> str | None:
    """Attempt to find a Plane 'In Review' task matching this PR. Best-effort."""
    try:
        client = _plane_client(settings)
        try:
            issues = client.list_issues()
        finally:
            client.close()
        for issue in issues:
            state_obj = issue.get("state")
            state_name = (state_obj.get("name", "") if isinstance(state_obj, dict) else "").strip()
            if state_name != "In Review":
                continue
            labels = issue.get("labels", [])
            if _label_value(labels, "repo") != repo_key:
                continue
            desc = issue.get("description") or issue.get("description_stripped") or ""
            if f"#{pr_number}" in desc or f"/{pr_number}" in desc:
                return str(issue["id"])
    except Exception as exc:
        logger.debug("pr_review_watcher: Plane task lookup failed — %s", exc)
    return None


# ── Heartbeat ──────────────────────────────────────────────────────────────────


def _write_heartbeat(status_dir: Path, *, success: bool = True, error: str | None = None) -> None:
    """Write the reviewer heartbeat, separating liveness from progress.

    Historically this wrote a fresh ``"active"`` heartbeat on EVERY cycle —
    including the catch-and-continue error path — so a reviewer failing every
    poll (e.g. empty GitHub token) looked perfectly healthy. Now ``success=False``
    keeps liveness (``at``) fresh but ages ``last_success_at``, so a crash-looping
    reviewer is caught by HeartbeatStallTask instead of hiding."""
    status = "active" if success else "error"
    write_heartbeat(status_dir, "review", status=status, success=success, error=error)


def _export_decision_metrics(status_dir: Path) -> None:
    """Surface the merge-decision metrics the instrumenter collects.

    pr_review_watcher records every merge decision via record_decision_outcome
    (→ the global MergeDecisionInstrumenter), but the collected metrics had no
    reader: export_metrics_json was never called. Write the instrumenter's
    summary to the status dir each cycle so the metrics are actually observable.
    Best-effort — metrics export must never break the review loop."""
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        metrics_path = status_dir / "merge_decision_metrics.json"
        metrics_path.write_text(get_instrumenter().export_metrics_json(), encoding="utf-8")
    except Exception:
        pass


# ── Poll cycle ────────────────────────────────────────────────────────────────


def _review_priority(state: dict) -> tuple[int, int, int]:
    """Sort key for the per-repo review worklist — lower sorts (and runs) first.

    Proactive ordering: a PR that may reach a terminal decision on this pass —
    a fresh self-review that could merge — must not be starved behind a PR
    already sunk into a multi-pass fix battle (each fix pass is a slow LLM run).
    Tiers:
      0  self_review, no fix attempts yet  — the quick-merge candidates
      1  ci_fix                            — bounded automated CI repair
      2  self_review already iterating     — slow fix loops, run last
    Within a tier: fewer consumed fix passes first, then PR number for a
    stable, deterministic order."""
    phase = state.get("phase", "ci_fix")
    fix_attempts = int(state.get("fix_attempts", 0))
    if phase == "self_review" and fix_attempts == 0:
        tier = 0
    elif phase == "ci_fix":
        tier = 1
    else:
        tier = 2
    return (tier, fix_attempts, int(state.get("pr_number", 0)))


def _poll_once(oc_root: Path, config_path: Path, settings) -> None:
    gh_client = _github_client(settings)

    repos_to_watch = {key: repo for key, repo in settings.repos.items() if repo.await_review}

    if not repos_to_watch:
        logger.debug("pr_review_watcher: no repos with await_review=true, nothing to do")
        return

    for repo_key, repo_cfg in repos_to_watch.items():
        try:
            owner, repo = _owner_repo(repo_cfg.clone_url)
        except Exception as exc:
            logger.warning("pr_review_watcher: bad clone_url for %s — %s", repo_key, exc)
            continue

        try:
            open_prs = gh_client.list_open_prs(owner, repo)
        except Exception as exc:
            logger.warning("pr_review_watcher: failed to list PRs %s/%s — %s", owner, repo, exc)
            continue

        # GC leftover review-state for PRs that terminated outside this watcher's
        # merge/close path (manual merge, another host, or while it was down).
        _prune_orphan_state_files(
            oc_root, repo_key, {int(p["number"]) for p in open_prs if p.get("number") is not None}
        )

        # Build the worklist (discover + load state) before processing, so the
        # sweep can be ordered. A single slow PR (a multi-pass fix battle) must
        # not push a merge-ready PR to the back of the sweep — or off it entirely
        # if the watcher restarts mid-cycle.
        worklist: list[tuple[dict, dict, Path]] = []
        for pr_data in open_prs:
            if pr_data.get("draft"):
                continue

            pr_number = int(pr_data["number"])
            sp = _state_path(oc_root, repo_key, pr_number)

            if not sp.exists():
                state = _new_state(repo_key, pr_number)
                state["plane_task_id"] = _find_plane_task_id(settings, repo_key, pr_number, pr_data)
                _save_state(sp, state)
                logger.info("pr_review_watcher: discovered PR #%d repo=%s", pr_number, repo_key)

            state = _load_state(sp)
            if not state:
                continue
            # Record the live head ref each poll (it changes across pushes) so the
            # auto-rebase path knows which branch to merge the base into.
            head_ref = (pr_data.get("head") or {}).get("ref")
            if head_ref:
                state["head_ref"] = head_ref
            worklist.append((pr_data, state, sp))

        # Proactive ordering: quick-merge candidates before slow fix loops.
        worklist.sort(key=lambda item: _review_priority(item[1]))

        for pr_data, state, sp in worklist:
            phase = state.get("phase", "ci_fix")

            # human_review is removed — any state files left over from the old
            # schema drop back to self_review so they finish autonomously.
            if phase == "human_review":
                phase = "self_review"
                state["phase"] = "self_review"

            try:
                if phase == "ci_fix":
                    _phase0_ci_fix(
                        state, sp, pr_data, gh_client, owner, repo, oc_root, settings, config_path
                    )
                elif phase == "self_review":
                    _phase1(
                        state, sp, pr_data, gh_client, owner, repo, oc_root, config_path, settings
                    )
            except (ContainmentRequiredError, EgressContainmentRequiredError) as exc:
                # Operator opted into fail-closed containment but it is unavailable
                # on this host. Skip JUST this PR — without this, the error would
                # abort the entire worklist and silently stall every other PR's
                # review/merge for the cycle.
                logger.error(
                    "pr_review_watcher: skipping PR #%s — containment required but unavailable: %s",
                    state.get("pr_number"),
                    exc,
                )
                continue


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OperationsCenter PR review watcher — two-phase state machine"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-interval-seconds", type=int, default=60, dest="poll_interval")
    parser.add_argument("--status-dir", type=Path, default=None, dest="status_dir")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [review] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    oc_root = Path(__file__).resolve().parents[4]
    status_dir = args.status_dir or (oc_root / "logs" / "local" / "watch-all")

    if not args.watch:
        try:
            settings = _load_settings(args.config)
            _poll_once(oc_root, args.config, settings)
        except Exception as exc:
            logger.error("pr_review_watcher: error — %s", exc, exc_info=True)
            _write_heartbeat(status_dir, success=False, error=str(exc))
            return 1
        _write_heartbeat(status_dir)
        _export_decision_metrics(status_dir)
        return 0

    logger.info("pr_review_watcher: starting — poll_interval=%ds", args.poll_interval)
    _log_provenance()

    # Containment self-check (audit Track A3): surface a broken posture at boot.
    for problem in verify_containment():
        logger.error(
            "pr_review_watcher: containment self-check FAILED — %s "
            '{"event": "containment_selfcheck_failed", "problem": "%s"}',
            problem,
            problem,
        )
    while True:
        try:
            settings = _load_settings(args.config)
            _poll_once(oc_root, args.config, settings)
        except Exception as exc:
            logger.error("pr_review_watcher: unhandled error — %s", exc, exc_info=True)
            # Failed cycle: liveness stays fresh, last_success_at ages → the stall
            # is detectable (this is the path that hid the 2026-06-21 outage).
            _write_heartbeat(status_dir, success=False, error=str(exc))
        else:
            _write_heartbeat(status_dir)
        _export_decision_metrics(status_dir)
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
