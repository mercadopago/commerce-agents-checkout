# Releasing to PyPI

The distribution name and the import package differ:

```text
mercadopago-commerce-agents-checkout   # pip install
mercadopago_commerce_agents            # import
```

Claim the adjacent `mercadopago-commerce-agents` defensively at the same time: it matches
the import name, so it is what someone will guess, and leaving it unregistered is a
dependency-confusion opening.

## Release gates

Before the first public release:

1. Complete the real test-credential Orders API and hosted Checkout Pro validation.
2. Complete joint integration validation with at least one independent consumer.
3. Obtain WebSec approval for the library and its reference host flow.
4. Confirm ownership of the PyPI project name and maintainer accounts.
5. Enable GitHub Actions and configure Trusted Publishers for `.github/workflows/release.yml`:
   organization `mercadopago`, repository `commerce-agents-checkout`, and environments
   `testpypi` and `pypi` in their respective registries.
6. Protect both GitHub environments; require explicit reviewer approval for `pypi`.
7. Protect `v*` tags against unauthorized creation, update, and deletion; the workflow
   also rejects tags whose signature GitHub does not verify.
8. Confirm README, the `Apache-2.0` license expression, `LICENSE`, `NOTICE`, and project
   URLs.

Do not store a long-lived PyPI API token in the repository. Prefer PyPI Trusted
Publishing with GitHub's OIDC identity and an explicitly protected release environment.

## Local artifact validation

Use a clean checkout and remove old local artifacts before starting. Then run:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install build twine pylint isort
.venv/bin/python -m unittest discover -s tests
.venv/bin/pylint --max-line-length=100 src/mercadopago_commerce_agents examples
.venv/bin/isort --check-only --diff src tests examples
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Inspect the wheel and source archive to confirm that they contain only intended public
files and no credentials, local configuration, test output, or private dependencies.

## Publication sequence

1. Choose and commit a semantic version in `pyproject.toml`.
2. Push a signed commit and signed version tag.
3. Publish a GitHub release for that tag. The release workflow verifies the tag signature
   and that its commit belongs to `main`, reruns unit/lint/contract gates, builds once,
   publishes the artifact to TestPyPI, and waits at the protected `pypi` environment.
4. Install the TestPyPI artifact into a clean environment and rerun the import/smoke
   checks before approving the production environment.
5. Approve `pypi`; the workflow publishes the unchanged artifact to PyPI.
6. Verify the project page, install command, metadata, hashes, and provenance.
7. Record the release URL, artifact hashes, and compatibility pin in the release notes.

The first public version should not be published while the repository still says the
real Orders flow or WebSec review is pending.
