<!-- console-context -->
## OperatorConsole Context

At the start of each session, read the compiled context before acting:

- `.console/.context` — compiled startup context (generated fresh each launch)

The context file contains your current task, guidelines, backlog, log, and runtime context.

**Source files** (editable truth — update these, not the context file):

| File | Role |
|------|------|
| `.console/task.md` | Current objective and definition of done |
| `.console/guidelines.md` | Repo policy, branch rules, operating constraints |
| `.console/backlog.md` | Work inventory — in-progress, up-next, done |
| `.console/log.md` | Recent decisions, stop points, what changed and why |

After meaningful progress, update `.console/backlog.md` and `.console/log.md`.
Do not edit `.console/.context` directly — it is regenerated at each launch.
<!-- /console-context -->

## Cognition Lifecycle

OC uses [ContextLifecycle](https://github.com/ProtocolWarden/ContextLifecycle) for bounded, resumable agent sessions. **Cognition is hosted by the anchoring manifest** — OC carries no `.context/` of its own. Per P3 of `PlatformDeployment/docs/architecture/adr/0002-work-order-manifest-cognition.md`, every Claude Code session targeting OC must first run `eval $(cl session start PlatformManifest)` (or your private-manifest repo for private work). All capsules, checkpoints, and handoffs land under the anchor's `.context/sessions/<CL_SESSION_ID>/` subtree.

| Surface                                | Purpose                                                              |
|----------------------------------------|----------------------------------------------------------------------|
| `.console/`                            | Operational truth — task, guidelines, backlog, log                   |
| `.console/workers.yaml`                | OC worker/watcher definitions (replaces old `.context/config.yaml`)  |
| `tools/loop/loop_schedule.json`        | Runtime watchdog state (cycle delay) — controller-local, not cognition |
| `<anchor>/.context/sessions/<sid>/`    | Durable cognition (capsules, checkpoints, handoffs) on the manifest  |
| `.claude/`                             | Claude Code adapter — ContextGuard hooks (CL shim per ADR 0002 P5)   |

**Orchestrator lifecycle:**

```
wake → read <anchor>/.context/sessions/<sid>/checkpoints/<latest>.yaml
     → read <anchor>/.context/sessions/<sid>/active/ capsule refs
     → classify state
     → dispatch bounded worker if needed
     → write updated checkpoint to <anchor>/.context/sessions/<sid>/checkpoints/
     → update .console/log.md
     → terminate or compact
```

**Dispatcher wrap (ADR 0002 P4):** `ExecutionCoordinator.execute()` wraps every adapter dispatch in `cl_dispatch_wrap()` (`src/operations_center/execution/cl_wrap.py`). The wrap calls `context_lifecycle.hydrate(lineage_id, request)` before the adapter runs and `context_lifecycle.capture(lineage_id, result)` after — including on adapter exception, so failed lineages still leave a trace under the anchor manifest. The wrap is a no-op when `CL_ANCHOR` is unset, preserving pre-P4 behavior for unanchored sessions.

**On session start:** verify `CL_ANCHOR` is set. Check `<anchor>/.context/sessions/<CL_SESSION_ID>/active/` for any active capsules; check `checkpoints/` for the latest checkpoint.
**On session end:** write a LoopCheckpoint to `checkpoints/`. Update any active capsule's `handoff_notes` and `next_actions`. `cl session end` archives the session subdir.
**Templates:** `<anchor>/.context/templates/`.
**Config:** `<anchor>/.context/config.yaml` (CL guard flags) and `.console/workers.yaml` (OC operational config).

## Delegation Policy

The session you are talking to is the lead. Prefer dispatching a subagent over doing the reading yourself — a subagent burns its own context window and returns only its conclusion, which is what keeps a long session workable.

**Before dispatching anything:** verify `CL_ANCHOR` is set. The ContextGuard `PreToolUse` hook (`.claude/hooks/pre_tool_use.sh`) blocks *every* tool call when it is unset, and subagents inherit the environment — so an unanchored session means each dispatched agent dies on its first tool call with a block message, which reads as the agent being broken rather than the session being unanchored. If it is unset, run `eval $(cl session start PlatformManifest)` (or `PrivateManifest` for private work) before delegating.

**Who does what** — definitions live in `.claude/agents/`:

| Need | Agent |
|------|-------|
| Where does X live, what covers it | `oc-locator` |
| Does it actually pass | `oc-test-runner` |
| Clean up before review | `oc-lint-fixer` |
| `log.md` / `backlog.md` before a commit | `oc-console-scribe` |

Git-state checks before a commit, push, or PR are handled by an operator-local agent that is deliberately not tracked here.

**Lead rules:**

- Write the plan to a file before fanning out. Subagents share nothing but the filesystem — a plan in `.console/task.md` is shared memory; a plan in the lead's conversation is not.
- Demand evidence, not claims. "Tests pass" from an agent with no Bash access is not a result. `oc-test-runner` exists so that claim arrives with a pytest summary line attached.
- Give each agent one self-contained assignment with explicit file paths. Subagents cannot ask clarifying questions — ambiguity becomes a confident guess.
- Never dispatch two writing agents at the same paths concurrently. Reads parallelize freely; writes need either sequencing or separate worktrees.
- Check git state before any commit, push, or PR — not after. Confirm the branch is not `main`, and remember the reviewer watcher squash-merges PRs once checks go green, which flattens ancestry.
