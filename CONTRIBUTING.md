# Contributing

`openenv-sprites` is experimental. Small, test-backed changes are preferred,
especially while OpenEnv and Sprites provider contracts continue to evolve.

## Setup

Install [uv](https://docs.astral.sh/uv/) and use Python 3.10 or newer:

```console
uv sync --extra dev
```

## Verify changes

Run the same checks used by CI:

```console
uv run ruff check .
uv run pytest
uv build
uv run twine check dist/*
```

Provider lifecycle changes should include failure-path tests. Changes that can
create billable Sprites must keep live tests opt-in and document cleanup behavior.
Never put Sprites tokens or environment credentials in tests, fixtures, command
arguments, issue reports, or recorded output.

## Pull requests

- Explain the user-visible behavior and compatibility impact.
- Add or update tests and documentation when behavior changes.
- Keep unrelated changes out of the pull request.
- Confirm all required CI checks pass.

Maintainers may use the audited ruleset bypass for urgent or administrative
changes. Normal contributions should use a reviewed pull request.

## Releases

1. Update `version` in `pyproject.toml` and `__version__` expectations in tests.
2. Move changelog entries under a dated release heading.
3. Confirm required CI passes on `main`.
4. Push a `vX.Y.Z` tag whose version exactly matches `pyproject.toml`.
5. Approve the protected `pypi` environment deployment when prompted.
6. Confirm the `Publish` workflow and PyPI attestations succeed.

PyPI uses Trusted Publishing rather than a stored API token. Before the first
release, configure a pending publisher with these exact values:

- PyPI project: `openenv-sprites`
- GitHub owner: `superfly`
- GitHub repository: `openenv-sprites`
- Workflow: `publish.yml`
- Environment: `pypi`

The package name is not reserved until that first publish completes.
