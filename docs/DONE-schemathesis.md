# Schemathesis contract surveillance (TODO)

Plan for a **schema-driven, auto-generated test layer** that calls every
operation in the OpenAPI document and checks that the server's responses
match the documented contract. Generated from the spec itself, it
self-updates: new endpoints get a test, removed endpoints drop out,
renamed parameters and changed response shapes are flagged on the first
run. This is the *breadth* layer; the curated suite (180 ops, strong
assertions) stays the *certification* layer — see `docs/spec-coverage.md`.

*Status: delivered and verified in both modes, phase 2 included (2026-09-22); CI wiring remains.*

## Goals

- Call all **517 operations** with requests built from the spec
  signatures (parameter names, query/body shapes, enums, formats).
- Assert, per call: the response status is one the spec documents, and
  **2xx bodies validate against the documented schema** (plus
  content-type where documented).
- Cover the 337 operations the curated suite never calls, at shallow
  depth, and keep doing so automatically across spec bumps.
- Run in CI as the upstream-drift canary (see `docs/TODO-ci-plan.md`
  Workflows 1 and 3), **not** as the release green gate initially.

## Tool

[Schemathesis](https://schemathesis.readthedocs.io/) — OpenAPI 3 +
Hypothesis; generates valid-ish inputs per schema (correct types,
enum-valid values, format-aware strings), sends them, and validates
responses against the schema. Pytest plugin and CLI; JUnit XML output.
Add to `requirements.txt` (unpinned, matching current policy; pin later).

## Design decisions

1. **Target document — patched for release, raw for nightly.**
   - Release: `kavita_0.9.1.4.fixed.json` — validates the contract the
     published client actually expects, so the fix_spec fixes are not
     re-flagged as noise.
   - Nightly/dev: `kavita_DEV.json` (raw) — the known upstream bugs are
     filtered by the exclusion list instead of `fix_spec.py`, so a *new*
     mismatch shows up as a new failure. (The curated nightly suite
     already tracks the known bugs as xfail; Schemathesis adds breadth.)
2. **Known quirks = exclusion lists generated from the registry**
   (`kavita_quirks.yaml` — the same source fix_spec.py reads): each
   quirk entry carries a `schemathesis` rule (`exclude-both`,
   `exclude-nightly`, `include`), and `schemathesis_exclusions.py`
   renders the per-mode lists. Never hand-edit the lists; add or fix a
   quirk in the registry. `tests/test_quirks_registry.py` audits the
   derivation and every entry's `covered_by` references.
3. **Assertion strength is deliberately shallow**: "a documented status
   + schema-valid 2xx body". A `400` validation error passes if the spec
   documents a 400 — this layer detects *signature/contract* drift, not
   happy paths. The curated suite owns happy paths.
4. **Server**: a dedicated throwaway container per run (the existing
   `kavita_instance.py` fixture pattern), separate from the curated
   suite's session — Schemathesis sends mutations, so it must not share
   state with the green gate. Exclude the curated destructive list
   (`Settings/reset`, `Users/delete-user`, `Server/cleanup`,
   `Series/delete-multiple`) so a run never bricks its own server
   mid-scan. Start it with the same GitHub-mock pinning as the fresh
   fixture (`--add-host` + CA, DESIGN quirk 23), so the network-backed
   Server endpoints are deterministic instead of excluded.
5. **Auth**: a small setup step registers the admin and logs in (exactly
   the curated fixture's first steps), then supplies the JWT (and API key
   for the `/api/Opds/{apiKey}/…` routes) to Schemathesis via `--auth` /
   case hooks. Anonymous fallback for the rest.

## Configuration sketch

```toml
# schemathesis.toml
[schemathesis]
checks = all                # status, schema, content-type
max-examples = 1            # one case per operation: 517 requests
base-url = "http://127.0.0.1:5000"
exclude-path = [ ... ]      # quirk list + non-goals + destructives (see above)
```

Makefile targets next to the existing ones:

- `make schemathesis-release` — patched spec vs `jvmilazz0/kavita:latest`
- `make schemathesis-nightly` — raw dev spec vs `jvmilazz0/kavita:nightly`

JUnit XML to `reports/schemathesis-release-junit.xml` /
`reports/schemathesis-nightly-junit.xml` (the curated
`reports/junit-*.xml` files stay separate).

## Value-provider registry (phase 2, optional)

By default Schemathesis generates random ints for ids, so stateful
endpoints land on 4xx and only get signature-level checking. To deepen
those: a small registry mapping parameter identities (`seriesId`,
`chapterId`, `libraryId`, …) to fixture-derived values, via case hooks.
Start empty; fill entries from the first failure review, prioritized by
what the consumer application calls. This is where the two layers meet —
an op graduating to "needs 200 evidence" moves to the curated suite
(one sweep line), per the contract in `docs/spec-coverage.md`.

## Onboarding (first iteration)

*Prototype run done (2026-09-21)*: coverage phase + conformance checks
against the fresh mock-pinned server. The triage found two fix-class spec
bugs (`Annotation/all-for-series` array-vs-object, `Reader/prompt-reread/*`
null-vs-object) — rescued as Fix 7/Fix 8 — and the noise buckets are now
encoded in the registry: Q26 (undocumented auth, 43 endpoints), Q27
(server 500s on garbage, 53), Q28 (stateful-id, 306), plus the
schema-wide date-time quirks (Q29a 7-digit fractions, Q29b
`0001-01-01` MinValue). Follow-up iterations of the canary found Fix 9
(nullable `RereadDto` chapter refs + `SideNavStreamDto.externalSource`/
`library`) and Fix 10 (`Stats/device/device-type` object-vs-array).

**Config-file approach chosen (keeps the CLI-only decision):**
`schemathesis.toml` carries
`[checks.response_schema_conformance] validate-formats = false` — the
Q29a/Q29b tolerance, applied without any Python wrapper. Exclusion lists
are generated from the registry per mode (`schemathesis_exclusions.py`)
and passed as `--exclude-path` flags. The canary is now **green**:
80 operations tested, 0 failures (down from 489), converging over ~5
triage rounds — the Makefile target that boots the stack and runs this
*Verification complete (2026-09-22)*: `make schemathesis-release` and
`make schemathesis-nightly` both run green end-to-end (stack boot ~1 min
+ run ~3 s — far under the 15-min target; teardown via trap). The drift
sandbox was proven: renaming a passing op's path in a scratch spec is
flagged as exactly one failure (exit 1) against a green control — note
that renames of *optional* query params are invisible by design (the
server tolerates the missing param and answers 200), while path/status/
schema drift is caught. The nightly run surfaced two dev-spec stragglers
(`Person/coversdb-image`, `Review/my-series`), folded into Q28.

Remaining:

1. **CI wiring** — `docs/TODO-ci-plan.md`: the release canary is a
   non-gating report in Workflow 1 (release) and keeps the site's release
   page fresh in Workflow 2 job A (stable line); the nightly canary is the
   drift signal in Workflow 2 job B (dev line), feeding the
   tracking-issue flow.
2. **Value-provider registry (phase 2)** — *delivered 2026-09-22*:
   `schemathesis_stack.py --populate` builds one of each entity kind,
   `kavita_providers.yaml` maps parameter names to state keys,
   `schemathesis_hooks.py` injects them (`hooks` in schemathesis.toml,
   so the run stays pure CLI), and the canary re-includes any bucket op
   whose parameters are fully provider-covered — minus the `no-provider`
   blocklist (57 ops) whose semantics providers cannot fix (Kavita+
   gating, pending-user-only, not-yet-generated covers, OPDS content
   negotiation, entity-kind mismatches). Both modes green: 57/58 ops
   re-included, 0 failures. Boundary rule: the canary asserts shape, the
   curated suite asserts semantics — neither replaces the other.

## Success criteria

- Green run with the exclusion list in place (both modes).
- A deliberate drift introduced in a sandbox (rename a query param in a
  scratch spec) is flagged.
- New spec versions require no test code changes — only a regenerated
  client and, rarely, exclusion-list edits.

## Open decisions

- [x] Pin Schemathesis (and its Hypothesis dependency) or follow the
      current unpinned policy — **decided 2026-09-21: pin both exactly**
      (verified pair: `schemathesis==4.27.5`, `hypothesis==6.168.0`, now
      in requirements.txt): Schemathesis is a judgment tool — check
      semantics, generated values, and CLI flags all churn between
      releases, so unpinned upgrades produce false-positive "drift"
      failures indistinguishable from the real signal. A bump becomes a
      deliberate single-variable change: update the pins, run the target,
      triage new failures as check-semantics changes vs genuine
      mismatches. This is also the first step of the broader
      generator/pytest pinning question in `docs/TODO-ci-plan.md` (leave
      that one for later).
- [x] Pytest-plugin integration (fixture reuse) vs standalone CLI in its
      own job (isolation) — **decided 2026-09-21: standalone CLI**; the
      plugin would couple the mutation-sending run to the curated
      session's ordering.
- [x] Exclusion-list maintenance: a hand file, or generated from a
      `QUIRKS` source of truth shared with `docs/DESIGN.md` (drift risk
      otherwise) — **decided 2026-09-21: registry** (`kavita_quirks.yaml`,
      YAML, single source of truth for fix_spec patch data + per-mode
      exclusion lists via `schemathesis_exclusions.py`), audited by
      `tests/test_quirks_registry.py`. The stateful-id category is
      populated by the prototype triage.
- [ ] Whether Schemathesis findings automatically open upstream reports
      or just the tracking issue. Start with the tracking issue only.

## Interplay with the curated work

Schemathesis is expected to independently find the same class of bug as
the fix_spec fixes — e.g. the `series-detail-plus` null list field and
`file-breakdown` object-vs-array (`docs/TODO-coverage-next.md` Batch B).
When a fix lands, the endpoint moves from the exclusion list to the
green set, and the curated test covers the happy path.

*Status: delivered and verified in both modes, phase 2 included (2026-09-22); remaining: CI jobs.*
