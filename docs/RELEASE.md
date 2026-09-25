# Release process

Releases are driven by git tags through GitHub Actions. Pushing a `vX.Y.Z` tag
to `main` triggers
[`.github/workflows/release.yml`](../.github/workflows/release.yml), which
builds the distribution, validates it with `twine`, publishes to PyPI via OIDC
trusted publishing, and creates a GitHub Release with the wheel, sdist, and
changelog body attached.

## Pre-release checklist

- [ ] Bump `version` in `pyproject.toml`. Nothing else carries a version:
  `gcmon.__version__` and `gcmon --version` follow from it.
- [ ] Add a `## Version X.Y.Z (YYYY-MM-DD)` section to `CHANGELOG.md` (below
  the `## WIP` block).
- [ ] Open a PR **from a branch named `release/...`**; the workflow's PR run
  is gated on that prefix, so a branch named anything else gets no validation
  and says nothing about it. On a `release/` branch CI fails if the changelog
  section is missing or version-mismatched.
- [ ] Merge the PR after CI is green.
- [ ] Tag the merge commit (`git tag vX.Y.Z <sha>`) and push the tag
  (`git push origin vX.Y.Z`).

The `release` environment on GitHub must have at least one approver
configured; the publish step will wait for approval otherwise.

## Preview the changelog

The release workflow extracts the version's section from `CHANGELOG.md` and
embeds it as the GitHub Release body. Preview locally before tagging:

```bash
python .github/scripts/extract_changelog.py          # WIP section
python .github/scripts/extract_changelog.py latest   # uses pyproject.toml version
python .github/scripts/extract_changelog.py v0.2.0   # explicit tag
```

The script writes directly to `$GITHUB_OUTPUT` in CI.

## Manual / dry-run

Run the workflow without publishing via **Actions → Release → Run workflow**:

| Input      | Default  | Effect                                              |
|------------|----------|-----------------------------------------------------|
| `dry_run`  | `false`  | Skip PyPI publish and GitHub Release steps.         |
| `ref`      | `main`   | Branch or tag to check out (useful for previews).   |

Use `dry_run: true` to validate a release-candidate commit or to test a
workflow change before the next real release.

## Troubleshooting

**Tag pushed but no release happened.** The `release` environment is waiting
for approval, or the workflow's `concurrency` group is locked by a previous
in-flight run. Check the Actions tab and the environment's review queue.

**`twine check` failed.** The built `dist/*.whl` or `dist/*.tar.gz` has
invalid metadata. Common cause: a syntax error in `README.md` (PyPI's Markdown
renderer is strict). Fix and re-tag.

**Release body is empty / GitHub Release notes are missing the changelog.**
The `## Version X.Y.Z` section was missing for the tag's version. The script
prints `::error::No changelog section for version 'X.Y.Z'.` and lists the
headers it found. Add the section, fix, and re-tag.

**Re-running a failed release.** Tags are immutable in git. Bump the version
(e.g., `0.1.0` → `0.1.1`) or delete and recreate the tag. The
`concurrency: cancel-in-progress: false` setting prevents accidental
cancellation of an in-flight publish.

## Versioning policy

[SemVer](https://semver.org/). Pre-release tags (`v0.2.0a1`, `v1.0.0rc2`) are
matched against the same version string in `CHANGELOG.md`. The
`pyproject.toml` version and the tag must match exactly, with no `+local` or
`.dev` suffixes.

`pyproject.toml` is the single source. `gcmon.__version__` reads the version
back from the installed distribution's metadata and `gcmon --version` prints
it, so never write the number down a second time.

Bump `pyproject.toml` in a dev tree without reinstalling and both report the
older number, the one `pip show gcmon` also gives. Import gcmon from a
checkout you never installed and you get `0.0.0+unknown`, which is not a
release, so the no-suffix rule above does not reach it.
