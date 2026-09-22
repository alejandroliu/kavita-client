# Test dependencies

* Docker - run kavita containers
* pandoc, typst and TTF font
  * pandoc loremipsum.md -o file.pdf --pdf-engine typst -V mainfont="liberation sans"
* poppler, pdf to cbz conversion
* openssl - generates the self-signed CA and server certificate for the
  OIDC TLS fixture (`test_oidc.py`)
* calibre - ebook-meta commands (active since coverage-plan Phase 2: sets
  series/index/publisher/tags on the sample epub and pdf; `pdf-to-cbz`
  reads them back into the embedded ComicInfo.xml)
* Docker images for companion services (coverage-plan Phase 3):
  * axllent/mailpit - SMTP capture for the email flows
  * ghcr.io/soluto/oidc-server-mock - OpenID Connect testing (runs behind
    a self-signed TLS certificate on a static docker network)
  * python:3-alpine - runs the local TLS mock of api.github.com /
    raw.githubusercontent.com (`github_mock` fixture, DESIGN quirk 23)

# CI (GitHub Actions)

GitHub-hosted `ubuntu-latest` runners provide Docker, pandoc,
poppler-utils and openssl out of the box.  The workflows install the rest
via `.github/actions/setup-test-tools` (calibre, `fonts-liberation`,
typst, plus the preinstalled ones for belt-and-braces).  Every
docker-backed job (release verification, nightly stable/dev lines, the
optional PR integration run) uses it.

If a tool is missing at run time anyway, the `media_library` fixture
skips the media tests with a clear message (`media generation tools are
not available: ...`) rather than failing the suite — the run stays green
but with reduced media coverage, which is worth checking in the logs.

