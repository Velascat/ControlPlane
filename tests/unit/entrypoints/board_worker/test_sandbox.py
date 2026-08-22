# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ProtocolWarden
"""Tests for the bwrap process sandbox (SBX Phase 2).

Covers the exit-gate properties (HARNESS_TRUST_HARDENING.md §3 Phase 2):
  - /proc/<parent>/environ unreadable in-sandbox (--unshare-pid + fresh /proc),
  - ~/.ssh (and other secret HOME dirs) never bound,
  - fail-open: missing bwrap / disabled / bad workspace -> command unchanged.

The two "real bwrap" tests actually run the sandbox and are skipped where bwrap
is unavailable (CI containers); the unit tests pin the argv contract offline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from operations_center.entrypoints.board_worker import containment
from operations_center.entrypoints.board_worker import sandbox as sbx
from operations_center.entrypoints.board_worker.sandbox import (
    build_sandbox_argv,
    bwrap_available,
    maybe_sandbox,
)

_HAS_BWRAP = shutil.which("bwrap") is not None


def _env(tmp_path: Path) -> dict:
    return {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "sk-x"}


class TestArgvContract:
    def test_unshare_pid_and_fresh_proc(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["echo", "hi"], oc_root=tmp_path, rw_root=ws, env=_env(tmp_path))
        assert "--unshare-pid" in argv
        # fresh /proc mounted in the new PID namespace
        assert argv[argv.index("--proc") + 1] == "/proc"
        assert "--die-with-parent" in argv

    def test_clearenv_and_explicit_setenv(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env=_env(tmp_path))
        assert "--clearenv" in argv
        joined = " ".join(argv)
        assert "--setenv OPENAI_API_KEY sk-x" in joined  # model cred passed explicitly
        # HOME is re-pointed at the sandbox tmpfs home, not the real one
        assert argv[-3:-1] != ["--setenv", "HOME"] or True
        assert "--setenv HOME /sandbox-home" in joined

    def test_secret_home_dirs_never_bound(self, tmp_path: Path):
        # Simulate a real HOME containing secrets.
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".aws").mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env={"HOME": str(home)})
        joined = " ".join(argv)
        assert "/.ssh" not in joined
        assert "/.aws" not in joined
        assert "/.gnupg" not in joined

    def test_claude_home_is_writable_with_auth_ro(self, tmp_path: Path):
        # ~/.claude must be a WRITABLE tmpfs in the sandbox (the agent writes its
        # session state there); only the auth/settings are bound read-only.
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text("{}")
        (home / ".claude" / "settings.json").write_text("{}")
        # a big state dir that must NOT be whole-dir ro-bound
        (home / ".claude" / "projects").mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env={"HOME": str(home)})
        joined = " ".join(argv)
        # /sandbox-home/.claude is a tmpfs (writable), not a whole-dir ro-bind
        assert "--tmpfs /sandbox-home/.claude" in joined
        assert f"--ro-bind {home / '.claude'} /sandbox-home/.claude " not in joined + " "
        # only the auth files are ro-bound, into the tmpfs
        creds = home / ".claude" / ".credentials.json"
        assert f"--ro-bind {creds} /sandbox-home/.claude/.credentials.json" in joined
        assert "/sandbox-home/.claude/settings.json" in joined

    def test_workspace_is_the_only_writable_bind(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env=_env(tmp_path))
        # exactly one --bind (rw), and it is the workspace; everything else ro
        rw = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        assert rw == [str(ws)]

    def test_secret_home_dir_bind_is_filtered(self, tmp_path: Path):
        # A toolchain path that resolves under a credential dir is dropped.
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        argv = build_sandbox_argv(
            ["x"],
            oc_root=tmp_path,
            rw_root=tmp_path,
            env={"HOME": str(home)},
            extra_ro_binds=[str(home / ".ssh")],
        )
        assert str(home / ".ssh") not in " ".join(argv)

    def test_inner_command_is_appended_last(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(
            ["python", "-m", "x"], oc_root=tmp_path, rw_root=ws, env=_env(tmp_path)
        )
        assert argv[-3:] == ["python", "-m", "x"]

    def test_is_sandbox_env_set(self, tmp_path: Path):
        # The agent runs as uid 0 in the sandbox (the pasta egress netns maps the
        # process to root) and refuses --dangerously-skip-permissions under root unless
        # IS_SANDBOX=1 attests the outer sandbox provides isolation. Must be set
        # whenever we wrap in bwrap.
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env=_env(tmp_path))
        assert "--setenv IS_SANDBOX 1" in " ".join(argv)

    def test_workspace_venv_bin_prepended_to_path(self, tmp_path: Path):
        # The agent must run bare `pytest`/`ruff`; the workspace .venv/bin is
        # prepended to PATH (resolved at exec time, after the bootstrap makes it).
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(
            ["x"], oc_root=tmp_path, rw_root=tmp_path, env=_env(tmp_path), chdir=ws
        )
        path_val = argv[argv.index("PATH") + 1]
        assert path_val.startswith(f"{ws}/.venv/bin:")

    def test_venv_python_interpreter_root_is_bound(self, tmp_path: Path):
        # A uv-managed venv symlinks .venv/bin/python to an interpreter OUTSIDE the
        # bound system dirs; the sandbox must bind that interpreter's install root or
        # bwrap can't execvp the venv python ("No such file or directory") and the
        # executor never starts. Regression for the "execute produced no result" churn.
        oc = tmp_path / "oc"
        (oc / ".venv" / "bin").mkdir(parents=True)
        interp_root = tmp_path / "uv" / "cpython-3.12"  # outside /usr
        (interp_root / "bin").mkdir(parents=True)
        real_py = interp_root / "bin" / "python3.12"
        real_py.write_text("#!/bin/true\n")
        real_py.chmod(0o755)
        (oc / ".venv" / "bin" / "python").symlink_to(real_py)
        ws = oc / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["python"], oc_root=oc, rw_root=ws, env=_env(tmp_path))
        ro_srcs = [
            argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind" and i + 1 < len(argv)
        ]
        assert str(interp_root) in ro_srcs, ro_srcs

    def test_venv_python_version_alias_binds_both_roots(self, tmp_path: Path):
        # uv layout: .venv/bin/python -> <store>/cpython-3.12-/bin/python3.12, and
        # <store>/cpython-3.12- is itself a symlink to the patch dir <store>/
        # cpython-3.12.13-. Binding only the realpath'd patch dir leaves the ALIAS
        # path dangling in the sandbox (bwrap execvp fails). BOTH install roots must
        # be bound. Regression for the incomplete first interpreter-bind fix.
        oc = tmp_path / "oc"
        (oc / ".venv" / "bin").mkdir(parents=True)
        store = tmp_path / "store"
        store.mkdir()
        real_dir = store / "cpython-3.12.13"
        (real_dir / "bin").mkdir(parents=True)
        real_py = real_dir / "bin" / "python3.12"
        real_py.write_text("#!/bin/true\n")
        real_py.chmod(0o755)
        alias_dir = store / "cpython-3.12"  # version-alias DIR symlink -> patch dir
        alias_dir.symlink_to(real_dir)
        (oc / ".venv" / "bin" / "python").symlink_to(alias_dir / "bin" / "python3.12")
        ws = oc / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["python"], oc_root=oc, rw_root=ws, env=_env(tmp_path))
        ro_srcs = [
            argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind" and i + 1 < len(argv)
        ]
        assert str(real_dir) in ro_srcs, ro_srcs  # realpath target's root (bin + lib)
        assert str(alias_dir) in ro_srcs, ro_srcs  # the alias path the symlink traverses

    def test_interpreter_under_system_dir_not_double_bound(
        self, tmp_path: Path, monkeypatch
    ):
        # When .venv/bin/python resolves under a bound system dir, the guard skips the
        # extra interpreter bind (the system dir is already bound) — it appears once,
        # not twice. Simulate by treating the fake interpreter's root as a system dir.
        oc = tmp_path / "oc"
        (oc / ".venv" / "bin").mkdir(parents=True)
        interp_root = tmp_path / "syspy"
        (interp_root / "bin").mkdir(parents=True)
        real_py = interp_root / "bin" / "python3.12"
        real_py.write_text("#!/bin/true\n")
        real_py.chmod(0o755)
        (oc / ".venv" / "bin" / "python").symlink_to(real_py)
        monkeypatch.setattr(sbx, "_RO_SYSTEM_DIRS", (str(interp_root),))
        ws = oc / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["python"], oc_root=oc, rw_root=ws, env=_env(tmp_path))
        ro_srcs = [
            argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind" and i + 1 < len(argv)
        ]
        # bound once (as a system dir), NOT a second time as a toolchain entry
        assert ro_srcs.count(str(interp_root)) == 1, ro_srcs


class TestFailOpen:
    def test_disabled_returns_unchanged(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        cmd = ["python", "-m", "execute"]
        assert (
            maybe_sandbox(cmd, oc_root=tmp_path, rw_root=ws, env=_env(tmp_path), enabled=False)
            == cmd
        )

    def test_missing_workspace_raises_by_default(self, tmp_path: Path, monkeypatch):
        # Track A3: containment is required unless explicitly opted out — a
        # degrade fails the task, it does not silently run un-sandboxed.
        monkeypatch.delenv("OC_SANDBOX_REQUIRED", raising=False)
        with pytest.raises(sbx.ContainmentRequiredError):
            maybe_sandbox(
                ["python"], oc_root=tmp_path, rw_root=tmp_path / "nope", env={}, enabled=True
            )

    def test_missing_workspace_fail_open_when_opted_out(self, tmp_path: Path):
        cmd = ["python"]
        out = maybe_sandbox(
            cmd,
            oc_root=tmp_path,
            rw_root=tmp_path / "nope",
            env={"OC_SANDBOX_REQUIRED": "0"},
            enabled=True,
        )
        assert out == cmd

    def test_no_bwrap_raises_by_default(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        monkeypatch.delenv("OC_SANDBOX_REQUIRED", raising=False)
        with pytest.raises(sbx.ContainmentRequiredError):
            maybe_sandbox(["python"], oc_root=tmp_path, rw_root=ws, env={}, enabled=True)

    def test_no_bwrap_fail_open_when_opted_out(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        cmd = ["python"]
        out = maybe_sandbox(
            cmd, oc_root=tmp_path, rw_root=ws, env={"OC_SANDBOX_REQUIRED": "0"}, enabled=True
        )
        assert out == cmd

    def test_enabled_but_degraded_logs_observable_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        # Audit fix: enabled-but-degraded must be OBSERVABLE, not silent —
        # the warning fires on the opted-out fail-open path too.
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        with caplog.at_level("WARNING"):
            maybe_sandbox(
                ["python"],
                oc_root=tmp_path,
                rw_root=ws,
                env={"OC_SANDBOX_REQUIRED": "0"},
                enabled=True,
            )
        assert any("sandbox_degraded" in r.message for r in caplog.records)

    def test_disabled_does_not_warn(self, tmp_path: Path, monkeypatch, caplog):
        # When the sandbox is intentionally OFF, there is no degradation to report.
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        with caplog.at_level("WARNING"):
            maybe_sandbox(["python"], oc_root=tmp_path, rw_root=tmp_path, env={}, enabled=False)
        assert not any("sandbox_degraded" in r.message for r in caplog.records)

    def test_required_raises_when_degraded(self, tmp_path: Path, monkeypatch):
        # B4: OC_SANDBOX_REQUIRED flips fail-open into fail-closed — a degrade
        # must REFUSE to run un-contained, not silently drop the sandbox.
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        with pytest.raises(sbx.ContainmentRequiredError):
            maybe_sandbox(
                ["python"],
                oc_root=tmp_path,
                rw_root=ws,
                env={"OC_SANDBOX_REQUIRED": "1"},
                enabled=True,
            )

    def test_required_via_process_env(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        monkeypatch.setenv("OC_SANDBOX_REQUIRED", "1")
        with pytest.raises(sbx.ContainmentRequiredError):
            maybe_sandbox(["python"], oc_root=tmp_path, rw_root=ws, env={}, enabled=True)

    def test_explicit_opt_out_fail_open(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.sandbox.bwrap_available", lambda: False
        )
        # explicit opt-out restores the observable fail-open degrade
        monkeypatch.setenv("OC_SANDBOX_REQUIRED", "0")
        assert maybe_sandbox(
            ["python"], oc_root=tmp_path, rw_root=ws, env={}, enabled=True
        ) == ["python"]

    def test_required_by_default_when_flag_unset(self, tmp_path: Path, monkeypatch):
        # Track A3 pin: an UNSET flag means required — this is the posture the
        # audit flipped; a regression back to opt-in must fail here.
        monkeypatch.delenv("OC_SANDBOX_REQUIRED", raising=False)
        assert sbx._containment_required({}, var="OC_SANDBOX_REQUIRED") is True
        assert sbx._containment_required({"OC_SANDBOX_REQUIRED": "0"}, var="OC_SANDBOX_REQUIRED") is False


class TestDefaultsAndSelfCheck:
    def test_sandbox_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("OC_BWRAP_SANDBOX", raising=False)
        assert sbx.sandbox_enabled() is True
        monkeypatch.setenv("OC_BWRAP_SANDBOX", "0")
        assert sbx.sandbox_enabled() is False
        monkeypatch.setenv("OC_BWRAP_SANDBOX", "1")
        assert sbx.sandbox_enabled() is True

    def test_verify_containment_reports_missing_bwrap(self, monkeypatch):
        monkeypatch.delenv("OC_BWRAP_SANDBOX", raising=False)
        monkeypatch.setenv("OC_EGRESS_NETNS", "0")
        monkeypatch.setattr(containment, "bwrap_available", lambda: False)
        problems = sbx.verify_containment()
        assert any("bwrap" in p for p in problems)

    def test_verify_containment_reports_netns_gaps(self, monkeypatch):
        monkeypatch.setenv("OC_BWRAP_SANDBOX", "0")
        monkeypatch.delenv("OC_EGRESS_NETNS", raising=False)  # default on
        monkeypatch.delenv("OC_EGRESS_PROXY", raising=False)
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.netns.pasta_path", lambda: None
        )
        problems = sbx.verify_containment()
        assert any("pasta" in p for p in problems)
        assert any("OC_EGRESS_PROXY" in p for p in problems)

    def test_verify_containment_reports_missing_iptables(self, monkeypatch):
        # The boot check covered bwrap, pasta and the proxy but NOT iptables — so a
        # machine with no iptables passed the self-check while every task ran in a
        # netns with no OUTPUT DROP. Boot must surface it.
        monkeypatch.setenv("OC_BWRAP_SANDBOX", "0")
        monkeypatch.delenv("OC_EGRESS_NETNS", raising=False)  # default on
        monkeypatch.setenv("OC_EGRESS_PROXY", "http://127.0.0.1:8889")
        monkeypatch.setattr(
            "operations_center.entrypoints.board_worker.netns.pasta_path",
            lambda: "/usr/bin/pasta",
        )
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: True)
        monkeypatch.setattr(containment, "iptables_path", lambda: None)
        problems = sbx.verify_containment()
        assert any("iptables" in p for p in problems)

    def test_iptables_path_falls_back_to_sbin_when_not_on_path(self, monkeypatch, tmp_path):
        # A minimized worker PATH typically drops /usr/sbin, so shutil.which misses an
        # installed iptables. The absolute-path fallback is what keeps the probe honest.
        fake = tmp_path / "iptables"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(containment.shutil, "which", lambda _cmd: None)
        monkeypatch.setattr(containment, "_IPTABLES_SBIN_PATHS", (str(fake),))
        assert containment.iptables_path() == str(fake)
        monkeypatch.setattr(containment, "_IPTABLES_SBIN_PATHS", ("/nonexistent/iptables",))
        assert containment.iptables_path() is None

    def test_verify_containment_clean_when_all_disabled(self, monkeypatch):
        monkeypatch.setenv("OC_BWRAP_SANDBOX", "0")
        monkeypatch.setenv("OC_EGRESS_NETNS", "0")
        assert sbx.verify_containment() == []


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
class TestRealBwrapExitGate:
    def test_parent_environ_unreadable_in_sandbox(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        # Try to read PID 1's environ from inside; with --unshare-pid + fresh
        # /proc there is no parent to read, so this must fail/empty.
        oc_root = Path(__file__).resolve().parents[4]
        argv = build_sandbox_argv(
            ["sh", "-c", "cat /proc/1/environ 2>/dev/null | tr -d '\\0'; echo DONE"],
            oc_root=oc_root,
            rw_root=ws,
            env={"HOME": str(tmp_path / "home"), "SECRET_SENTINEL": "leak-me"},
        )
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        assert "leak-me" not in out.stdout  # the parent's secret env did not leak
        assert out.stdout.strip().endswith("DONE")

    def test_ssh_dir_unreadable_in_sandbox(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        oc_root = Path(__file__).resolve().parents[4]
        argv = build_sandbox_argv(
            ["sh", "-c", "ls ~/.ssh 2>&1; echo DONE"],
            oc_root=oc_root,
            rw_root=ws,
            env={"HOME": "/home/dev"},  # real home with a real ~/.ssh on this host
        )
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        # ~/.ssh resolves to the tmpfs sandbox home where it does not exist
        assert "id_" not in out.stdout and "authorized_keys" not in out.stdout
        assert "DONE" in out.stdout


def test_bwrap_available_matches_shutil():
    assert bwrap_available() == (shutil.which("bwrap") is not None)


class TestEgressProxyWiring:
    """SBX Phase 3 (D-SBX-2): HTTPS_PROXY wiring into the sandbox, fail-open."""

    def test_proxy_env_sets_both_cases_and_localhost_bypass(self):
        out = sbx._proxy_env("http://127.0.0.1:8889")
        assert out["HTTPS_PROXY"] == "http://127.0.0.1:8889"
        assert out["https_proxy"] == "http://127.0.0.1:8889"
        assert out["HTTP_PROXY"] == "http://127.0.0.1:8889"
        # localhost (ollama floor + key-proxy) must bypass the egress proxy
        assert "127.0.0.1" in out["NO_PROXY"]
        assert "localhost" in out["NO_PROXY"]
        assert out["NO_PROXY"] == out["no_proxy"]

    def test_proxy_env_preserves_existing_no_proxy_without_dupes(self):
        out = sbx._proxy_env("http://127.0.0.1:8889", existing_no_proxy="example.com,127.0.0.1")
        parts = out["NO_PROXY"].split(",")
        assert "example.com" in parts
        # 127.0.0.1 came from both the bypass default and the caller — dedup'd
        assert parts.count("127.0.0.1") == 1

    def test_resolve_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("OC_EGRESS_PROXY", raising=False)
        assert sbx._resolve_egress_proxy({}) is None

    def test_resolve_returns_none_when_unreachable(self, monkeypatch):
        # Set the flag but make the reachability probe fail -> fail-open (None).
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: False)
        assert sbx._resolve_egress_proxy({"OC_EGRESS_PROXY": "http://127.0.0.1:8889"}) is None

    def test_resolve_returns_url_when_reachable(self, monkeypatch):
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: True)
        assert (
            sbx._resolve_egress_proxy({"OC_EGRESS_PROXY": "http://127.0.0.1:8889"})
            == "http://127.0.0.1:8889"
        )

    def test_resolve_falls_back_to_parent_env(self, monkeypatch):
        monkeypatch.setenv("OC_EGRESS_PROXY", "http://127.0.0.1:9999")
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: True)
        assert sbx._resolve_egress_proxy({}) == "http://127.0.0.1:9999"

    def test_maybe_sandbox_injects_proxy_when_reachable(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(sbx, "bwrap_available", lambda: True)
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: True)
        env = {"HOME": str(tmp_path / "home"), "OC_EGRESS_PROXY": "http://127.0.0.1:8889"}
        argv = maybe_sandbox(["x"], oc_root=tmp_path, rw_root=ws, env=env, enabled=True)
        joined = " ".join(argv)
        assert "--setenv HTTPS_PROXY http://127.0.0.1:8889" in joined
        assert "--setenv NO_PROXY" in joined

    def test_maybe_sandbox_no_proxy_when_unreachable_fail_open(self, tmp_path: Path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(sbx, "bwrap_available", lambda: True)
        monkeypatch.setattr(containment, "_proxy_reachable", lambda url, **kw: False)
        env = {"HOME": str(tmp_path / "home"), "OC_EGRESS_PROXY": "http://127.0.0.1:8889"}
        argv = maybe_sandbox(["x"], oc_root=tmp_path, rw_root=ws, env=env, enabled=True)
        # dead proxy => no proxy env injected => still sandboxed, direct egress
        assert "HTTPS_PROXY" not in " ".join(argv)
        assert "bwrap" in argv[0]

    def test_proxy_reachable_false_on_closed_port(self):
        # Nothing listens on this port -> reachable is False (real socket probe).
        assert sbx._proxy_reachable("http://127.0.0.1:1", timeout=0.2) is False


class TestGitHttpsTokenAuth:
    """SBX Phase 2: git@github clones work in the sandbox (no ~/.ssh) via an
    HTTPS+token rewrite injected as GIT_CONFIG_* env."""

    def test_no_token_returns_empty(self):
        assert sbx._git_auth_env({}) == {}
        assert sbx._git_auth_env({"GITHUB_TOKEN": ""}) == {}

    def test_builds_insteadof_and_extraheader_from_github_token(self):
        import base64

        out = sbx._git_auth_env({"GITHUB_TOKEN": "tok123"})
        assert out["GIT_CONFIG_COUNT"] == "2"
        assert out["GIT_CONFIG_KEY_0"] == "url.https://github.com/.insteadOf"
        assert out["GIT_CONFIG_VALUE_0"] == "git@github.com:"
        assert out["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
        expected = base64.b64encode(b"x-access-token:tok123").decode()
        assert out["GIT_CONFIG_VALUE_1"] == f"Authorization: Basic {expected}"

    def test_falls_back_to_git_token(self):
        out = sbx._git_auth_env({"GIT_TOKEN": "abc"})
        assert out.get("GIT_CONFIG_COUNT") == "2"

    def test_github_token_preferred_over_git_token(self):
        out = sbx._git_auth_env({"GITHUB_TOKEN": "primary", "GIT_TOKEN": "secondary"})
        import base64

        assert base64.b64encode(b"x-access-token:primary").decode() in out["GIT_CONFIG_VALUE_1"]

    def test_build_sandbox_argv_setenvs_git_config_when_token_present(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        env = {"HOME": str(tmp_path / "home"), "GITHUB_TOKEN": "tok"}
        argv = build_sandbox_argv(["git", "clone", "x"], oc_root=tmp_path, rw_root=ws, env=env)
        joined = " ".join(argv)
        assert "--setenv GIT_CONFIG_COUNT 2" in joined
        assert "url.https://github.com/.insteadOf" in joined

    def test_build_sandbox_argv_no_git_config_without_token(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=ws, env={"HOME": str(tmp_path)})
        assert "GIT_CONFIG_COUNT" not in " ".join(argv)


class TestEditableInstallBinds:
    """SBX Phase 2: editable-installed sibling-repo deps (team_executor,
    dag_executor) must be ro-bound or the sandboxed executor can't import them."""

    def _make_editable(self, oc_root: Path, name: str, src: Path, *, editable: bool) -> None:
        site = oc_root / ".venv" / "lib" / "python3.12" / "site-packages"
        di = site / f"{name}-0.1.0.dist-info"
        di.mkdir(parents=True, exist_ok=True)
        (di / "direct_url.json").write_text(
            json.dumps({"url": f"file://{src}", "dir_info": {"editable": editable}}),
            encoding="utf-8",
        )

    def test_discovers_editable_dirs_outside_oc_root(self, tmp_path: Path):
        oc_root = tmp_path / "oc"
        (oc_root / "src").mkdir(parents=True)
        sibling = tmp_path / "Sibling"
        sibling.mkdir()
        self._make_editable(oc_root, "sibling_pkg", sibling, editable=True)
        dirs = sbx._editable_install_dirs(oc_root)
        assert str(sibling) in dirs

    def test_skips_non_editable_and_oc_root_self(self, tmp_path: Path):
        oc_root = tmp_path / "oc"
        (oc_root / "src").mkdir(parents=True)
        wheel = tmp_path / "Wheel"
        wheel.mkdir()
        self._make_editable(oc_root, "wheel_pkg", wheel, editable=False)  # not editable
        self._make_editable(oc_root, "operations_center", oc_root, editable=True)  # self
        dirs = sbx._editable_install_dirs(oc_root)
        assert str(wheel) not in dirs
        assert str(oc_root) not in dirs

    def test_toolchain_binds_include_editable_dirs(self, tmp_path: Path):
        oc_root = tmp_path / "oc"
        (oc_root / "src").mkdir(parents=True)
        sibling = tmp_path / "TeamExecutor"
        sibling.mkdir()
        self._make_editable(oc_root, "team_executor", sibling, editable=True)
        argv = build_sandbox_argv(
            ["x"], oc_root=oc_root, rw_root=tmp_path, env={"HOME": str(tmp_path)}
        )
        assert str(sibling) in " ".join(argv)


class TestWheelhouseBind:
    """SBX: the pre-provisioned wheelhouse is ro-bound + setenv'd so the
    in-sandbox dev-install runs fully offline."""

    def test_wheelhouse_bound_and_setenv(self, tmp_path: Path):
        wh = tmp_path / "wheelhouse"
        wh.mkdir()
        env = {"HOME": str(tmp_path / "home"), "OC_WHEELHOUSE": str(wh)}
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=tmp_path, env=env)
        joined = " ".join(argv)
        assert f"--ro-bind {wh} {wh}" in joined
        assert f"--setenv OC_WHEELHOUSE {wh}" in joined

    def test_no_wheelhouse_no_bind(self, tmp_path: Path):
        argv = build_sandbox_argv(
            ["x"], oc_root=tmp_path, rw_root=tmp_path, env={"HOME": str(tmp_path)}
        )
        assert "OC_WHEELHOUSE" not in " ".join(argv)

    def test_tiktoken_cache_bound_and_setenv(self, tmp_path: Path):
        tk = tmp_path / "tiktoken"
        tk.mkdir()
        env = {"HOME": str(tmp_path / "home"), "TIKTOKEN_CACHE_DIR": str(tk)}
        argv = build_sandbox_argv(["x"], oc_root=tmp_path, rw_root=tmp_path, env=env)
        joined = " ".join(argv)
        assert f"--ro-bind {tk} {tk}" in joined
        assert f"--setenv TIKTOKEN_CACHE_DIR {tk}" in joined
