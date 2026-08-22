#!/usr/bin/env bash
# =============================================================================
# install-system-deps.sh — install the OS packages the containment layer needs
#
# `oc setup` installs uv, the provider CLIs and the executor backends, but it has
# never touched system packages. The containment layer's prerequisites — bwrap,
# pasta, iptables, setpriv — were therefore installed by hand or not at all, and
# "not at all" was invisible: a missing iptables made the in-netns OUTPUT DROP
# silently skip, so egress fell back to the honor-system HTTPS_PROXY while the
# boot self-check still reported a healthy posture.
#
# What each one is for (see board_worker/sandbox.py and board_worker/netns.py):
#   bubblewrap  bwrap     process containment — pid/uts/ipc namespaces, clearenv,
#                         ro system, tmpfs $HOME, one writable workspace path
#   passt       pasta     rootless netns that maps the host loopback in, so the
#                         egress proxy and ollama stay reachable
#   iptables    iptables  the in-netns OUTPUT DROP — what makes egress structural
#                         rather than advisory
#   util-linux  setpriv   cap-drop for payloads running directly in the netns
#
# apt only, by design: this targets the Debian/Ubuntu hosts the fleet runs on.
# On anything else it tells you the four packages and exits rather than guessing.
#
# Usage:
#   scripts/install-system-deps.sh            # install what is missing, then verify
#   scripts/install-system-deps.sh --check    # verify only, install nothing
#   scripts/install-system-deps.sh --no-probe # skip the live netns DROP probe
#
# Exit codes: 0 all present (and the probe passed, when run), 1 something missing
# or the probe found egress NOT blocked.
# =============================================================================
set -euo pipefail

CHECK_ONLY=0
RUN_PROBE=1
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --no-probe) RUN_PROBE=0 ;;
        -h | --help)
            sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

# binary → apt package
BINARIES=(bwrap pasta iptables setpriv)
declare -A PACKAGE=(
    [bwrap]=bubblewrap
    [pasta]=passt
    [iptables]=iptables
    [setpriv]=util-linux
)

# iptables normally lands in an sbin dir that a non-root PATH may not carry, so
# resolution mirrors containment.iptables_path(): PATH first, then the known
# absolute locations.
resolve() {
    local binary="$1" found
    if found="$(command -v "$binary" 2>/dev/null)"; then
        echo "$found"
        return 0
    fi
    for candidate in "/usr/sbin/$binary" "/sbin/$binary"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

missing_packages() {
    local binary
    for binary in "${BINARIES[@]}"; do
        resolve "$binary" >/dev/null || echo "${PACKAGE[$binary]}"
    done
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "error: need root to install packages, and sudo is not available." >&2
        echo "  re-run as root, or install by hand: ${*: -1}" >&2
        exit 1
    fi
}

# ── install ──────────────────────────────────────────────────────────────────

mapfile -t MISSING < <(missing_packages | sort -u)

if [ "${#MISSING[@]}" -gt 0 ] && [ "$CHECK_ONLY" -eq 0 ]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "error: apt-get not found — this script is apt-only." >&2
        echo "  install these with your package manager: ${MISSING[*]}" >&2
        exit 1
    fi
    echo "installing: ${MISSING[*]}"
    as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING[@]}"
    echo
fi

# ── verify ───────────────────────────────────────────────────────────────────

status=0
echo "containment prerequisites:"
for binary in "${BINARIES[@]}"; do
    if path="$(resolve "$binary")"; then
        printf '  %-9s %s\n' "$binary" "$path"
    else
        printf '  %-9s MISSING (apt package: %s)\n' "$binary" "${PACKAGE[$binary]}"
        status=1
    fi
done

if [ "$status" -ne 0 ]; then
    echo
    echo "posture is NOT satisfiable — board_worker will fail tasks closed" >&2
    exit 1
fi

# ── live probe: does the OUTPUT DROP actually enforce? ────────────────────────
#
# Presence is not enforcement. WSL and container hosts can have the binary while
# the kernel lacks the netfilter modules, or forbid netns creation outright — in
# which case the firewall block fails and degrades fail-open exactly as if it
# were absent. This runs the real rules in a real pasta netns and checks that an
# outbound connection is refused afterwards but worked before.

if [ "$RUN_PROBE" -eq 0 ]; then
    exit 0
fi

if ! pasta --config-net -- true >/dev/null 2>&1; then
    echo
    echo "note: pasta cannot create a netns here — skipping the live DROP probe."
    exit 0
fi

IPT="$(resolve iptables)"
probe=$(
    cat <<PROBE
set -u
# curl writes its -w output even when the transfer fails, so \`|| echo\` here would
# append a SECOND code and produce "000000". Swallow the exit status instead and
# default an empty capture (curl killed before writing) to 000.
probe_code() {
    code=\$(curl -s -m 6 -o /dev/null -w '%{http_code}' https://1.1.1.1/ 2>/dev/null || true)
    echo "\${code:-000}"
}
before=\$(probe_code)
"$IPT" -A OUTPUT -o lo -j ACCEPT 2>/dev/null \\
  && "$IPT" -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \\
  && "$IPT" -P OUTPUT DROP 2>/dev/null || { echo "RULES-FAILED"; exit 0; }
after=\$(probe_code)
echo "BEFORE \$before AFTER \$after"
PROBE
)

echo
echo "probing the in-netns OUTPUT DROP..."
result="$(pasta --config-net -- sh -c "$probe" 2>/dev/null | tail -1)"

case "$result" in
    "BEFORE 000 "*)
        echo "  inconclusive: the netns had no egress even before the rules ($result)"
        ;;
    *"AFTER 000")
        echo "  OK — egress worked before the rules and is kernel-blocked after ($result)"
        ;;
    RULES-FAILED)
        echo "  FAILED — iptables is installed but the rules did not apply." >&2
        echo "  the kernel likely lacks the netfilter modules; egress is NOT confined." >&2
        status=1
        ;;
    *)
        echo "  FAILED — egress still reachable after the DROP ($result)" >&2
        status=1
        ;;
esac

exit "$status"
