'''Docker-backed fixtures: run integration tests against a real Kavita server.

Environment variables:

- `KAVITA_IMAGE`: container image under test
  (default `jvmilazz0/kavita:latest` for `release` mode,
  `jvmilazz0/kavita:nightly` for `nightly` mode)
- `KAVITA_PULL`: image refresh policy, `missing`/`never`/`always`
  (default `missing`)
- `KAVITA_READY_TIMEOUT`: seconds to wait for first boot (default `240`)
- `KAVITA_SCAN_TIMEOUT`: seconds to wait for a library scan to finish
  (default `180`)
- `KAVITA_EXPECTED_VERSION`: if set, tests assert the server reports exactly
  this version (default unset)

Two session fixtures start containers:

- `kavita_server`: a fresh, empty server (the fresh-install tests), with
  api.github.com pinned to the local TLS mock (`github_mock` fixture) so
  the update-check endpoints are hermetic.
- `media_kavita_server`: same, with a `/media` volume the `media_library`
  fixture populates with generated sample books/comics before creating
  libraries and scanning.
'''

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from kavita_client import AuthenticatedClient, Client
from kavita_client.api.account import (
  post_api_account_create_auth_key,
  post_api_account_login,
  post_api_account_register,
)
from kavita_client.api.library import post_api_library_create, post_api_library_scan
from kavita_client.api.reader import post_api_reader_progress
from kavita_client.api.series import (
  get_api_series_series_detail,
  get_api_series_volumes,
  post_api_series_v_2,
)
from kavita_client.api.server import get_api_server_server_info_slim
from kavita_client.api.settings import get_api_settings, post_api_settings
from kavita_client.models import (
  FileTypeGroup,
  LibraryType,
  LoginDto,
  MangaFormat,
  MetadataProvider,
  ProgressDto,
  RegisterDto,
  RotateAuthKeyRequestDto,
  SeriesDto,
  SeriesFilterV2Dto,
  SmtpConfigDto,
  UpdateLibraryDto,
)

from kavita_instance import KavitaInstance, MediaLibrary, expect_upstream_fix

KAVITA_IMAGE = os.environ.get('KAVITA_IMAGE',
                              'jvmilazz0/kavita:nightly'
                              if os.environ.get('KAVITA_MODE', 'release') == 'nightly'
                              else 'jvmilazz0/kavita:latest')
KAVITA_PULL = os.environ.get('KAVITA_PULL', 'missing')
KAVITA_READY_TIMEOUT = float(os.environ.get('KAVITA_READY_TIMEOUT', '240'))
KAVITA_SCAN_TIMEOUT = float(os.environ.get('KAVITA_SCAN_TIMEOUT', '180'))

# Local TLS mock of api.github.com / raw.githubusercontent.com, so the
# Server update-check endpoints (changelog, check-update, …) are hermetic:
# no GitHub rate limiting, no hang when GitHub is unreachable (DESIGN
# quirk #23). Runs in a python:3-alpine container on its own network.
GITHUB_MOCK_IMAGE = 'python:3-alpine'
GITHUB_MOCK_NETWORK = 'kavita-github-mock'
GITHUB_MOCK_SUBNET = '172.30.0.0/29'
GITHUB_MOCK_IP = '172.30.0.2'
GITHUB_KAVITA_IP = '172.30.0.3'

CONTAINER_PORT = 5000  # the port Kavita serves inside the container
MEDIA_MOUNT = '/media'  # where the sample media volume is mounted

ADMIN_USERNAME = 'it-admin'
ADMIN_PASSWORD = 'it-admin-password'
ADMIN_EMAIL = 'it-admin@example.com'

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / 'tests' / 'data'
PDF_TO_CBZ = ROOT / 'pdf-to-cbz'

# 'Lorem' comes from ComicInfo (metadata on); 'ipsum' is folder-derived
# (no ComicInfo in that cbz), and Kavita keeps folder-derived names
# lowercase.
COMIC_SERIES_NAME = 'Lorem'
SECOND_SERIES_NAME = 'ipsum'
MANGA_LIBRARY_NAME = 'it-manga'
BOOK_LIBRARY_NAME = 'it-books'


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
  return subprocess.run(['docker', *args], capture_output=True, text=True)


def _docker_available() -> bool:
  return shutil.which('docker') is not None and _docker(['info']).returncode == 0


def _image_present(image: str) -> bool:
  return _docker(['image', 'inspect', image]).returncode == 0


def _pull(image: str) -> None:
  proc = _docker(['pull', image])
  if proc.returncode != 0:
    raise RuntimeError(f'docker pull {image} failed: {proc.stderr.strip()}')


def _container_running(name: str) -> bool:
  proc = _docker(['inspect', '-f', '{{.State.Running}}', name])
  return proc.returncode == 0 and proc.stdout.strip() == 'true'


def _mapped_port(container: str, container_port: int = CONTAINER_PORT) -> int:
  proc = _docker(['port', container, f'{container_port}/tcp'])
  if proc.returncode != 0:
    raise RuntimeError(f'docker port failed: {proc.stderr.strip()}')
  for line in proc.stdout.splitlines():
    _, _, port = line.rpartition(':')
    if port.isdigit():
      return int(port)
  raise RuntimeError(f'no host port mapping found: {proc.stdout!r}')


def _wait_for_ready(name: str, base_url: str, timeout: float) -> None:
  '''Poll the health endpoint until the server answers.

  First boot runs EF migrations, which can take a while.
  '''
  deadline = time.monotonic() + timeout
  last_error = 'no attempt made'
  while time.monotonic() < deadline:
    if not _container_running(name):
      logs = _docker(['logs', '--tail', '100', name]).stdout
      raise RuntimeError(f'Kavita container exited during startup. Last logs:\n{logs}')
    try:
      with httpx.Client(timeout=5.0) as client:
        response = client.get(f'{base_url}/api/Health')
      if response.status_code == 200:
        return
      last_error = f'HTTP {response.status_code}'
    except httpx.HTTPError as exc:
      last_error = str(exc)
    time.sleep(2.0)
  raise RuntimeError(
    f'Kavita did not become ready within {timeout}s (last error: {last_error})'
  )


def _kavita_server(
  tmp_path_factory: pytest.TempPathFactory,
  with_media: bool,
  network: str | None = None,
  ip: str | None = None,
  extra_env: dict[str, str] | None = None,
  extra_volumes: list[str] | None = None,
  extra_args: list[str] | None = None,
) -> Iterator[KavitaInstance]:
  '''Start a throwaway Kavita container, register the admin, and log in.

  `network`/`ip` pin the container to a docker network with a static IP
  (needed by the OIDC TLS fixture); `extra_env`/`extra_volumes`/`extra_args`
  are added to the `docker run` arguments (e.g. the self-signed CA for
  OIDC, the GitHub-mock pinning for the fresh server).
  '''
  if not _docker_available():
    pytest.skip('docker is not available')

  if KAVITA_PULL == 'always' or (KAVITA_PULL == 'missing' and not _image_present(KAVITA_IMAGE)):
    _pull(KAVITA_IMAGE)

  # Kavita may run as any uid inside the container, so make the config and
  # media directories writable by everyone.
  config_dir = tmp_path_factory.mktemp('kavita-config')
  config_dir.chmod(0o777)
  media_dir = ''
  if with_media:
    media_path = tmp_path_factory.mktemp('kavita-media')
    media_path.chmod(0o777)
    media_dir = str(media_path)

  name = f'kavita-client-test-{uuid.uuid4().hex[:8]}'
  args = [
    'run', '-d', '--rm',
    '--name', name,
    # Run as the current user so files written into the mounted directories
    # are removable by the test process afterwards.
    '--user', f'{os.getuid()}:{os.getgid()}',
    '-p', f'127.0.0.1::{CONTAINER_PORT}',
    '-v', f'{config_dir}:/kavita/config',
  ]
  if media_dir:
    args += ['-v', f'{media_dir}:{MEDIA_MOUNT}']
  if network is not None:
    args += ['--network', network]
    if ip is not None:
      args += ['--ip', ip]
  if extra_env is not None:
    for key, value in extra_env.items():
      args += ['-e', f'{key}={value}']
  if extra_volumes is not None:
    for volume in extra_volumes:
      args += ['-v', volume]
  if extra_args is not None:
    args += extra_args
  args.append(KAVITA_IMAGE)

  run = _docker(args)
  if run.returncode != 0:
    pytest.fail(f'docker run failed: {run.stderr.strip()}')

  try:
    host_port = _mapped_port(name)
    base_url = f'http://127.0.0.1:{host_port}'
    _wait_for_ready(name, base_url, KAVITA_READY_TIMEOUT)

    client = Client(base_url=base_url, raise_on_unexpected_status=True)

    # The first registered user on a fresh server becomes the admin.
    register = post_api_account_register.sync_detailed(
      client=client,
      body=RegisterDto(username=ADMIN_USERNAME, password=ADMIN_PASSWORD, email=ADMIN_EMAIL),
    )
    if register.status_code != 200 or register.parsed is None:
      pytest.fail(f'admin registration failed: HTTP {register.status_code} {register.content!r}')

    login = post_api_account_login.sync_detailed(
      client=client,
      body=LoginDto(username=ADMIN_USERNAME, password=ADMIN_PASSWORD),
    )
    if login.status_code != 200 or login.parsed is None or not login.parsed.token:
      pytest.fail('admin login failed or did not return a token')

    token = login.parsed.token
    admin_client = AuthenticatedClient(base_url=base_url, token=token, raise_on_unexpected_status=True)

    server_version = ''
    info = get_api_server_server_info_slim.sync_detailed(client=admin_client)
    if info.parsed is not None and info.parsed.kavita_version is not None:
      server_version = info.parsed.kavita_version

    yield KavitaInstance(
      image=KAVITA_IMAGE,
      base_url=base_url,
      admin_username=ADMIN_USERNAME,
      admin_password=ADMIN_PASSWORD,
      admin_user_id=login.parsed.id,
      token=token,
      server_version=server_version,
      media_dir=media_dir,
      client=client,
      admin_client=admin_client,
    )
  finally:
    _docker(['rm', '-f', name])


@pytest.fixture(scope='session')
def github_mock(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
  '''Local TLS mock of api.github.com / raw.githubusercontent.com.

  Pins the update-check endpoints to deterministic responses (DESIGN
  quirk #23): the fresh server's changelog/check-* calls no longer hit
  the real GitHub (rate limits, hangs when unreachable). Yields the
  docker network, the mock/Kavita IPs, and the certs dir.
  '''
  if _docker(['info']).returncode != 0:
    pytest.skip('docker is not available')
  if _docker(['image', 'inspect', GITHUB_MOCK_IMAGE]).returncode != 0:
    _pull(GITHUB_MOCK_IMAGE)

  certs_dir = tmp_path_factory.mktemp('github-certs')
  subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                  '-keyout', str(certs_dir / 'ca.key'), '-out', str(certs_dir / 'ca.crt'),
                  '-days', '7', '-subj', '/CN=kavita-github-mock-ca',
                  '-addext', 'basicConstraints=critical,CA:TRUE',
                  '-addext', 'keyUsage=critical,keyCertSign'],
                 check=True, capture_output=True)
  subprocess.run(['openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
                  '-keyout', str(certs_dir / 'server.key'), '-out', str(certs_dir / 'server.csr'),
                  '-subj', '/CN=api.github.com',
                  # IP:127.0.0.1 is only for the host-side readiness probe;
                  # Kavita itself connects by hostname.
                  '-addext', 'subjectAltName=DNS:api.github.com,DNS:raw.githubusercontent.com,IP:127.0.0.1'],
                 check=True, capture_output=True)
  subprocess.run(['openssl', 'x509', '-req', '-in', str(certs_dir / 'server.csr'),
                  '-CA', str(certs_dir / 'ca.crt'), '-CAkey', str(certs_dir / 'ca.key'),
                  '-CAcreateserial', '-out', str(certs_dir / 'server.crt'),
                  '-days', '7', '-copy_extensions', 'copyall'], check=True, capture_output=True)

  _docker(['network', 'rm', GITHUB_MOCK_NETWORK])
  if _docker(['network', 'create', '--subnet', GITHUB_MOCK_SUBNET, GITHUB_MOCK_NETWORK]).returncode != 0:
    pytest.skip('could not create the github mock network')

  mock_name = f'kavita-github-mock-{uuid.uuid4().hex[:8]}'
  run = _docker(['run', '-d', '--rm', '--name', mock_name,
                 '--network', GITHUB_MOCK_NETWORK, '--ip', GITHUB_MOCK_IP,
                 '-p', '127.0.0.1::443',
                 '-v', f'{certs_dir}:/certs:ro',
                 '-v', f'{ROOT}/tests/integration/github_mock.py:/github_mock.py:ro',
                 GITHUB_MOCK_IMAGE, 'python3', '/github_mock.py',
                 '--cert', '/certs/server.crt', '--key', '/certs/server.key'])
  if run.returncode != 0:
    pytest.skip(f'github mock start failed: {run.stderr.strip()}')

  try:
    # Wait until the mock serves over TLS (trusting our CA) on the host port.
    host_port = ''
    for line in _docker(['port', mock_name, '443/tcp']).stdout.splitlines():
      _, _, host_port = line.rpartition(':')
      if host_port.isdigit():
        break
    if not host_port:
      pytest.skip('github mock did not publish a host port')
    verify = ssl.create_default_context(cafile=str(certs_dir / 'ca.crt'))
    deadline = time.monotonic() + 60
    ready = False
    while time.monotonic() < deadline:
      try:
        if httpx.get(
          f'https://127.0.0.1:{host_port}/repos/Kareadita/Kavita/releases',
          verify=verify, timeout=5,
        ).status_code == 200:
          ready = True
          break
      except httpx.HTTPError:
        pass
      time.sleep(1)
    if not ready:
      pytest.skip('github mock did not become ready')

    yield {
      'network': GITHUB_MOCK_NETWORK,
      'mock_ip': GITHUB_MOCK_IP,
      'kavita_ip': GITHUB_KAVITA_IP,
      'certs_dir': str(certs_dir),
    }
  finally:
    _docker(['rm', '-f', mock_name])
    _docker(['network', 'rm', GITHUB_MOCK_NETWORK])


@pytest.fixture(scope='session')
def kavita_server(
  tmp_path_factory: pytest.TempPathFactory,
  github_mock: dict[str, str],
) -> Iterator[KavitaInstance]:
  '''A fresh, empty Kavita server, with api.github.com pinned to the local
  TLS mock so the update-check endpoints are hermetic.'''
  yield from _kavita_server(
    tmp_path_factory,
    with_media=False,
    network=github_mock['network'],
    ip=github_mock['kavita_ip'],
    extra_args=[
      '--add-host', f"api.github.com:{github_mock['mock_ip']}",
      '--add-host', f"raw.githubusercontent.com:{github_mock['mock_ip']}",
    ],
    extra_volumes=[f"{github_mock['certs_dir']}/ca.crt:/etc/kavita-ca.crt:ro"],
    extra_env={'SSL_CERT_FILE': '/etc/kavita-ca.crt'},
  )


@pytest.fixture(scope='session')
def media_kavita_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[KavitaInstance]:
  '''A fresh Kavita server with a `/media` volume for sample content.

  Also mounts `tests/data/favicon.ico` at `/favicon.ico` so the OPDS
  favicon endpoint finds one (DESIGN quirk #19 — the container ships no
  `.ico` next to the app dir; the endpoint only checks the extension).
  '''
  yield from _kavita_server(
    tmp_path_factory,
    with_media=True,
    extra_volumes=[f'{DATA_DIR}/favicon.ico:/favicon.ico:ro'],
  )


@pytest.fixture(scope='session')
def currently_reading_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[KavitaInstance]:
  '''Dedicated throwaway server for the `currently-reading` test.

  The endpoint's filter only lists series whose last read is *older* than
  `OnDeckProgressDays` (the comparison is flipped in
  `SeriesFilter.HasReadLast`, DESIGN quirk #15) — unless the setting is 0.
  Setting it to 0 empties on-deck, so the shared media fixture cannot
  carry it; this dedicated container can (coverage-plan "Dedicated
  fixture" pattern).
  '''
  yield from _kavita_server(tmp_path_factory, with_media=True)


@pytest.fixture()
def client(kavita_server: KavitaInstance) -> Client:
  '''Anonymous client (raises on undocumented status codes).'''
  return Client(base_url=kavita_server.base_url, raise_on_unexpected_status=True)


@pytest.fixture()
def admin_client(kavita_server: KavitaInstance) -> AuthenticatedClient:
  '''Client authenticated with the admin JWT.'''
  return AuthenticatedClient(
    base_url=kavita_server.base_url,
    token=kavita_server.token,
    raise_on_unexpected_status=True,
  )


@pytest.fixture()
def media_admin_client(media_kavita_server: KavitaInstance) -> AuthenticatedClient:
  '''Admin client for the media-populated server.'''
  return AuthenticatedClient(
    base_url=media_kavita_server.base_url,
    token=media_kavita_server.token,
    raise_on_unexpected_status=True,
  )


# --- companion services (coverage-plan Phase 3) ------------------------------


@pytest.fixture(scope='session')
def mailpit() -> Iterator[dict[str, str]]:
  '''Mailpit (SMTP capture) sidecar: yields its container IP and API port.

  The Kavita server reaches the SMTP listener on the container IP (port
  1025); the tests read captured mail through the HTTP API on the mapped
  host port.
  '''
  image = 'axllent/mailpit:latest'
  if not _docker_available():
    pytest.skip('docker is not available')
  if not _image_present(image):
    pytest.skip(f'{image} is not pulled (KAVITA_PULL does not apply to companion images)')
  name = f'kavita-client-mailpit-{uuid.uuid4().hex[:8]}'
  run = _docker(['run', '-d', '--rm', '--name', name, '-p', '127.0.0.1::8025', image])
  if run.returncode != 0:
    pytest.skip(f'mailpit start failed: {run.stderr.strip()}')
  try:
    ip = _docker(['inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name]).stdout.strip()
    api_port = _mapped_port(name, 8025)
    yield {'ip': ip, 'api_port': str(api_port)}
  finally:
    _docker(['rm', '-f', name])


@pytest.fixture(scope='session')
def mail_kavita_server(tmp_path_factory: pytest.TempPathFactory, mailpit: dict[str, str]) -> Iterator[KavitaInstance]:
  '''A fresh Kavita server with its SMTP pointed at Mailpit.'''
  gen = _kavita_server(tmp_path_factory, with_media=False)
  server = next(gen)
  try:
    settings = get_api_settings.sync_detailed(client=server.admin_client)
    if settings.status_code != 200 or settings.parsed is None:
      pytest.fail(f'settings fetch failed: HTTP {settings.status_code}')
    # IsEmailSetup() needs HostName + SMTP host + sender address (server source).
    settings.parsed.host_name = 'kavita-test.local'
    settings.parsed.smtp_config = SmtpConfigDto(
      host=mailpit['ip'],
      port=1025,
      sender_address='kavita@example.com',
      sender_display_name='Kavita',
      user_name='',
      password='',
      enable_ssl=False,
      size_limit=0,
    )
    updated = post_api_settings.sync_detailed(client=server.admin_client, body=settings.parsed)
    if updated.status_code != 200:
      pytest.fail(f'smtp configuration failed: HTTP {updated.status_code} {updated.content!r}')
    yield server
  finally:
    next(gen, None)


# --- populated library -------------------------------------------------------


def _media_tools_missing() -> list[str]:
  missing = [tool for tool in ('pandoc', 'typst', 'pdftoppm', 'ebook-meta') if shutil.which(tool) is None]
  if not PDF_TO_CBZ.is_file():
    missing.append('pdf-to-cbz')
  return missing


def _generate_sample_media(media_dir: str) -> None:
  '''Generate sample books and comics from tests/data/loremipsum.md.

  - `books/lorem/`: an epub whose series/volume come from embedded metadata
    (ebook-meta), plus a pdf. Book libraries only parse files inside a
    series subfolder.
  - `comics/lorem/`: a volume-patterned cbz with ComicInfo.xml metadata
    (real chapters), a loose-leaf cbz (parses as a special), and
    `cover.jpg` as the folder cover.
  - `comics/ipsum/`: a second series (volume-patterned cbz).
  '''
  books_dir = Path(media_dir) / 'books' / COMIC_SERIES_NAME
  comics_dir = Path(media_dir) / 'comics' / COMIC_SERIES_NAME
  second_dir = Path(media_dir) / 'comics' / SECOND_SERIES_NAME
  books_dir.mkdir(parents=True)
  comics_dir.mkdir(parents=True)
  second_dir.mkdir(parents=True)

  # Epub: series + volume index are embedded with ebook-meta, so the bare
  # filename does not need the "Series vNN" pattern the parser otherwise
  # requires (DESIGN quirk #5).
  epub = books_dir / 'lorem.epub'
  subprocess.run(
    ['pandoc', 'loremipsum.md', '-o', str(epub), '--metadata=title=Lorem Ipsum'],
    cwd=DATA_DIR, check=True, capture_output=True, text=True,
  )
  subprocess.run(
    ['ebook-meta', str(epub), '--series', 'Lorem', '--index', '1',
     '--publisher', 'Claude Press', '--tags', 'sample,comic'],
    check=True, capture_output=True, text=True,
  )

  # Pdf: the plain version first (its cbz becomes the loose-leaf special),
  # then re-tagged so the regenerated cbz carries ComicInfo.xml.
  pdf = books_dir / 'lorem.pdf'
  subprocess.run(
    [
      'pandoc', 'loremipsum.md', '-o', str(pdf),
      '--pdf-engine', 'typst', '-V', 'mainfont=liberation sans',
      '--metadata=title=Lorem Ipsum',
    ],
    cwd=DATA_DIR, check=True, capture_output=True, text=True,
  )
  subprocess.run([str(PDF_TO_CBZ), str(pdf)], check=True, capture_output=True, text=True)
  shutil.copy(books_dir / 'lorem.cbz', comics_dir / 'lorem.cbz')

  subprocess.run(
    ['ebook-meta', str(pdf), '--title', 'Lorem Ipsum', '--series', 'Lorem',
     '--index', '2', '--publisher', 'Claude Press', '--tags', 'sample,comic'],
    check=True, capture_output=True, text=True,
  )
  subprocess.run([str(PDF_TO_CBZ), str(pdf)], check=True, capture_output=True, text=True)
  shutil.copy(books_dir / 'lorem.cbz', comics_dir / 'lorem v01.cbz')

  # Second series.
  second_pdf = second_dir / 'ipsum.pdf'
  subprocess.run(
    [
      'pandoc', 'loremipsum.md', '-o', str(second_pdf),
      '--pdf-engine', 'typst', '-V', 'mainfont=liberation sans',
      '--metadata=title=Ipsum Dolor',
    ],
    cwd=DATA_DIR, check=True, capture_output=True, text=True,
  )
  subprocess.run([str(PDF_TO_CBZ), str(second_pdf)], check=True, capture_output=True, text=True)
  (second_dir / 'ipsum.cbz').rename(second_dir / 'ipsum v01.cbz')
  second_pdf.unlink()

  shutil.copy(DATA_DIR / 'cover.jpg', comics_dir / 'cover.jpg')


def _create_library(
  admin_client: AuthenticatedClient,
  name: str,
  library_type: LibraryType,
  folder: str,
  file_group_types: list[FileTypeGroup],
  metadata_provider: MetadataProvider,
) -> int:
  resp = post_api_library_create.sync_detailed(
    client=admin_client,
    body=UpdateLibraryDto(
      # The DTO requires an id even for creation; the server ignores it and
      # assigns the real one (the web UI sends 0 the same way).
      id=0,
      name=name,
      type_=library_type,
      folders=[folder],
      folder_watching=False,
      # The dashboard flag gates the on-deck / currently-reading lists.
      include_in_dashboard=True,
      include_in_search=True,
      manage_collections=False,
      manage_reading_lists=False,
      allow_scrobbling=False,
      allow_metadata_matching=False,
      # Metadata processing must be on: ComicInfo.xml and embedded epub
      # metadata (ebook-meta) are only parsed when it is (verified live).
      # Matching stays off, so no online provider lookups happen.
      enable_metadata=True,
      remove_prefix_for_sort_name=False,
      inherit_web_links_from_first_chapter=False,
      file_group_types=file_group_types,
      exclude_patterns=[],
      # The server rejects the DTO unless a provider valid for the library
      # type is sent (the C# enum has no 0, so omitting it binds an invalid
      # default, and each library type allows only specific providers).
      metadata_provider=metadata_provider,
    ),
  )
  if resp.status_code != 200 or resp.parsed is None or not isinstance(resp.parsed.id, int):
    pytest.fail(f'library create failed: HTTP {resp.status_code} {resp.content!r}')
  return resp.parsed.id


def _list_series(admin_client: AuthenticatedClient) -> list[SeriesDto]:
  try:
    resp = post_api_series_v_2.sync_detailed(
      client=admin_client,
      body=SeriesFilterV2Dto(),
      page_number=1,
      page_size=100,
    )
  except Exception as exc:  # noqa: BLE001
    # The nightly client crashes parsing the null `metadataProviderOverride`;
    # that is the known Fix 2 upstream bug, recorded as xfail.
    expect_upstream_fix('Fix 2 (nullable enums)', repr(exc))
    raise
  return resp.parsed if resp.status_code == 200 and resp.parsed else []


def _wait_for_series(
  admin_client: AuthenticatedClient,
  predicate: Callable[[SeriesDto], bool],
  timeout: float,
  what: str,
) -> list[SeriesDto]:
  '''Poll the series list until `predicate` finds its series (or fail).'''
  deadline = time.monotonic() + timeout
  series: list[SeriesDto] = []
  while time.monotonic() < deadline:
    series = _list_series(admin_client)
    found = [s for s in series if predicate(s)]
    if found:
      return found
    time.sleep(3.0)
  seen = ', '.join(f'{s.name or "?"} (lib {s.library_id})' for s in series)
  raise RuntimeError(f'scan did not populate {what} within {timeout}s (series: {seen})')


def _scan_library(admin_client: AuthenticatedClient, library_id: int) -> None:
  scan = post_api_library_scan.sync_detailed(client=admin_client, library_id=library_id, force=True)
  if scan.status_code != 200:
    pytest.fail(f'library scan failed: HTTP {scan.status_code} {scan.content!r}')


@pytest.fixture(scope='session')
def media_library(media_kavita_server: KavitaInstance) -> Iterator[MediaLibrary]:
  '''Populate the media volume, create libraries, scan, and wait for series.'''
  missing = _media_tools_missing()
  if missing:
    pytest.skip(f'media generation tools are not available: {missing}')

  _generate_sample_media(media_kavita_server.media_dir)
  admin_client = media_kavita_server.admin_client

  manga_library_id = _create_library(
    admin_client,
    MANGA_LIBRARY_NAME,
    LibraryType.MANGA,
    f'{MEDIA_MOUNT}/comics',
    [FileTypeGroup.ARCHIVE],
    MetadataProvider.MANGABAKA,
  )
  book_library_id = _create_library(
    admin_client,
    BOOK_LIBRARY_NAME,
    LibraryType.BOOK,
    f'{MEDIA_MOUNT}/books',
    [FileTypeGroup.EPUB, FileTypeGroup.PDF],
    MetadataProvider.HARDCOVER,
  )

  # Kavita queues overlapping scans (potentially hours out), so scan the
  # libraries sequentially and wait for each before starting the next.
  _scan_library(admin_client, manga_library_id)
  comic = _wait_for_series(
    admin_client,
    lambda s: s.library_id == manga_library_id and s.name == COMIC_SERIES_NAME,
    KAVITA_SCAN_TIMEOUT,
    'the manga library',
  )
  second = _wait_for_series(
    admin_client,
    lambda s: s.library_id == manga_library_id and s.name == SECOND_SERIES_NAME,
    KAVITA_SCAN_TIMEOUT,
    f'the second series ({SECOND_SERIES_NAME})',
  )

  _scan_library(admin_client, book_library_id)
  books = _wait_for_series(
    admin_client,
    lambda s: s.library_id == book_library_id,
    KAVITA_SCAN_TIMEOUT,
    'the book library',
  )

  # Collect the ids the endpoint tests need.
  volumes_resp = get_api_series_volumes.sync_detailed(client=admin_client, series_id=comic[0].id)
  if volumes_resp.status_code != 200 or volumes_resp.parsed is None:
    pytest.fail(f'comic volumes failed: HTTP {volumes_resp.status_code} {volumes_resp.content!r}')
  comic_volume_ids = [volume.id for volume in volumes_resp.parsed if isinstance(volume.id, int)]
  comic_chapter_ids = [
    chapter.id
    for volume in volumes_resp.parsed
    for chapter in volume.chapters or []
    if isinstance(chapter.id, int)
  ]

  detail_resp = get_api_series_series_detail.sync_detailed(client=admin_client, series_id=comic[0].id)
  comic_special_ids = [
    chapter.id
    for chapter in (detail_resp.parsed.specials or [])
    if isinstance(chapter.id, int)
  ]

  epub_chapter_id = 0
  for series_id in [series.id for series in books if isinstance(series.id, int)]:
    book_volumes = get_api_series_volumes.sync_detailed(client=admin_client, series_id=series_id)
    for volume in book_volumes.parsed or []:
      for chapter in volume.chapters or []:
        if chapter.format_ == MangaFormat.EPUB and isinstance(chapter.id, int):
          epub_chapter_id = chapter.id

  # Reading activity: page 3 of the first comic chapter, so the progress
  # and history endpoints have real data.
  read_chapter_id = comic_chapter_ids[0] if comic_chapter_ids else 0
  if read_chapter_id:
    progress = post_api_reader_progress.sync_detailed(
      client=admin_client,
      body=ProgressDto(
        chapter_id=read_chapter_id,
        page_num=3,
        series_id=comic[0].id,
        volume_id=comic_volume_ids[0] if comic_volume_ids else 0,
        library_id=manga_library_id,
      ),
    )
    if progress.status_code != 200:
      pytest.fail(f'reader progress failed: HTTP {progress.status_code} {progress.content!r}')

  # An API key: the Reader image/thumbnail and Panels endpoints require one.
  auth_key_resp = post_api_account_create_auth_key.sync_detailed(
    client=admin_client,
    body=RotateAuthKeyRequestDto(name='it-auth-key', key_length=32),
  )
  if auth_key_resp.status_code != 200 or auth_key_resp.parsed is None or not auth_key_resp.parsed.key:
    pytest.fail(f'auth key creation failed: HTTP {auth_key_resp.status_code} {auth_key_resp.content!r}')
  api_key = auth_key_resp.parsed.key

  yield MediaLibrary(
    manga_library_id=manga_library_id,
    book_library_id=book_library_id,
    comic_series_id=comic[0].id,
    comic_series_name=COMIC_SERIES_NAME,
    second_series_id=second[0].id,
    book_series_ids=[series.id for series in books if isinstance(series.id, int)],
    comic_volume_ids=comic_volume_ids,
    comic_chapter_ids=comic_chapter_ids,
    comic_special_ids=comic_special_ids,
    epub_chapter_id=epub_chapter_id,
    read_chapter_id=read_chapter_id,
    api_key=api_key,
  )
