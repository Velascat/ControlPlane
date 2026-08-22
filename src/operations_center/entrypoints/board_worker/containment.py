# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""Containment posture for worker/executor subprocesses (audit Track A3).

Single source of truth for the enable/require flags, binary availability, and
the startup self-check shared by the bwrap sandbox (sandbox.py), the egress
netns (netns.py), and their consumers (board_worker dispatch, the reviewer
pipeline, wheelhouse provisioning).

Posture: DEFAULT-ON + FAIL-CLOSED PER TASK. Unset flags mean enabled and
required; a degrade raises and dispatch fails that task with a visible fault —
the fleet keeps serving (degrade-never-halt, §0.1, holds at fleet level). An
operator opts out explicitly with ``<FLAG>=0``.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# SBX Phase 3 (D-SBX-2): the egress proxy URL for the sandbox's HTTPS egress.
_EGRESS_PROXY_ENV_FLAG = "OC_EGRESS_PROXY"

# Override for the iptables binary the in-netns egress firewall uses (symmetry with
# netns.py's OC_PASTA_BIN).
_IPTABLES_BIN_ENV = "OC_IPTABLES_BIN"
# iptables normally lives in an sbin dir, which a minimized PATH drops. Probed at its
# absolute locations too, so availability does not depend on PATH shape.
_IPTABLES_SBIN_PATHS = ("/usr/sbin/iptables", "/sbin/iptables")


class ContainmentRequiredError(RuntimeError):
    """Raised when containment is required (the default) but unavailable.

    Fail-closed by default (audit Track A3): never run a token-holding backend
    un-contained unless the operator explicitly opts out with
    ``OC_SANDBOX_REQUIRED=0``. Dispatch turns this into a blocked task + fault,
    not a crash-loop — the fleet keeps serving.
    """


_FALSY = {"0", "false", "no", "off"}


def _containment_required(env: dict | None, *, var: str) -> bool:
    """True unless the operator explicitly opted out (``var=0``).

    Checks the passed worker env first (the minimized, authoritative env) then
    the process env, so the flag works whether or not it survived minimization.
    Unset means REQUIRED (fail-closed default, audit Track A3).
    """

    for source in (env or {}, os.environ):
        val = source.get(var)
        if val is not None:
            return str(val).strip().lower() not in _FALSY
    return True


def sandbox_enabled() -> bool:
    """bwrap containment is ON unless explicitly disabled (``OC_BWRAP_SANDBOX=0``).

    Default-on per audit Track A3 — the single gate consumed by board_worker
    dispatch, the reviewer pipeline, and wheelhouse provisioning, so the three
    call sites cannot drift.
    """
    return str(os.environ.get("OC_BWRAP_SANDBOX", "1")).strip().lower() not in _FALSY


def verify_containment() -> list[str]:
    """Startup self-check (audit Track A3): the containment components that are
    enabled but unavailable, checked BEFORE any task is accepted.

    Returns human-readable problem strings; empty means the posture is
    satisfiable. Callers log these loudly at startup — per-task enforcement
    still raises ``ContainmentRequiredError`` / ``EgressContainmentRequiredError``,
    but discovering a broken posture at task N is strictly worse than at boot.
    """
    from .netns import netns_enabled, pasta_path  # local import: netns is a sibling leaf

    problems: list[str] = []
    if sandbox_enabled() and not bwrap_available():
        problems.append("bwrap sandbox enabled but the bwrap binary is not on PATH")
    if netns_enabled():
        if pasta_path() is None:
            problems.append("egress netns enabled but the pasta binary is not on PATH")
        if iptables_path() is None:
            problems.append(
                "egress netns enabled but no iptables binary was found "
                "(the in-netns OUTPUT DROP would be skipped, leaving egress on the "
                "honor system)"
            )
        url = os.environ.get(_EGRESS_PROXY_ENV_FLAG)
        if not url:
            problems.append(
                "egress netns enabled but OC_EGRESS_PROXY is unset "
                "(a locked netns with no proxy has no egress)"
            )
        elif not _proxy_reachable(url):
            problems.append(f"egress proxy configured but unreachable ({url})")
    return problems



def bwrap_available() -> bool:
    """Check if bwrap (bubblewrap) is available in the PATH."""
    return shutil.which("bwrap") is not None


def iptables_path() -> str | None:
    """The resolved ``iptables`` binary, or ``None`` when absent.

    The in-netns ``OUTPUT DROP`` is the only thing that makes egress confinement
    STRUCTURAL rather than honor-system (``HTTPS_PROXY``, which a compromised agent
    can simply ``unset``). Its setup script guarded on a bare ``command -v
    iptables``, so an absent binary skipped the firewall silently: pasta still built
    the netns, ``maybe_netns`` still reported success, and the posture read healthy
    while nothing was enforced. Resolving here — once, absolutely — is what lets the
    availability probe and the script agree, and lets a missing binary fail closed
    like every other containment gap.
    """
    found = shutil.which(os.environ.get(_IPTABLES_BIN_ENV) or "iptables")
    if found:
        return found
    for candidate in _IPTABLES_SBIN_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def iptables_available() -> bool:
    """True when the in-netns egress firewall can actually be installed."""
    return iptables_path() is not None


def _proxy_reachable(url: str, *, timeout: float = 0.5) -> bool:
    """True if the egress proxy host:port accepts a TCP connection. This is the
    gate that keeps proxy wiring FAIL-OPEN (§0.1): if the proxy is down we inject
    nothing and the sandbox keeps direct egress rather than losing all HTTPS."""
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8889
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_egress_proxy(env: dict) -> str | None:
    """The egress proxy URL to wire into the sandbox, or ``None``. Reads
    ``OC_EGRESS_PROXY`` from the controlled env (falling back to the parent
    process env, where the fleet actually sets it) and returns it only when the
    proxy is reachable — so a dead/missing proxy degrades to direct egress
    instead of breaking every HTTPS call (fail-open, §0.1)."""
    url = env.get(_EGRESS_PROXY_ENV_FLAG) or os.environ.get(_EGRESS_PROXY_ENV_FLAG)
    if not url or not _proxy_reachable(url):
        return None
    return url



__all__ = [
    "ContainmentRequiredError",
    "bwrap_available",
    "iptables_available",
    "iptables_path",
    "sandbox_enabled",
    "verify_containment",
]
