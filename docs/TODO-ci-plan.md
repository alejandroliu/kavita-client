# CI/CD plan (TODO)

Status: **workflow files added (2026-09-22), not yet run on GitHub Actions**.
The test suite and the Makefile packaging targets this describes exist.

## Goals

- **Tagged releases**: verify the generated client against a *pinned* Kavita
  version, then publish the built client on this repo's GitHub Release page as
  the "verified/working" artifact other developers can reuse.
- **Nightly tracking**: detect regressions in (a) hotfix releases of the stable
  line and (b) the Kavita dev branch / our own pipeline, and flag them.
- **Published site**: the nightly workflow owns and deploys the GitHub
  Pages site — API docs (release version) plus the release/nightly status
  pages (`docs/DONE-gh-pages.md`); the release workflow triggers a refresh.

The test suite (`make test-release` / `make test-nightly`,
`make test-fix_spec`) is
the shared machinery; CI only changes *what* runs against *which* container,
via environment variables the suite already honors:

| Variable | Meaning | Default |
|---|---|---|
| `KAVITA_IMAGE` | container image under test | `jvmilazz0/kavita:latest` |
| `KAVITA_PULL` | image refresh policy: `missing`/`never`/`always` | `missing` |
| `KAVITA_EXPECTED_VERSION` | if set, a test asserts the server reports exactly this version | unset |
| `KAVITA_READY_TIMEOUT` | seconds to wait for first boot | `240` |
| `KAVITA_SCAN_TIMEOUT` | seconds to wait for a library scan to finish | `180` |
| `KAVITA_MODE` | client under test: `release` (patched) or `nightly` (raw spec; known upstream bugs recorded as xfail) | `release` |

Image/tag conventions (verified 2026-09):

- `jvmilazz0/kavita:latest` and `jvmilazz0/kavita:0.9.1` are the same digest
  (the v0.9.1.4 stable build). Kavita's Docker stable tags use 3 components
  (`0.9.1`) while GitHub releases use 4 (`v0.9.1.4`).
- `jvmilazz0/kavita:nightly` = `nightly-0.9.1`, the dev build from `develop`.
- All of these are *movable* tags. Pinning for a release therefore means
  recording the digest at release time (`:0.9.1@sha256:…`).

## Workflow 1 — Release (on tag)

Trigger: tag push of `v<VER>`, e.g. `v0.9.1.4` (this repo's tags mirror
Kavita's release tags), plus `workflow_dispatch` (with version/image inputs,
so the pipeline can be exercised without pushing a tag).

1. Extract the Kavita version from the tag name (strip the leading `v`).
2. Fetch `https://raw.githubusercontent.com/Kareadita/Kavita/v<VER>/openapi.json`.
3. `fix_spec.py` (invoked with `<VER>` as the tag argument, so the generated
   client's version matches the tag) → `openapi-python-client generate`
   (generator intentionally unpinned — see Decisions). The Makefile passes
   `KAVITA_VERSION` from the environment (`KAVITA_VERSION ?=`), so
   `KAVITA_VERSION=<VER> make package` is all that is needed.
4. Build the client: `make package` → wheel + sdist in `dist/`, plus the
   release zip `dist/kavita-client-<VER>.zip`. `make test-release` installs
   the built wheel, so the suite tests the exact artifact that will be
   published.
5. Resolve the server image: `docker pull jvmilazz0/kavita:latest`, record the
   resulting digest, run `make test-release` against `latest@sha256:…`
   with `KAVITA_IMAGE` set to the digest form,
   `KAVITA_EXPECTED_VERSION=<VER>` and `KAVITA_PULL=never` (the image was
   just pulled). A test asserts the running server reports
   `<VER>` (via `GET /api/Server/server-info-slim` → `kavitaVersion`), which
   is the digest/version cross-check.
6. If green: attach `dist/kavita-client-<VER>.zip` (the wheel + sdist, i.e.
   the installable `kavita-client`) to the GitHub Release, and record the
   verified image digest in the release body.
7. Failures block the release.
8. If green: build the API docs from the release client (`make sphinx-html`)
   and attach `docs.zip` to the GitHub Release — this is what the nightly
   site build imports as `docs/` (the site always shows the *exact
   released* docs, not a regeneration).
9. Trigger the nightly workflow (`gh workflow run nightly.yml`) so the
   site refreshes the same day. The release workflow never deploys Pages
   itself — the nightly workflow is the site's single deployer.

## Workflow 2 — Nightly, stable + dev lines (cron)

Trigger: nightly cron + `workflow_dispatch`.

One workflow, three jobs — both test lines plus a single site deploy, so
there is exactly **one deployer** of the "latest" site and the two report
sets can never overwrite each other.

**Job A — stable line** (`make test-release`):

- Client: the released client — checkout of the latest release tag (`main`
  as a fallback before the first release).
- Image: `jvmilazz0/kavita:latest`, `KAVITA_PULL=always` — catches hotfix
  releases that move the `latest` tag and regress the API.
- Also runs the `make schemathesis-release` canary.
- Outputs: `reports/junit-release.xml` +
  `reports/schemathesis-release-junit.xml`.

**Job B — dev line** (`make test-nightly`):

- Client: generated fresh from the `develop` branch (`kavita_DEV.json` +
  `make build-nightly`); generator version may be unpinned here — nothing
  is committed or published, so drift is irrelevant.
- Image: `jvmilazz0/kavita:nightly`, `KAVITA_PULL=always`.
- Also runs the `make schemathesis-nightly` canary.
- The tests that depend on a `fix_spec.py` fix record the still-present
  upstream bug as xfail (`expect_upstream_fix`), and the offline suite
  records the spec-version/tag mismatch as xfail too — so a green run means
  "the known bugs are still there, nothing changed". When upstream fixes
  one, the corresponding test stops xfailing and starts passing — review
  and retire the fix. Any *other* failure is a real regression.
- Outputs: `reports/junit-nightly.xml` +
  `reports/schemathesis-nightly-junit.xml`.

**Job C — pages** (runs after A and B, **whether they passed or failed** —
the site exists to show the failures):

- Downloads the same-run report artifacts from A and B, restores
  `reports/junit-nightly.previous.xml` from the previous run's artifact
  (the upstream-fixed flag handoff), and downloads `docs.zip` from the
  latest GitHub Release (`gh release download`; before the first release
  the docs link is simply omitted).
- `pages/build_site.py --docs-from <unzipped docs>` → renders
  `gh-pages/` → `actions/upload-pages-artifact` + `actions/deploy-pages`.
- Single deployer: no overwrite between the lines, no race.

**Failure handling** (jobs A and B): **do not block anything**. Update the
persistent tracking issue (see Decisions) with the JUnit breakdown
(`--junitxml`) listing exactly which endpoints broke — stable line
regressions and dev-line drift land in the same issue, each labelled with
its line; the comment links the site page.

## Workflow 3 — PR / push (fast gate)

- Offline unit tests only (`make test-offline` — the fix_spec, quirks
  registry, pages and docker-guard tests).
- Docker integration runs optionally (labels / `workflow_dispatch`) to save CI
  minutes; the image pull alone is ~250 MB. The optional run uploads the
  JUnit XML as a run artifact.
- No pages publishing here: the gate runs before anything is verified, so
  it must not touch the published site.

## Pages publishing (Workflow 2, job C)

`make gh-pages` renders `gh-pages/`: the API docs (as `docs/`), the
release status page (`kavita_quirks.yaml` registry + the release test run)
and the nightly status page (the dev-line test run, with the "upstream
fixed" flags computed against `reports/junit-nightly.previous.xml`). See
`docs/DONE-gh-pages.md`. In CI, job C runs
`pages/build_site.py --docs-from <docs.zip unpacked>` instead of the
sphinx copy, so the docs always match the exact released client.

### Report artifacts (split by line)

The release and nightly runs write disjoint report sets, so they never
overwrite each other and the site renders both:

| Report | Written by |
|---|---|
| `reports/junit-release.xml` | `make test-release` (workflow 1; nightly job A) |
| `reports/junit-nightly.xml` | `make test-nightly` (nightly job B) |
| `reports/junit-nightly.previous.xml` | CI handoff (previous nightly run) |
| `reports/schemathesis-release-junit.xml` | `make schemathesis-release` |
| `reports/schemathesis-nightly-junit.xml` | `make schemathesis-nightly` |

- Deploy: `actions/upload-pages-artifact` + `actions/deploy-pages`. The
  repo's Pages source must be set to "GitHub Actions"; the deploy job needs
  `pages: write` and `id-token: write` and runs against the `github-pages`
  environment.
- Single "latest" site with a version stamp; per-release archives deferred
  (DONE-gh-pages decision 3).
- Non-blocking by design: a pages failure never unpublishes the release zip
  and never fails the nightly signal.
- Previous-junit handoff: job B uploads `junit-nightly.previous.xml` as a
  run artifact; the next run's job C downloads it before building the
  site, so the upstream-fixed flag compares consecutive runs.

## Workflow outputs

What each workflow leaves behind, besides the pass/fail status:

| Workflow | Durable outputs | Diagnostics |
|---|---|---|
| 1 — Release (tag) | GitHub Release with `kavita-client-<VER>.zip` (wheel + sdist) and `docs.zip` (sphinx); release body records the verified image digest; triggers the nightly site refresh | `reports/junit-release.xml` + `reports/schemathesis-release-junit.xml` uploaded as run artifacts |
| 2 — Nightly (stable + dev) | **published site** (docs + release status + release test run + nightly test run) | `reports/junit-release.xml`, `reports/junit-nightly.xml` + both schemathesis junits as run artifacts; update/comment on the persistent tracking issue; `junit-nightly.previous.xml` handoff artifact |
| 3 — PR / push | — | JUnit XML run artifact only when the optional integration run is enabled |

The durable outputs of the CI setup are the release zip + docs.zip attached
to the GitHub Release and the published Pages site (single deployer: the
nightly workflow); the nightlies exist to *observe and flag*, so their
outputs are diagnostic (artifacts + issue comments) plus the refreshed
site.

## Dependency drift (decided: track, don't gate)

The original idea was for CI to fail when a fresh generation differs from the
released client. Decided against it:

- A diff check is only meaningful when the generator is pinned; with
  dependencies deliberately unpinned (see Decisions), "drift" has no fixed
  reference point.
- Drift still surfaces, as regressions, through the nightly jobs: a generator
  or dependency change that breaks generation or behavior shows up in the
  tracking issue like any other regression (Workflow 2, jobs A and B).

## Decisions (2026-09-22)

- [x] **No pinning.** `requirements.txt` stays deliberately unpinned; the
      nightly jobs are the drift tracker for the generator and other
      dependencies as well as for Kavita itself.
- [x] **Release artifact format:** zip of wheel + sdist
      (`make package` → `dist/kavita-client-<VER>.zip`) on the GitHub
      Release. PyPI publishing is deferred (revisit only if the client is
      consumed via `pip install`).
- [x] **Tag naming convention:** `v<VER>` (e.g. `v0.9.1.4`), mirroring
      Kavita's tags; the release workflow strips the leading `v`.
- [x] **Nightly failures:** one persistent tracking issue that gets updated
      per failing run, rather than a new issue per failure.
- [x] **Pages publishing:** single "latest" site (`make gh-pages`),
      deployed non-blocking via the GitHub Pages actions
      (`docs/DONE-gh-pages.md`, decision 5).

## Known live-server quirks (from test runs, v0.9.1.4 source)

Confirmed against the live server; the tests accommodate all of them (see
`docs/DESIGN.md` → "Live-server quirks"):

- String-schema endpoints (`/api/Health`, `/api/Account/invite-url`,
  `/api/Upload/upload-by-file`, `/api/Account/opds-url`,
  `/api/Settings/base-url`, `/api/Metadata/language-title`,
  `/api/Series/age-rating`, `/api/Account/forgot-password`) return bare
  (non-JSON) text bodies, which the generated client parses with
  `response.json()` — it crashes. `fix_spec.py` Fix 3 (added 2026-09-20)
  rewrites their media types so the client reads `response.text`; the
  nightly build records the still-present upstream bug as xfail.
- Nullable enum fields (`SeriesDto.metadataProviderOverride`,
  `ChapterDto.format`, `MetadataSettingsDto.filterAboveWeight`,
  `TachiyomiChapterDto.format`) are `null` on the server but declared
  non-nullable in the spec. `fix_spec.py` Fix 2 rewrites them to the
  `allOf` + `nullable` form the generator honors; the nightly build xfails
  the dependent tests while upstream keeps the fields non-nullable.
- `info.version` in the tagged document is one step behind the tag
  (`0.9.1.1` in the `v0.9.1.4` document). `fix_spec.py` Fix 4 (added
  2026-09-20) rewrites it, with a warning on stderr, and the offline suite
  records the mismatch as xfail until upstream aligns them.
- `POST /api/Account/register` is first-user-only (`400` once an admin
  exists).
- `POST /api/Account/invite` null-derefs `roles`/`ageRestriction` if omitted
  (`400` generic). With email unconfigured it returns `emailSent: false`,
  `invalidEmail: true` plus a working `emailLink`; with SMTP configured
  (Mailpit fixture) it sends real mail (`emailSent: true`).
- `GET /api/Account/invite-url` needs the id of a *pending* user (the
  admin's is already email-confirmed) and `withBaseUrl`.
- `POST /api/Library/create` needs a `metadataProvider` valid for the
  library type; `scan-all` silently skips libraries; overlapping scans are
  queued hours out (scan sequentially and wait).
- Book libraries only parse files in series subfolders. With metadata
  processing off, the epub parser needs the "Series vNN" filename pattern;
  with it on (the fixture's setting), embedded ebook-meta metadata is
  honored and the bare filename parses.
- A loose-leaf cbz (no volume marker) lands in `SeriesDetailDto.specials`,
  not `chapters`/`volumes`; the tests read it from there.
- The container must run with `--user` (the test fixture does), otherwise
  root-owned files accumulate in the mounted config dir and temp cleanup
  fails with warnings.
