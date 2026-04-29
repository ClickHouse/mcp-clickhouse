---
name: release-prep
description: Prepare a release by bumping the version, updating the changelog, and syncing the lock file on a release branch. Use when the user wants to cut a new release.
argument-hint: <version>
---

# Release Prep

Prepare a release for version `$ARGUMENTS`.

## Steps

### 1. Validate the version argument

The user must supply a version number (e.g. `0.4.0`). If `$ARGUMENTS` is empty or missing, ask the user to provide one and stop.

### 2. Switch to main and pull latest

```
git checkout main
git pull origin main
```

If there are uncommitted changes, warn the user and stop.

### 3. Create the release branch

```
git checkout -b release/$ARGUMENTS
```

If the branch already exists locally or on the remote, do not silently reuse it. Stop and ask the user how to proceed; the existing branch may have stale or in-progress work.

### 4. Bump the version in pyproject.toml

Find the line `version = "X.Y.Z"` in `pyproject.toml` and replace `X.Y.Z` with `$ARGUMENTS`. Capture the old version first so the summary in Step 7 can quote `<old> -> <new>` accurately. Do not reformat the file or touch any other field.

### 5. Update the changelog

Open `CHANGELOG.md` and find the `## Unreleased` section. Rename it to `## $ARGUMENTS - <today's date in YYYY-MM-DD format>`. Insert a new empty `## Unreleased` heading immediately above the renamed section, with a blank line between them:

```
## Unreleased

## $ARGUMENTS - YYYY-MM-DD

### Added
...
```

If the Unreleased section has no entries, run `git log <last-tag>..HEAD --oneline` to see commits since the last release and populate entries categorized under `### Added`, `### Changed`, `### Fixed`, or `### Removed`.

Match the existing link style. Every entry references the originating issue or PR like:

```
- Description of change. ([#171](https://github.com/ClickHouse/mcp-clickhouse/pull/171))
```

Use that exact shape so the new section reads consistently with the rest of the file.

### 6. Sync the lock file

Run `uv lock` to regenerate `uv.lock`. This is needed because the package's own version in `pyproject.toml` changed and is referenced in the lockfile. Do not run `uv sync` or `uv build` here.

### 7. Summary

Show the user a short summary:
- The branch you are on
- The version bump (`<old> -> $ARGUMENTS`)
- The changelog section that was added
- Confirmation that `uv.lock` was regenerated

Do not commit. Let the user review and commit when ready.

## What happens after merge

For reference, do not perform these steps as part of release-prep:

1. The release branch is merged to `main`.
2. A GitHub Release is published (e.g. tag `v$ARGUMENTS`). This fires `.github/workflows/release-docker.yml` and pushes the image to GHCR.
3. The PyPI publish runs via manual `workflow_dispatch` of `.github/workflows/publish.yml`.

Do not push tags from the release branch and do not invoke either workflow yourself.
