# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""Structural egress confinement via a rootless network namespace (Phase 3, B1).

The bwrap sandbox shares the host network namespace (D-SBX-2), so egress was only
*honor-system* — the proxy is reached via ``HTTPS_PROXY``, which a compromised agent
can ``unset`` or bypass with a raw socket. The audit confirmed this; the audit's
suggested kernel fix (``systemd-run --user -p IPAddressDeny``) was empirically shown
NOT to enforce under a rootless ``--user`` manager.

This closes it structurally and rootless, validated end to end:

1. **pasta** (``passt``) runs the command in a rootless network namespace and
   transparently maps the netns's ``127.0.0.1`` to the **host's** loopback — so the
   host egress proxy (``127.0.0.1:8889``) and ollama (``127.0.0.1:11434``) stay
   reachable at the SAME addresses with **no env change and no forwarder**. The
   command inside keeps the caller's uid (it is NOT mapped to root) but holds a full
   capability set — including CAP_NET_ADMIN — over *that* netns, which is what lets
   step 2 install rules without privilege on the host.
2. An in-netns **iptables OUTPUT DROP** (allow only ``lo`` + established) kernel-
   blocks every non-loopback egress — a raw socket to the internet gets dropped,
   while the proxy/ollama on the mapped loopback still work.
3. **Caps are dropped** (``setpriv --bounding-set=-all``) before exec'ing the
   executor, so the agent cannot flush the firewall. (bwrap's child userns can't
   reach the parent-owned netns either — belt and suspenders.)

Net: an agent that does ``unset HTTPS_PROXY`` + a raw socket is kernel-blocked,
while HTTPS-through-the-proxy keeps working. The honor-system hole is closed.

**Default-on + fail-closed per task (audit Track A3).** Enabled unless
``OC_EGRESS_NETNS=0`` and required unless ``OC_EGRESS_REQUIRED=0``: when pasta is
missing, when iptables is missing (step 2 would be skipped and the netns left
UNRESTRICTED — egress back on the honor system, the exact hole this module closes),
or when no proxy is configured (a locked netns with no proxy would have *no* egress
at all), the dispatch raises ``EgressContainmentRequiredError`` — the
worker fails THAT TASK with a visible fault and keeps serving (degrade-never-halt
holds at fleet level, §0.1; the task, not the fleet, is what fails closed). An
operator who explicitly opts out with ``OC_EGRESS_REQUIRED=0`` restores the old
observable fail-open degrade."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
from collections.abc import Sequence
from urllib.parse import urlparse

from .containment import iptables_path

logger = logging.getLogger(__name__)

_NETNS_FLAG = "OC_EGRESS_NETNS"
_REQUIRED_FLAG = "OC_EGRESS_REQUIRED"
_PASTA_BIN_ENV = "OC_PASTA_BIN"
_EXTRA_PORTS_ENV = "OC_EGRESS_NETNS_PORTS"  # comma-sep extra host-loopback ports
_OLLAMA_PORT = 11434


# In-netns setup: lock egress to loopback (the proxy lives on the host loopback that
# pasta maps in), then exec the cmd.
#
# The iptables binary is resolved by the CALLER (``containment.iptables_path``) and
# interpolated as an absolute path rather than looked up here as a bare name. Two
# reasons, both learned the hard way:
#   - a bare `command -v iptables` guard silently skipped the whole firewall when the
#     binary was absent, leaving an unrestricted netns while the posture read healthy;
#     absence is now caught up front in ``maybe_netns`` and fails closed;
#   - iptables lives in an sbin dir that the worker's minimized PATH does not carry,
#     so even an installed binary was not necessarily findable from in here.
# A *failing* (as opposed to missing) iptables still degrades fail-open to running
# without the filter (§0.1) — as does a missing setpriv for the cap-drop.
#
# The cap-drop (`setpriv --bounding-set=-all`) is applied ONLY when the payload runs
# DIRECTLY in this netns (sandbox off). When the payload is the bwrap sandbox the
# cap-drop is both redundant AND fatal, so it is omitted (see ``maybe_netns``'s
# ``drop_caps``):
#   - redundant: the agent runs inside bwrap's CHILD user namespace and cannot reach
#     this parent-owned netns firewall regardless of its caps (docstring §3);
#   - fatal: `--bounding-set=-all` empties the bounding set, which PERSISTS into
#     bwrap's child userns and masks CAP_SYS_ADMIN there, so bwrap can no longer
#     create its pid/uts/ipc namespaces — bwrap aborts with "Creating new namespace
#     failed: Operation not permitted" and the two SBX layers compose fail-CLOSED
#     (the executor never starts; the lane churns claim→no-result forever).
def _firewall_setup(iptables_bin: str) -> str:
    """PURE: the in-netns firewall snippet, bound to a resolved ``iptables_bin``."""
    ipt = shlex.quote(iptables_bin)
    return f"""
set -u
IPT={ipt}
if [ -x "$IPT" ] || command -v "$IPT" >/dev/null 2>&1; then
  "$IPT" -A OUTPUT -o lo -j ACCEPT 2>/dev/null \\
    && "$IPT" -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \\
    && "$IPT" -P OUTPUT DROP 2>/dev/null \\
    || echo "oc-netns: egress filter not applied (fail-open)" >&2
else
  echo "oc-netns: iptables not executable at $IPT (fail-open)" >&2
fi
"""


# Drop all caps before exec so a payload running DIRECTLY in the netns can't flush the
# firewall. Fail-open: a missing setpriv degrades to exec'ing without the drop.
_CAPDROP_EXEC = r"""
if command -v setpriv >/dev/null 2>&1; then
  exec setpriv --inh-caps=-all --bounding-set=-all --ambient-caps=-all "$@"
fi
exec "$@"
"""

# Sandboxed payload: bwrap is the containment, so exec it WITH caps intact (it needs
# CAP_SYS_ADMIN in its own child userns to build its namespaces).
_PLAIN_EXEC = r"""
exec "$@"
"""


def _setup_script(*, drop_caps: bool, iptables_bin: str) -> str:
    """In-netns setup script: firewall always; the cap-drop only when ``drop_caps``."""
    return _firewall_setup(iptables_bin) + (_CAPDROP_EXEC if drop_caps else _PLAIN_EXEC)


_FALSY = {"0", "false", "no", "off"}


def netns_enabled() -> bool:
    """Egress netns confinement is ON unless explicitly disabled
    (``OC_EGRESS_NETNS=0``). Default-on per audit Track A3: containment that
    must be remembered at deploy time is containment that silently rots."""
    return str(os.environ.get(_NETNS_FLAG, "1")).strip().lower() not in _FALSY


class EgressContainmentRequiredError(RuntimeError):
    """Raised when egress confinement cannot be established and the operator has
    not explicitly opted out (``OC_EGRESS_REQUIRED=0``). Dispatch turns it into
    a failed task + fault — the fleet keeps serving (§0.1 holds at fleet level).
    """


def egress_required() -> bool:
    """Fail-closed by default (audit Track A3): a degrade raises unless the
    operator explicitly opts out with ``OC_EGRESS_REQUIRED=0``."""
    return str(os.environ.get(_REQUIRED_FLAG, "1")).strip().lower() not in _FALSY


def pasta_path() -> str | None:
    return shutil.which(os.environ.get(_PASTA_BIN_ENV, "pasta"))


def _forward_ports(proxy_url: str) -> list[int]:
    """Host-loopback ports to expose at the netns ``127.0.0.1`` (pasta ``-T``): the
    egress proxy + ollama + any operator-configured extras. These are the ONLY
    host services the confined executor can reach; everything else is dropped."""
    ports: list[int] = []
    proxy_port = urlparse(proxy_url).port
    if proxy_port:
        ports.append(proxy_port)
    if _OLLAMA_PORT not in ports:
        ports.append(_OLLAMA_PORT)
    for extra in os.environ.get(_EXTRA_PORTS_ENV, "").split(","):
        extra = extra.strip()
        if extra.isdigit() and int(extra) not in ports:
            ports.append(int(extra))
    return ports


def maybe_netns(
    cmd: Sequence[str], *, proxy_url: str | None, enabled: bool, drop_caps: bool = True
) -> list[str]:
    """Wrap ``cmd`` to run inside a pasta netns whose only egress is the proxy.

    pasta ``-T <port>`` forwards each host-loopback service (proxy, ollama) to the
    netns ``127.0.0.1:<port>`` so the executor's existing env (``HTTPS_PROXY=
    127.0.0.1:8889``) works unchanged; the in-netns iptables drops everything else.

    ``drop_caps`` (default True) runs ``setpriv --bounding-set=-all`` before exec so a
    payload running directly in the netns can't flush the firewall. Pass ``False``
    when ``cmd`` is the bwrap sandbox: the cap-drop is redundant there (bwrap's child
    userns can't reach this netns firewall) and fatal (it masks the CAP_SYS_ADMIN
    bwrap needs to build its namespaces). See ``_setup_script``.

    Fail-open ONLY when the operator opted out (``OC_EGRESS_REQUIRED=0``): returns
    ``cmd`` unchanged when disabled, when pasta is unavailable, when iptables is
    unavailable (no OUTPUT DROP means an unrestricted netns), or when no egress
    proxy is configured (a locked netns with no proxy would have no usable egress —
    the proxy is the sole channel out). Otherwise each of those raises."""
    if not enabled:
        return list(cmd)
    pasta = pasta_path()
    iptables_bin = iptables_path()
    if pasta is None or not proxy_url or iptables_bin is None:
        # Enabled but degraded: make the silent fail-open observable (§0.1 keeps
        # it non-halting, but the audit flagged that absent isolation must be
        # visible). The structured ``event`` key lets the log sweep alert on it.
        if pasta is None:
            reason = "pasta_unavailable"
        elif not proxy_url:
            reason = "no_egress_proxy"
        else:
            # A netns with no OUTPUT DROP is a netns with unrestricted egress: the
            # HTTPS_PROXY wiring survives, but a compromised agent that unsets it and
            # opens a raw socket is no longer stopped. Same standing as a missing
            # pasta, so it gets the same fail-closed treatment.
            reason = "iptables_unavailable"
        logger.warning(
            "netns_degraded: egress confinement enabled but running with "
            'shared netns (%s) {"event": "netns_degraded", "reason": "%s"}',
            reason,
            reason,
        )
        if egress_required():
            raise EgressContainmentRequiredError(
                f"egress confinement required (default; opt out with "
                f"OC_EGRESS_REQUIRED=0) but unavailable ({reason})"
            )
        return list(cmd)
    forwards: list[str] = []
    for port in _forward_ports(proxy_url):
        forwards += ["-T", str(port)]
    script = _setup_script(drop_caps=drop_caps, iptables_bin=iptables_bin)
    return [pasta, "--config-net", *forwards, "--", "sh", "-c", script, "oc-netns", *cmd]


__all__ = [
    "EgressContainmentRequiredError",
    "egress_required",
    "iptables_path",
    "maybe_netns",
    "netns_enabled",
    "pasta_path",
]
