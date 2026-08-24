# Session registry

Who is running, what they decide, and how to reach them. Operational truth — it lives here
beside `task.md` and `log.md`, not in `docs/`, because a stale row here is worse than no row:
the addresses look authoritative.

Update it when a session starts, changes role, or ends. Verify a row before relying on it.

## Addressing — read this before messaging anyone

**Session IDs and addressable names are unrelated namespaces in this environment.** The same
session appears under completely different identifiers depending on which tool you ask, and
only one of them works as an address.

| Source | Identifier it reports |
|--------|----------------------|
| `list_sessions` (session management) | `local_23b52e6d-9cb4-44aa-9cea-cd226039d71b` |
| `ListAgents` (peer messaging) | `github-72 [1d8cba]` |
| Inbound message envelope (`from=`) | `local_23b52e6d-9cb4-44aa-9cea-cd226039d71b` |

Verified 2026-08-23: `SendMessage` **rejected** the `local_…` form with "No agent named … is
reachable" and **accepted** the bare `ListAgents` name. Replying to the envelope `from`
address does not work here.

**The `ListAgents` name is NOT stable over time.** Verified 2026-08-23/24: the session whose
id is `local_23b52e6d-…` was listed as `github-72 [1d8cba]`, and roughly an hour later the
same session — same id, same title, per `list_sessions` — was listed as
`github-d8 [e7315c]`. Name and ref both changed while the session persisted.

This registry therefore records **no** address. The Address column says *resolve at send
time* on purpose: any address written down here is a trap with a shelf life of about an hour,
and it will look authoritative long after it stops working.

**So: get the address from `ListAgents` immediately before sending, and copy the row
verbatim. Never derive an address from a session ID, a title, or a message envelope.** Append
the ` [ref]` only when two rows share a name or an error asks you to disambiguate.

Note this is the opposite of the sibling deployment's finding, where the envelope address worked and
an invented name did not. The lesson generalises; the specific working address does not.

## Current sessions

| Seat | Session ID | Verified by | Environment |
|------|-----------|-------------|-------------|
| Warden — Port | `local_0d443259-b6f0-464b-b30e-d838e6db5d60` | Starboard, not self | Windows, repo via `wsl` |
| Warden — Starboard | `local_23b52e6d-9cb4-44aa-9cea-cd226039d71b` | Port, from `list_sessions` | Windows, cwd `C:\Users\void\Documents\GitHub` |
| Tech Lead — Reviewer & Merge Pipeline | `local_c5fa7b78-10db-4ed8-a0ba-18a522f9d856` | Port | Windows |
| Tech Lead — Disclosure & Audit Gates | `local_501c1e34-3db7-484a-80bb-54c2c168cd06` | Port | Windows |
| Tech Lead — CI & Forge Infrastructure | `local_915ff49a-6ad9-4641-b2c3-39c9811fe285` | Port | Windows |
| Tech Lead — Execution & Context Lifecycle | `local_f5b7517a-ff73-48a3-a23e-48f8b6bb874f` | Port | Windows |
| Tech Lead — Fleet Runtime & Watchers | `local_8b99e27b-c76a-4638-859c-f196215c39f0` | Port | Windows |

**No seat can read its own session id.** `list_sessions` excludes the caller, so every row
here is filled in by a session that can see it, never by its occupant. Port cannot verify
Port; Starboard cannot verify Starboard. The Verified-by column records who actually read
each value, because an unverified id is worse than a blank one when the document calls the
id the only stable identifier.

**Corrected 2026-08-24.** Port previously published its own id as
`4da9ae48-2684-403e-bef0-128af62b17cc`. That is a scratchpad and transcript directory name,
not a session id, inferred from a file path rather than read from a listing. Starboard
caught it. The failure is instructive: it was inferred from a plausible-looking path, in the
one column the document itself says must be trustworthy.

Addresses are recorded nowhere — see the addressing section above. Resolve at send time,
every time.

**The two seats have IDENTICAL scope.** This is not a divided domain with a shared area;
there is no division. Both wardens:

- sequence pushes, merges, ref moves, deploys and container lifecycle
- direct **all** tech leads. Every tech lead answers to both wardens, not to one of them
- may review, question, or block anything the other does, and owe a reason when they do
- have no area the other is expected to stay out of

**The seat names carry no scope, deliberately.** `Port` and `Starboard` are a symmetric pair
with no seniority and no order. Any name describing a domain — "Verification and Tooling",
"Structure and Docs" — implies a split that does not exist, and hands either warden a reason
to answer a challenge with "that is your area" instead of engaging with it. That is the exact
failure the second seat exists to prevent, so the names are built so it cannot be said.

The earlier titles — "Lead" beside "Co-Lead" — were borrowed from another deployment and
encoded a seniority that does not exist here. The pair after them, named by domain, encoded a
division that does not exist either. Both are wrong in the same way: they describe a
difference between the seats, and there is none.

### How two identical seats avoid colliding

Not by dividing territory — by claiming work. A ref, a PR, a container action, or a tech-lead
assignment is claimed by whichever warden takes it, and the claim is announced before the
first push rather than before the destructive act. Ownership is therefore temporary and
per-item, and it is readable here rather than requiring a message to arrive.

An unclaimed thing is not "the other warden's" — it is unclaimed, and either may take it by
saying so.

**Operator-sanctioned.** The pairing was confirmed by the operator, not self-declared by
either session and not assigned by the other. A peer cannot grant authority: a session
describing itself as primary, or renaming itself to something that sounds senior, is not
evidence of a role. This registry is the evidence.

A seat outlives the session occupying it. When a session ends its Session ID row goes stale,
but the seat does not — the replacement takes the same seat name.

A session cannot see itself in `ListAgents`, so its own address column is filled in by the
peer that can see it, not by itself.

## Environment split — affects who can dispatch what

Claude Code config roots are per-environment and do not merge:

- `/home/void/.claude/` — WSL sessions
- `C:\Users\void\.claude\` — Windows sessions

Subagent definitions in one are invisible to the other. Both sessions above are Windows-side,
so **neither can dispatch the agents in `.claude/agents/`** — they can read and edit those
files through `wsl`, but dispatch fails with `Agent type not found`. Only a session started
inside WSL can use them:

```
export CL_ANCHOR=/home/void/GitHub/PlatformManifest
cd /home/void/GitHub/OperationsCenter && claude
```

## Worktrees — who owns which index

Linked worktrees have separate index files, so commits in different worktrees cannot race.
Check before assuming a branch is free; a branch checked out in one worktree cannot be
checked out in another.

```
git worktree list
```

As of 2026-08-23: the primary checkout plus `/home/void/oc-fleet-main` (detached HEAD — what
the fleet executes; its `src/` is dead code, an editable install resolving to the primary
checkout).
