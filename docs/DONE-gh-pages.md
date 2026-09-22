# Web page generation

*Status: generator + Makefile target delivered (2026-09-22); CI plan wiring
delivered (2026-09-22); workflow files remain.*

## Vision

The output of the CI/CD pipeline, beyond the release artifacts, should be
an updated web site with:

* automatically generated documentation (linked from the main page)
* a link to the release page
* **Released page status** — focused on the fix_spec APIs: how different
  the release client is from the officially published spec
* **Nightly test status** — a report of the nightly build results,
  emphasising API specification correctness, with all test results shown

The original question — separate web site vs sphinx-generated — is
answered in [Design decisions].

## What already exists

Everything the site needs is produced by machinery that is already in the
repo:

- **Docs**: `sphinx/` project + `make sphinx-html` → `sphinx/_build/html`.
- **Release status data**: `kavita_quirks.yaml` — the machine-readable
  source of truth for exactly "how different the release client is from
  the published spec" (fixes F1–F10 with endpoints/fields, quirk statuses
  and categories, `covered_by`, upstream references such as Q01 →
  Kareadita/Kavita#4934 and Q18a →
  `docs/issue-draft-library-delete-multiple.md`).
- **Report data (split by line)**: the release and nightly runs write
  disjoint report sets, so they never overwrite each other:
  - `reports/junit-release.xml` (curated) + `reports/schemathesis-release-junit.xml` (canary) — release line
  - `reports/junit-nightly.xml` (curated) + `reports/schemathesis-nightly-junit.xml` (canary) — nightly line
  - `reports/junit-nightly.previous.xml` — CI handoff for the upstream-fixed flag

## Design decisions

1. **One static-site build that embeds the sphinx output** (the answer to
   the open question): sphinx keeps its authoring experience; the status
   pages are data renderers, not documentation. The site build copies
   `sphinx/_build/html` in as `docs/` and generates the status pages from
   the registry + junit files. One artifact, one deploy.
2. **Status pages are dumb generators** (`pages/` scripts run through
   `pys.sh`, matching the repo's style): registry → release page, junit
   XML → nightly page. No framework, no server.
3. **Single "latest" site + a version stamp** (KAVITA_VERSION, image,
   timestamp). Per-release archives are a later option, not the first cut.
4. **Upstream-fixed flag**: on the nightly page, an xfail that *starts
   passing* is highlighted — that is the retirement signal the xfail
   machinery exists for, and it deserves prominence, not a green line.
5. **Deployment**: GitHub Pages via the standard
   `actions/upload-pages-artifact` + `actions/deploy-pages` pair, emitted
   by the CI plan's workflows (`docs/TODO-ci-plan.md`). Local preview:
   `make gh-pages` + `python -m http.server` in the build dir.

## Work items

### A1 — site skeleton + release page generator

- `pages/build_site.py`: renders into `gh-pages/`:
  - `index.html` — links to `docs/`, the release status page, the nightly
    status page, and the GitHub Release; version stamp + build timestamp.
  - `status/release.html` — from `kavita_quirks.yaml`: the fix list
    (F1–F10, title, endpoints/fields), the quirk ledger (status,
    category, `covered_by`, notes), links to upstream issues, and the
    explicit "what the release client patches vs the published spec"
    framing — plus the **release test run** section
    (`reports/junit-release.xml` + `reports/schemathesis-release-junit.xml`).
  - `status/nightly.html` — from `reports/junit-nightly.xml` +
    `reports/schemathesis-nightly-junit.xml`: pass/fail/xfail tables per
    module and per check, a legend for the nightly xfail semantics ("green
    = known upstream bugs still present"), and the upstream-fixed flag
    (vs `reports/junit-nightly.previous.xml`).
- Renders hand-written HTML via string templates; no dependencies beyond
  PyYAML + the stdlib XML parser.

### A2 — docs embedding + Makefile target

- `make gh-pages`:
  1. `make sphinx-html`
  2. `./pys.sh pages/build_site.py` (copies the sphinx output to
     `gh-pages/docs/`, generates the status pages).
- Local preview documented in the target help text.

### A3 — CI wiring (`docs/TODO-ci-plan.md`)

*(Plan wiring delivered 2026-09-22; the workflow files remain.)*

- Release workflow: after a green run, `make gh-pages` and upload the
  artifact; a `pages` job deploys it.
- Nightly workflow: `make gh-pages` refreshes the nightly page and the
  tracking issue gets the page link (the existing tracking-issue flow).
- The site deployment is **non-blocking** for the release artifacts.

### A4 — verification

- Dry-run `pages/build_site.py` against the current `reports/` +
  registry: the release page lists the registry plus the release test run;
  the nightly page renders the last nightly junit runs; the upstream-fixed
  flag triggers on a synthetic junit with a passing former-xfail.

## Sequencing

| Item | Effort | Notes |
|---|---|---|
| A1 | **done** (2026-09-22) | `pages/build_site.py`: index + release page (registry + release test run) + nightly page (nightly curated + canary junit, upstream-fixed flag vs `reports/junit-nightly.previous.xml`) + sphinx copy |
| A2 | **done** | `make gh-pages` builds the site into `gh-pages/` |
| A4 | **done** | `tests/test_pages.py` (3 tests incl. the synthetic previous-junit flag) |
| A3 | **done** (plan edits) | CI plan wiring in `docs/TODO-ci-plan.md` (pages section, outputs table, previous-junit handoff); workflow files pending |

## Open questions

- [ ] Per-release page archives vs single latest (deferred, see decision 3).
- [ ] Whether the nightly page embeds the full Schemathesis junit or a
      summary with a link to the artifact.
- [ ] Whether the "upstream fixed" flag also auto-opens/updates the
      tracking issue or just links to it (see `docs/TODO-schemathesis.md`
      findings-disposition decision).

*Status: A1/A2/A4 delivered (2026-09-22); A3 plan wiring delivered
(2026-09-22) — workflow files remain; the open questions are still open.*
