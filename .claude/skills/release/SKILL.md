---
name: release
description: Prepare and ship a new mcp-clickhouse release — bump the version, update the changelog, refresh the lockfile, then walk through tagging, PyPI, Docker, and MCP Registry publishing. Use when asked to cut a release, bump the version, or prep a release PR.
argument-hint: "[version]"
allowed-tools: Read, Edit, Bash, AskUserQuestion
---

# Release mcp-clickhouse

This project has no automated release pipeline beyond a version-bump PR — everything after
that is a chain of manual GitHub Actions runs. This skill drives the whole chain, but treats
anything that pushes, tags, or publishes as requiring explicit user confirmation first — those
are visible/irreversible actions, not local edits.

## Step 1: Determine the target version

Read the current version:

```
grep -m1 '^version' pyproject.toml
```

`$ARGUMENTS` is the target version hint (e.g. `0.4.1`, with or without a leading `v`). Strip any
leading `v`.

- If no argument was given, propose the next **patch** bump (increment the last segment of the
  current version) and confirm it with the user via AskUserQuestion before proceeding.
- Validate the target is a valid `X.Y.Z` semver and strictly greater than the current version.
  If not, stop and ask.

## Step 2: Preflight

- `git status --short` must be clean. If dirty, stop and tell the user — do not stash or discard
  anything automatically.
- Switch to `main` and pull to make sure it's current:
  ```bash
  git checkout main
  git pull
  ```
- Re-read the version from `pyproject.toml` now that `main` is current. Re-run the Step 1 default
  and the "strictly greater" check against it. If it changed, stop and ask.
- Create a local release branch off the now-current `main`:
  ```bash
  git checkout -b release/X.Y.Z
  ```
- Check `CHANGELOG.md` has a non-empty `## Unreleased` section (at least one bullet under it). If
  it's empty, warn the user there may be nothing to release yet and confirm before continuing.

## Step 3: Version bump edits

Make these three edits (this mirrors past release-prep commits, e.g. `5645bc9`):

1. **`pyproject.toml`** — bump `version = "..."` to the target version.
2. **`CHANGELOG.md`** — rename the `## Unreleased` heading to `## X.Y.Z - <date>`. Get the date
   with `date +%Y-%m-%d` — never guess or hardcode it.
3. **`uv.lock`** — run `uv lock` so the `mcp-clickhouse` package entry's version matches.

Do **not** touch `server.json` — its version fields are rewritten automatically from
`pyproject.toml` by the MCP Registry publish workflow (Step 6) at publish time.

## Step 4: Review & hand off

Stage just those three files (`git add pyproject.toml CHANGELOG.md uv.lock`, never `git add -A`),
then show `git diff --staged` for review.

Do not commit, push, or open a PR unless the user explicitly asks you to. Leave the change staged
on the `release/X.Y.Z` branch for their review.

Everything below only applies **after** the version-bump PR has merged to `main`.

## Step 5: Tag and cut the GitHub release

Tagging and releasing is what triggers `release-docker.yml`, which auto-publishes multi-arch
Docker images to GHCR (`ghcr.io/clickhouse/mcp-clickhouse:X.Y.Z`, `:X.Y`, `:latest`).

**Confirm with the user before running any of this** — pushing a tag and publishing a GitHub
release are visible, hard-to-reverse actions.

Tag the merged commit on `main`, not the local release branch:

```bash
git checkout main
git pull
git tag vX.Y.Z
git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes "<paste the new CHANGELOG.md section>"
```

## Step 6: Publish to PyPI

Manual only — `publish.yml` has no automatic trigger.

**Confirm with the user before running** — this publishes an immutable artifact to PyPI.

```bash
gh workflow run publish.yml --ref vX.Y.Z
```

Wait for it to succeed (`gh run list --workflow=publish.yml`) before moving on.

## Step 7: Publish to the MCP Registry

Must run **last** — `publish-mcp.yml` verifies both the PyPI package and the GHCR image exist
for this version before publishing, and it rewrites `server.json`'s version fields from
`pyproject.toml` automatically.

Check the `release-docker.yml` run for `vX.Y.Z` has succeeded first
(`gh run list --workflow=release-docker.yml`).

**Confirm with the user before running.**

```bash
gh workflow run publish-mcp.yml -f ref=vX.Y.Z
```

## Order summary

1. Bump version/changelog/lockfile → merge to `main` (Step 3–4)
2. Tag `vX.Y.Z` + GitHub release → Docker image auto-publishes to GHCR (Step 5)
3. Run `publish.yml` → PyPI (Step 6)
4. Run `publish-mcp.yml` → MCP Registry, must be last (Step 7)
