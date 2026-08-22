# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""SBX Phase 2 — bwrap process containment for the worker/executor subprocess.

Wraps the executor command in a rootless ``bwrap`` (bubblewrap) sandbox that:

- ``--unshare-pid`` + a fresh ``--proc /proc`` so the sandbox cannot read
  ``/proc/<parent>/environ`` (the parent still holds full ``os.environ``) — the
  red-team's "Layer 0 still leaks" defeat (HARNESS_TRUST_HARDENING.md §3.2).
- ``--clearenv`` + an explicit ``--setenv`` allowlist (the already-minimized
  worker env), so nothing leaks through inherited environment either.
- read-only system + a host-pre-built toolchain (claude/cl/uv/git/venv) and the
  Claude auth dir; **never** binds ``~/.ssh ~/.gnupg ~/.aws ~/.config/gh`` — those
  are simply absent inside.
- the task workspace is the only writable real path; ``/tmp`` and ``$HOME`` are
  fresh tmpfs.

SBX Phase 3 — network confinement (D-SBX-2 = `--share-net` + L7/SNI egress
proxy). bwrap keeps the host network namespace (``--share-net``, the default) so
the sandbox can still reach the host ollama floor and the localhost proxies; the
constraint is applied at L7 by pointing the executor's egress at the allowlist
proxy via ``HTTPS_PROXY``. ``--unshare-net`` was rejected (D-SBX-2): an isolated
netns cannot reach a host-loopback proxy / ollama without a forwarder, and an L7
proxy at least logs+constrains the deliberate-exfil case. Wiring is gated on
``OC_EGRESS_PROXY`` and is **fail-open**: if it is unset or the proxy is not
listening, no proxy env is injected and the sandbox keeps direct egress (losing
all HTTPS to a dead proxy would be a §0.1 violation). localhost (ollama floor +
key-proxy) always bypasses the proxy via ``NO_PROXY``.

CONTAINMENT POSTURE (audit Track A3): **default-on, fail-closed per task.**
The sandbox is enabled unless ``OC_BWRAP_SANDBOX=0`` and required unless
``OC_SANDBOX_REQUIRED=0``: a degrade (missing bwrap, absent workspace,
construction error) raises ``ContainmentRequiredError``, which dispatch turns
into a failed task + fault — the FLEET keeps serving (§0.1 degrade-never-halt
holds at fleet level; it is the task, not the fleet, that fails closed). An
operator who explicitly opts out with ``OC_SANDBOX_REQUIRED=0`` restores the
old observable fail-open degrade (the pre-audit posture, where a missing bwrap
silently ran the token-holding backend un-sandboxed — the exact finding).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from .containment import (  # noqa: F401 — re-exported for existing importers
    ContainmentRequiredError,
    _containment_required,
    _proxy_reachable,
    _resolve_egress_proxy,
    bwrap_available,
    sandbox_enabled,
    verify_containment,
)

logger = logging.getLogger(__name__)


def _warn_degraded(reason: str) -> None:
    """Emit an observable warning when the sandbox was ENABLED but degraded to
    un-sandboxed execution (fail-open, §0.1). Silent fail-open hides the fact that
    the nominal containment is absent at runtime — exactly the audit finding. The
    structured ``event`` key lets the log sweep / alerting key off it."""
    logger.warning(
        "sandbox_degraded: enabled but running UN-SANDBOXED (%s) "
        '{"event": "sandbox_degraded", "reason": "%s"}',
        reason,
        reason,
    )


# HOME subdirectories that hold host credentials — NEVER bound into the sandbox.
_SECRET_HOME_DIRS = (".ssh", ".gnupg", ".aws", ".config/gh", ".config/gcloud")

# The only ~/.claude entries bound read-only into the agent's (otherwise writable)
# sandbox ~/.claude: the subscription credential + settings. Everything else the
# agent writes itself (session state), so the dir must be writable.
_CLAUDE_AUTH_FILES = (".credentials.json", "settings.json")

# System directories the toolchain needs at runtime (read-only).
_RO_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

_SANDBOX_HOME = "/sandbox-home"

# Env vars that may carry the github token forwarded into the executor env
# (config token_env=GITHUB_TOKEN; GIT_TOKEN mirrors it). First non-empty wins.
_GIT_TOKEN_ENVS = ("GITHUB_TOKEN", "GIT_TOKEN")

# SBX Phase 3 (D-SBX-2): when set to the egress proxy URL in the fleet env, the
# sandbox routes HTTPS egress through the L7/SNI allowlist proxy. Unset => no
# proxy wiring (fail-open: direct net, as Phase 2).
_EGRESS_PROXY_ENV_FLAG = "OC_EGRESS_PROXY"
# localhost destinations that MUST bypass the egress proxy: the host ollama floor
# and the localhost cloud-key proxy (D-OP-1) live on loopback and are not
# "egress" — routing them through the CONNECT proxy would break the local floor.
_PROXY_BYPASS = "127.0.0.1,localhost,::1"


def _proxy_env(url: str, *, existing_no_proxy: str = "") -> dict[str, str]:
    """PURE: the proxy env vars that route the executor's egress through ``url``.
    Sets both upper- and lower-case forms (clients differ) and a ``NO_PROXY`` that
    always exempts localhost (ollama floor + key-proxy), preserving any caller
    NO_PROXY entries."""
    no_proxy = ",".join(
        dict.fromkeys(filter(None, (*_PROXY_BYPASS.split(","), *existing_no_proxy.split(","))))
    )
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def _git_auth_env(env: dict) -> dict[str, str]:
    """PURE: ``GIT_CONFIG_*`` vars that let git reach github over HTTPS with the
    forwarded token, rewriting SSH remotes.

    Why this is needed: the sandbox never binds ``~/.ssh`` (a ``_SECRET_HOME_DIR``)
    and its tmpfs ``$HOME`` has no keys, so a ``git@github.com:`` clone/fetch/push
    inside the sandbox fails ("Could not read from remote repository"). This routes
    those operations through ``https://github.com/`` with an ``Authorization``
    header built from the token instead. Injected via ``GIT_CONFIG_COUNT`` env
    (read by every git invocation), so it covers clone, fetch and push uniformly,
    is **never written to ``.git/config``** (the workspace token-leak verification
    still passes), and leaves ``remote.origin.url`` as the original SSH URL.

    Returns ``{}`` when no token is present — fail-open to the prior SSH path (the
    un-sandboxed executor, which has ``~/.ssh``, is unaffected since this only
    enters the sandbox env)."""
    token = next((env[k] for k in _GIT_TOKEN_ENVS if env.get(k)), None)
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_0": "git@github.com:",
        "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_1": f"Authorization: Basic {basic}",
    }


def _real(p: str | None) -> str | None:
    if not p:
        return None
    try:
        rp = os.path.realpath(p)
        return rp if os.path.exists(rp) else None
    except OSError:
        return None


def _editable_install_dirs(oc_root: Path) -> list[str]:
    """Source dirs of editable-installed dependencies that live OUTSIDE oc_root.

    The OC venv installs sibling repos (TeamExecutor, DAGExecutor) as editable
    (PEP 660), so their ``.dist-info/direct_url.json`` records the real source
    path (e.g. /home/dev/Documents/GitHub/TeamExecutor). Those back
    ``import team_executor`` / ``import dag_executor`` — the executor backends.
    Without binding them the sandboxed executor fails ``No module named ...`` at
    its very first stage (only surfaces once clone works). oc_root's own editable
    install is skipped (already bound via ``oc_root/src``). Discovered (not
    hardcoded) so new editable deps are picked up automatically."""
    dirs: list[str] = []
    oc_real = _real(str(oc_root))
    for du in (oc_root / ".venv").glob("lib/python*/site-packages/*.dist-info/direct_url.json"):
        try:
            meta = json.loads(du.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not meta.get("dir_info", {}).get("editable"):
            continue
        url = meta.get("url", "")
        rp = _real(url[len("file://") :] if url.startswith("file://") else url)
        if not rp or rp in dirs:
            continue
        if oc_real and (rp == oc_real or rp.startswith(oc_real + os.sep)):
            continue  # oc_root is already bound
        dirs.append(rp)
    return dirs


def _venv_interpreter_roots(venv_python: Path) -> list[str]:
    """Install root(s) to ro-bind so ``venv_python`` resolves inside the sandbox.

    Returns the install root (the dir holding ``bin/``) of both the realpath'd
    interpreter AND the immediate symlink target, deduped. A uv-managed venv points
    ``.venv/bin/python`` at a version-alias dir (``cpython-3.12-…``) that is itself a
    symlink to the patch dir (``cpython-3.12.13-…``); binding only the realpath'd
    patch dir leaves that alias path dangling, so both are needed. Existing/real
    paths only (a copied — non-symlink — venv python yields just its own root)."""
    roots: list[str] = []

    def _push(root: str) -> None:
        if root and os.path.exists(root) and root not in roots:
            roots.append(root)

    vp = str(venv_python)
    rp = _real(vp)  # realpath target's install root (has bin + lib)
    if rp:
        _push(os.path.dirname(os.path.dirname(rp)))
    # The immediate symlink target's install root — the version-alias dir the symlink
    # traverses. Do NOT realpath it: realpath would collapse the very alias we must
    # bind (its dir is itself a symlink to the patch dir already added above).
    try:
        if os.path.islink(vp):
            link = os.readlink(vp)
            link_abs = (
                link if os.path.isabs(link) else os.path.normpath(os.path.join(os.path.dirname(vp), link))
            )
            _push(os.path.dirname(os.path.dirname(link_abs)))
    except OSError:
        pass
    return roots


def _toolchain_ro_binds(oc_root: Path, env: dict) -> list[str]:
    """Read-only host paths the executor toolchain needs. Derived from the
    resolved binaries + env so it tracks where things actually live."""
    binds: list[str] = []

    def add(p: str | None) -> None:
        rp = _real(p)
        if rp and rp not in binds:
            binds.append(rp)

    # The OC source tree (PYTHONPATH) + its venv (python/pytest/ruff).
    add(str(oc_root / "src"))
    add(str(oc_root / ".venv"))
    # A uv-managed venv symlinks `.venv/bin/python` to an interpreter OUTSIDE the
    # bound system dirs, AND through a version-alias symlink:
    #   .venv/bin/python -> <store>/cpython-3.12-<plat>/bin/python3.12
    #   <store>/cpython-3.12-<plat>        (a symlink) -> <store>/cpython-3.12.13-<plat>
    # Binding only the *realpath'd* patch dir (cpython-3.12.13-…) leaves the ALIAS
    # path (cpython-3.12-…) the symlink actually traverses dangling inside the
    # sandbox, so bwrap aborts `execvp .../.venv/bin/python: No such file or
    # directory` and the executor never starts (→ "execute produced no result", the
    # lane churns). Bind the install root of BOTH the realpath target and the
    # immediate symlink target so the whole chain resolves. Each is skipped when it
    # already lives under a bound system dir (a system python needs no extra bind).
    # Append VERBATIM, not via add(): add() realpaths, which would collapse the
    # version-alias dir back onto the patch dir and lose the alias path the venv
    # symlink actually traverses. _venv_interpreter_roots already returns existing,
    # correct paths.
    for interp_root in _venv_interpreter_roots(oc_root / ".venv" / "bin" / "python"):
        if interp_root not in binds and not any(
            interp_root == d or interp_root.startswith(d + os.sep) for d in _RO_SYSTEM_DIRS
        ):
            binds.append(interp_root)
    # Editable-installed sibling-repo deps (team_executor, dag_executor, …).
    for d in _editable_install_dirs(oc_root):
        add(d)
    # Agent CLIs + their install dirs.
    for tool in ("claude", "cl", "uv", "aider", "node", "npm"):
        found = shutil.which(tool)
        if found:
            add(os.path.dirname(_real(found) or found))
    # ContextLifecycle home (cl) + the anchor manifest (.context tree).
    add(env.get("CL_HOME"))
    add(env.get("CL_ANCHOR"))
    # Pre-provisioned host caches, bound at their real paths so the executor needs
    # no pypi/CDN egress: the wheelhouse (offline dev-install via --find-links
    # $OC_WHEELHOUSE) and the tiktoken encoding cache (offline token accounting).
    add(env.get("OC_WHEELHOUSE"))
    add(env.get("TIKTOKEN_CACHE_DIR"))
    # NOTE: ~/.claude is intentionally NOT ro-bound at the real path here — the
    # agent uses $HOME=/sandbox-home and build_sandbox_argv re-seeds a WRITABLE
    # ~/.claude there (auth files ro). Binding the real dir read-only would just
    # re-create the "read-only filesystem" trap for the agent's state writes.
    return binds


def build_sandbox_argv(
    inner_cmd: Sequence[str],
    *,
    oc_root: Path,
    rw_root: Path,
    env: dict,
    chdir: Path | None = None,
    extra_ro_binds: Sequence[str] = (),
) -> list[str]:
    """Construct the bwrap argv wrapping ``inner_cmd``. Pure/deterministic given
    its inputs (so it is unit-testable offline). Does NOT check availability —
    callers gate via ``maybe_sandbox``."""
    home = _SANDBOX_HOME
    argv: list[str] = [
        "bwrap",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        home,
    ]
    for d in _RO_SYSTEM_DIRS:
        if os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    real_home = os.path.realpath(env.get("HOME") or os.path.expanduser("~"))
    secret_paths = {os.path.join(real_home, d) for d in _SECRET_HOME_DIRS}
    for src in (*_toolchain_ro_binds(oc_root, env), *extra_ro_binds):
        # Defense-in-depth: never bind a credential dir even if a toolchain path
        # somehow resolved to one (e.g. a misconfigured CL_HOME under ~/.ssh).
        if any(src == s or src.startswith(s + os.sep) for s in secret_paths):
            continue
        argv += ["--ro-bind", src, src]
    # Re-seed Claude state into the tmpfs home as a WRITABLE ~/.claude. The agent
    # writes session state there during a task (projects/, todos/, file-history/,
    # history.jsonl, …); ro-binding the whole dir made every such write fail
    # "read-only filesystem", so the agent couldn't do real work and the commit
    # stage found nothing to commit. Only the auth + settings are bound read-only;
    # the rest the agent creates fresh in the tmpfs (discarded with the sandbox).
    claude_dir = _real(os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude"))
    if claude_dir:
        argv += ["--tmpfs", f"{home}/.claude"]
        for fname in _CLAUDE_AUTH_FILES:
            src = _real(os.path.join(claude_dir, fname))
            if src:
                argv += ["--ro-bind", src, f"{home}/.claude/{fname}"]
    # The one writable real path: the per-task ephemeral dir (workspace, the
    # plan bundle, the config copy, the result file — all under it). oc_root
    # itself is NOT bound, so .env secrets and the .git token stay outside.
    argv += ["--bind", str(rw_root), str(rw_root)]
    # Controlled env (already minimized by build_allowlist_env), HOME re-pointed
    # at the sandbox tmpfs home so secrets under the real HOME are unreachable.
    # Add the git HTTPS-token config so git@github remotes work without ~/.ssh
    # (absent in the sandbox); no-op when no token is present. Also prepend the
    # task workspace's .venv/bin to PATH so the agent can run `pytest`/`ruff`
    # directly — the repo's dev tools live there (created by the bootstrap), and
    # without this bare `pytest` is "command not found" and the agent reports it
    # "cannot run tests" and the verify stage fails. The dir need not exist yet at
    # build time (PATH is resolved at exec time, after the bootstrap creates it).
    workspace = chdir if chdir is not None else rw_root
    venv_bin = f"{workspace}/.venv/bin"
    path_env = {"PATH": f"{venv_bin}:{env['PATH']}"} if env.get("PATH") else {}
    setenv = {**env, **_git_auth_env(env), **path_env}
    for k, v in setenv.items():
        if v is None:
            continue
        argv += ["--setenv", k, str(v)]
    argv += ["--setenv", "HOME", home]
    # The executor runs as uid 0 inside the sandbox — bwrap's OWN user namespace maps
    # the calling uid to root, so this holds whether or not the pasta egress netns is
    # wrapped around it (the netns keeps the caller's uid; it is bwrap that produces
    # the root mapping). The agent CLI REFUSES `--dangerously-skip-permissions` under
    # root "for security reasons" → `claude exited 1` and every goal task fails at the
    # agent launch. `IS_SANDBOX=1` is the agent's explicit signal that an outer sandbox
    # already provides the isolation, so the skip is allowed as root. Setting it here
    # is correct by construction: build_sandbox_argv only runs when the bwrap sandbox
    # is active, which IS the isolation it attests to.
    argv += ["--setenv", "IS_SANDBOX", "1"]
    argv += ["--chdir", str(chdir if chdir is not None else rw_root)]
    return [*argv, *inner_cmd]


def maybe_sandbox(
    inner_cmd: Sequence[str],
    *,
    oc_root: Path,
    rw_root: Path,
    env: dict,
    enabled: bool,
    chdir: Path | None = None,
) -> list[str]:
    """Return the bwrap-wrapped command when sandboxing is enabled AND available
    AND the workspace exists; otherwise DEGRADE — which by default means RAISE.

    This is the single decision point, and it is fail-CLOSED by default since
    audit Track A3: when containment is unavailable and ``OC_SANDBOX_REQUIRED`` is
    not explicitly ``0``, ``_degrade`` raises ``ContainmentRequiredError`` rather than
    returning the unwrapped command. Dispatch turns that into a blocked task
    with a visible fault, so §0.1 (degrade-never-halt) still holds at FLEET
    level — the task fails, the fleet keeps serving.

    Only with an explicit ``OC_SANDBOX_REQUIRED=0`` does it return ``inner_cmd``
    unchanged, which is the pre-A3 fail-open behavior and runs the token-holding
    backend un-contained.
    """
    if not enabled:
        return list(inner_cmd)
    required = _containment_required(env, var="OC_SANDBOX_REQUIRED")

    def _degrade(reason: str) -> list[str]:
        _warn_degraded(reason)
        if required:
            raise ContainmentRequiredError(
                f"sandbox containment required (default; opt out with "
                f"OC_SANDBOX_REQUIRED=0) but unavailable ({reason})"
            )
        return list(inner_cmd)

    if not bwrap_available():
        return _degrade("bwrap_unavailable")
    try:
        if not Path(rw_root).is_dir():
            return _degrade("workspace_missing")
        # Phase 3 (D-SBX-2): route egress through the L7/SNI allowlist proxy when
        # OC_EGRESS_PROXY is set AND reachable. The reachability check is the
        # fail-open gate (a dead proxy => no proxy env => direct egress, never a
        # halt). Computed here (the impure decision point) so build_sandbox_argv
        # stays a pure, offline-testable env→argv function.
        sandbox_env = dict(env)
        proxy_url = _resolve_egress_proxy(env)
        if proxy_url:
            sandbox_env.update(
                _proxy_env(
                    proxy_url,
                    existing_no_proxy=env.get("NO_PROXY") or env.get("no_proxy") or "",
                )
            )
        return build_sandbox_argv(
            inner_cmd, oc_root=oc_root, rw_root=rw_root, env=sandbox_env, chdir=chdir
        )
    except ContainmentRequiredError:
        raise  # operator opted into fail-closed — propagate, don't swallow
    except Exception as exc:  # noqa: BLE001 — sandbox construction must never break dispatch
        return _degrade(f"construction_error:{type(exc).__name__}")


__all__ = [
    "build_sandbox_argv",
    "bwrap_available",
    "maybe_sandbox",
    "sandbox_enabled",
    "verify_containment",
]
