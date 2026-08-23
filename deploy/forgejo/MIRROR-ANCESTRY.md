# Why `github/main` has to stay an ancestor, and what breaks when it does not

The forge pushes `main` to GitHub through a push mirror. GitHub's `main` is a
protected branch, so it accepts a fast-forward and nothing else. A fast-forward
is only possible while `github/main` is still an ancestor of the forge's `main`.
The moment it is not, every mirror run fails with:

```
remote: error: GH006: Protected branch update failed for refs/heads/main.
```

and the only git-side answer is a force-push, which is exactly what the
protection exists to refuse. There is no configuration that resolves this from
the forge end.

## Reconciling the two histories

The forge and GitHub grew separate histories during the cutover. The fix is to
**merge** `github/main` into the forge's `main`. That produces a commit with two
parents, the second being `github/main` — which is what puts it back in the
ancestry and lets the mirror fast-forward again.

## The trap: never squash a reconciliation PR

The entire value of such a PR is its second parent. Squashing collapses it to a
single-parent commit carrying the same tree. The content still lands, everything
looks merged, and `github/main` silently stops being an ancestor — so the mirror
keeps failing GH006 and it is not obvious why.

This is not hypothetical. On 2026-08-21 PR #14 was merged with squash by the
`pr_review_watcher`, which hard-coded `merge_method="squash"`. Forge `main`
became a single-parent commit, the reconciliation was undone at the moment it
landed, and it had to be redone.

Two things guard against a repeat:

* The watcher's merge method is no longer hard-coded — it reads
  `OC_MERGE_METHOD`, defaulting to `squash` for ordinary PRs. An ancestry PR is
  landed with `OC_MERGE_METHOD=merge`.
* A reconciliation PR changes **zero files**. If you are looking at one and the
  diff is empty, that is correct and expected — the content is already on `main`.
  The commit exists for its parents, not its tree.

## Checking it

```
git merge-base --is-ancestor github/main origin/main && echo ok
```

If that prints nothing, the mirror is broken and no amount of re-running it will
help until the ancestry is restored.

## The other half: GH007

Restoring the ancestry clears GH006 only. The mirror can also be refused with:

```
remote: error: GH007: Your push would publish a private email address.
```

That one is not a git problem. It fires when a commit being pushed carries an
address GitHub has been told to keep private, and it is cleared from the account
settings at `github.com/settings/emails` — or by rewriting the offending commits
to use the account's `users.noreply.github.com` address. Both halves have to be
clear before the mirror actually runs.

## When the mirror actually runs

Two triggers, and it matters which one you are waiting for:

| Setting | Value |
|---|---|
| `interval` | `8h` |
| `sync_on_commit` | `true` |
| retry on failure | none |

`sync_on_commit` is what you see almost all the time — a push to the forge
triggers a sync within seconds. The 8-hour interval is the floor for a
repository nobody is touching. Observed gaps on 2026-08-22/23 show both:

```
22:01 → 06:08   8h07m   the interval firing; nothing was pushed overnight
06:28 → 06:37     9m
06:53 → 06:57     4m    commit-driven, tracking the PRs landing
07:19 → 07:22     3m
```

Irregular short gaps clustered around merges, one clean 8-hour gap when the
repo was idle. That is push-driven with a timer floor, not a poll.

**There is no retry and therefore no backoff.** A failed sync is simply not
retried — it waits for the next commit or the next 8-hour tick. This is worth
knowing in both directions. A transient failure can leave GitHub stale for up
to 8 hours with nothing reporting it, so `last_error` on the push-mirror config
is the thing to check, not the absence of noise. And a *persistent* failure
will not hammer GitHub, which is why the GH006/GH007 rejections that ran for
two days cost nothing beyond a stale mirror.

Read the live values rather than trusting this table:

```
curl -s -H "Authorization: token $TOKEN" \
  http://localhost:3000/api/v1/repos/Operations_Center_Admin/OperationsCenter/push_mirrors
```

`last_error` is empty when the mirror is healthy. A non-empty `last_error` with
a recent `last_update` means the sync ran and was rejected — check for GH006
(ancestry, see above) and GH007 (an address GitHub is told to keep private on a
commit it has not already accepted).
