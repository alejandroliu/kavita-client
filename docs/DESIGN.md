# Kavita client — design

This repository generates a Python client for the
[Kavita](https://www.kavitareader.com/) API from Kavita's published
`openapi.json`, patches known spec problems, and verifies the result against a
real Kavita server running in Docker.

## Goals

- **For the author**: a pinned, working client for a specific Kavita release,
  used by an application under development.  This makes use of fix_spec.py
  to reach a working release.
- **For the community**: each release is verified against the matching Kavita
  server and published on this repo's GitHub Release page as a
  "verified/working" artifact, so other developers can reuse it.
  This uses the fix_spec to create a working release.
- **Later**: track Kavita's dev builds and flag API regressions. (See
  `docs/TODO-ci-plan.md` for the CI/CD plan; the workflows are not
  implemented yet.) The dev-line check does **NOT** make use of
  `fix_spec.py` — the client is generated from the raw dev spec. Tests for
  the known spec bugs record them as `xfail` when they are still present
  upstream, so a nightly run also tells us when upstream fixes them.

## Pipeline

```text
Kavita openapi.json (release tag or develop branch)
        │
        ▼
  fix_spec.py ────── patches known spec issues (release build only)
        │
        ▼
  openapi-python-client generate ──► <VER>-fixed/ (patched) or DEV/ (raw)
        │
        ▼
  test suite (unit + docker integration)
```

The `Makefile` drives the pipeline:

- `kavita_<VER>.json` — downloaded from
  `https://raw.githubusercontent.com/Kareadita/Kavita/v<VER>/openapi.json`
- `kavita_DEV.json` — downloaded from the `develop` branch (nightly builds)
- `kavita_<VER>.fixed.json` — output of `fix_spec.py`, invoked with the
  release tag so `info.version` matches it
- `make build` / `make build-nightly` — generate the client from the
  patched tagged spec into `<VER>-fixed/kavita-client/`, or from the raw
  develop spec into `DEV/kavita-client/`, with `openapi-client-config.yml`
- `make test-release` / `make test-nightly` — install the selected client
  in the venv and run the full suite (writes `reports/junit-release.xml`
  or `reports/junit-nightly.xml`)
- `make test-fix_spec` — offline unit tests only

The pipeline's Python steps run through `pys.sh`, which manages the
`.venv` virtual environment (created from `requirements.txt`);
`pdf-to-cbz` is invoked directly by the media fixture.

## Spec fixes (fix_spec.py)

`fix_spec.py` patches known problems in Kavita's document before code
generation.  this is driven by the `kavita_quirks.yaml` registry.

## Test suite

Two layers, run by pytest, plus a schemathesis run.

### Offline unit tests (`tests/test_fix_spec*.py`)

No network, no Docker, fast. These cover `fix_spec.py` only:

- `test_fix_spec.py` — behavior of the patching functions (all three fixes)
  on small synthetic spec documents (all content types rewritten, unrelated
  parts untouched, idempotence, return values).
- `test_fix_spec_cli.py` — the command line interface (exit codes, output
  file, warning when nothing to patch).
- `test_fix_spec_regression.py` — re-applies `fix_spec.py` to the downloaded
  original spec and asserts it matches the generated fixed spec, so the two
  cannot drift apart. A second test compares the spec's `info.version`
  against the release tag (derived from the filename) and records the
  upstream mismatch as xfail while it remains.

### Docker integration tests (`tests/integration/`)

Everything else is tested against a real Kavita server in a throwaway Docker
container — the user's explicit requirement. The suite is "contract-first":
assertions are limited to what the spec documents (status codes, response
shapes), so the same suite doubles as the regression probe against dev
builds later.

The suite runs in two modes (`KAVITA_MODE`): `release` (default) against the
patched client, and `nightly` against the client generated from the raw
upstream spec. Tests that depend on a `fix_spec.py` fix call
`expect_upstream_fix()` when the known bug still shows up on the nightly
build, recording it as `xfail` — so a nightly run answers "is the bug still
there upstream?", and the same run turns green (the test starts passing)
once upstream fixes it.

**Fixture lifecycle** (`tests/integration/conftest.py`, session-scoped):

1. Pull the image (`KAVITA_IMAGE`, refresh policy via `KAVITA_PULL`).
2. `docker run -d --rm` with a random host port mapped to container port
   5000 and a writable temp config volume at `/kavita/config`. The
   container runs as the current uid (`--user`), so files it writes into
   the temp config dir stay removable by the test process.
3. Wait for readiness by polling `GET /api/Health` (first boot runs EF
   migrations), bounded by `KAVITA_READY_TIMEOUT`.
4. Register the first admin account (the first registered user on a fresh
   server becomes admin), log in to obtain the JWT.
5. Yield a `KavitaInstance` (base URL, credentials, anonymous `Client`,
   authenticated `AuthenticatedClient`, reported server version).
6. Teardown: `docker rm -f` always.

The whole layer is skipped automatically when Docker is unavailable.

A second session fixture, `media_kavita_server`, starts the same container
with an extra `/media` volume. The `media_library` fixture (session-scoped)
populates it with sample content generated from `tests/data/loremipsum.md`
via pandoc/typst, calibre's `ebook-meta`, and the repo's `pdf-to-cbz`
script: two comic series (`Lorem` with a ComicInfo.xml-carrying
volume-patterned cbz plus a loose-leaf cbz that parses as a special, and
`ipsum`), a book series with an epub whose series/volume come from embedded
ebook-meta metadata and a pdf, and `tests/data/cover.jpg` as the comic's
folder cover. It creates a manga and a book library (metadata processing
enabled, matching off), scans each sequentially (Kavita queues overlapping
scans, so they must not run concurrently), posts reading progress on the
first comic chapter, and creates an API key (required by the Reader
image/thumbnail and Panels endpoints). It skips when the media tooling is
unavailable. The populated-library tests live in `tests/integration/`
(`test_media.py`, `test_reader.py`, `test_download.py`, `test_image.py`,
`test_chapter.py`, `test_series_extra.py`, `test_metadata_extra.py`,
`test_small_families.py`).

**Companion services:**

- `mailpit` — an `axllent/mailpit` sidecar; `mail_kavita_server` boots a
  fresh Kavita server, points its SMTP at Mailpit through the generated
  Settings POST (`IsEmailSetup()` needs `hostName` + SMTP host + sender
  address), and `test_email.py` drives the forgot-password and invite
  flows, asserting real delivery through Mailpit's HTTP API.
- `oidc_env` (in `test_oidc.py`) — `ghcr.io/soluto/oidc-server-mock`
  behind a self-signed TLS certificate on a static docker network; the
  Kavita container trusts the generated CA via `SSL_CERT_FILE`, OIDC is
  configured through the generated Settings POST (authority must match the
  mock's issuer exactly, `customScopes` empty), and the container is
  recreated on the same config volume so the auth scheme registers at
  startup. The login challenge is asserted to reach the provider; the
  mock's own scope-parsing bug blocks its final login page.
- Both sidecars skip when their images are not pulled (like the media
  tooling).

**Environment variables:**

| Variable | Meaning | Default |
|---|---|---|
| `KAVITA_IMAGE` | container image under test | `jvmilazz0/kavita:latest` |
| `KAVITA_PULL` | image refresh policy: `missing`/`never`/`always` | `missing` |
| `KAVITA_READY_TIMEOUT` | seconds to wait for first boot | `240` |
| `KAVITA_SCAN_TIMEOUT` | seconds to wait for a library scan to finish | `180` |
| `KAVITA_EXPECTED_VERSION` | if set, assert the server reports exactly this version | unset |
| `KAVITA_MODE` | client under test: `release` (patched, default) or `nightly` (raw spec; known upstream bugs recorded as xfail) | `release` |

**Markers and tiers:**

- `integration` — needs a running Kavita container.
- `release` — the gate for publishing a release: the auth flow
  (`test_auth.py`) and the invite contract (`test_invite.py`, the endpoint
  `fix_spec.py` Fix 1 exists for).
- `compat` — a broad sweep of endpoints: the fresh-install reads
  (`test_server.py`: settings, stats, libraries, jobs, email history,
  roles, server info; `test_sweep.py`: 68 read-only operations from
  coverage-plan Phase 1) and the populated-library flows (coverage-plan
  Phase 2: `test_media.py` plus the Reader/Download/Image/Chapter/Series/
  Metadata/Person/Tachiyomi/Panels/ColorScape modules). Used for
  regression tracking.

**Test targets:**

```text
make test-release    full suite against the patched client (writes reports/junit-release.xml)
make test-nightly    full suite against the raw develop-spec client (known upstream bugs recorded as xfail; writes reports/junit-nightly.xml)
make test-fix_spec   offline unit tests only
make schemathesis-release  canary: patched spec vs the stable image (reports/schemathesis-release-junit.xml)
make schemathesis-nightly  canary: raw dev spec vs the nightly image (reports/schemathesis-nightly-junit.xml)
```

### Import strategy (`conftest.py`)

The tests import an *installed* `kavita_client` package; the
`make test-release` / `make test-nightly` recipes install the selected
client into the venv first (and uninstall it afterwards), so the suite
always exercises the exact generated artifact. Running pytest directly
falls back to the generated source trees (the patched build first, then any
other).

### Schemathesis

The contract-surveillance layer (`docs/TODO-schemathesis.md`): Schemathesis
generates a case per operation from the spec and checks the server's
responses against the documented contract (status codes, 2xx body schemas).
The exclusion lists are generated from `kavita_quirks.yaml` by
`schemathesis_exclusions.py` — never hand-edited; `tests/test_quirks_registry.py`
audits the registry and its `covered_by` references. The schema-wide
date-time quirks (Q29a/Q29b) are tolerated via `schemathesis.toml`
(`validate-formats = false`). The prototype is green (80 ops, 0 failures)
and has already produced Fix 7–Fix 10. The network-backed Server
endpoints are deterministic via the `github_mock` fixture. Phase 2 adds
value providers (`kavita_providers.yaml` + `schemathesis_hooks.py`):
the stack populates one of each entity kind and the hooks inject real ids,
so 57 previously-excluded stateful ops are schema-checked on real data.

## Auth flow notes

- `POST /api/Account/register` and `POST /api/Account/login` both return a
  `UserDto`; its `token` field carries the JWT.
- `AuthenticatedClient(base_url=…, token=…)` sends
  `Authorization: Bearer <token>`.

## Version / image conventions

Kavita's Docker stable tags use three components while GitHub releases use
four:

| Purpose | Docker image | GitHub source |
|---|---|---|
| latest stable | `jvmilazz0/kavita:latest` (= `0.9.1`, same digest) | `v0.9.1.4` release tag |
| dev builds | `jvmilazz0/kavita:nightly` (= `nightly-0.9.1`) | `develop` branch |

All of these tags are *movable*; a release therefore pins the digest
recorded at release time (`:0.9.1@sha256:…`).


## Coding style

- 2-space indentation, single quotes, `'''…'''` docstrings.
- Type annotations on function declarations.
- Docstrings in MyST Markdown (Sphinx-compatible).
- Tests: plain `assert` statements, pytest fixtures and markers.

