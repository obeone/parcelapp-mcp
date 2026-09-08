# Changelog

Generated from the Conventional Commits in the git history. Do not edit by
hand: run `uvx git-cliff -o CHANGELOG.md` instead.

## 0.2.1 - 2026-09-08

### Documentation

- Rewrite the README around the published artefacts
- Document how to verify the image signatures

### Build and CI

- Publish the image to GHCR and Docker Hub on a tag
- Sign the published images with cosign
- Keep the Docker Hub description in step with the README
- Drop the Mermaid diagram from the Docker Hub description
- Make the Docker Hub description sync actually reachable
## 0.2.0 - 2026-09-06

### Features

- Per-request API key, rate-limit budget, filtering and a resource
- Containerise the HTTP transport

### Bug fixes

- Pin setup-uv to an existing ref

### Refactoring

- Adopt src layout, rename to parcelapp-mcp, move live scripts
- Split the HTTP client out of the server module

### Documentation

- Add CLAUDE.md with architecture and rate-limit invariants
- Add the MIT licence and rewrite the README for publication
- Rewrite the README for the two transports
- Record the HTTP-transport invariants in CLAUDE.md
- Generate the changelog from the conventional commits

### Tests

- Add a pytest suite mocking the upstream API with respx

### Build and CI

- Publish to PyPI on a released tag
- Derive the version from the git tag
- Drive the whole release from a tag push
- Grant the publish build job contents read

### Chores

- Add ruff, strict mypy and a CI workflow
- Untrack the design brief
- Fill in the package, image and repository metadata
- Ignore the local worktrees directory

