#!/usr/bin/env python3
'''Boot/teardown the Schemathesis canary stack.

`up STATE` — generate the TLS certs, start the GitHub mock and a fresh
Kavita container (api.github.com pinned to the mock, CA trusted), wait
for readiness, register the admin, and write the state JSON
(host port, JWT, container/network names, certs dir) to STATE.
`down STATE` — remove the containers, network, certs, and STATE.

Same layout as the pytest `github_mock` fixture (DESIGN quirk 23);
standalone so the Makefile targets do not need pytest.
'''

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
MOCK_IMAGE = 'python:3-alpine'
NETWORK = 'kavita-github-mock'
SUBNET = '172.30.0.0/29'
MOCK_IP = '172.30.0.2'
KAVITA_IP = '172.30.0.3'
CONTAINER_PORT = 5000


def _docker(args: list[str]) -> subprocess.CompletedProcess:
  return subprocess.run(['docker', *args], capture_output=True, text=True)


def _pull(image: str) -> None:
  pull = _docker(['pull', image])
  if pull.returncode != 0:
    sys.exit(f'docker pull {image} failed: {pull.stderr.strip()}')


def up(state_path: str, image: str) -> None:
  if _docker(['info']).returncode != 0:
    sys.exit('docker is not available')
  from docker_guard import list_conflicts
  conflicts = list_conflicts()
  if conflicts:
    sys.exit(
      'docker resource conflict '
      '(stop any in-progress run first, then `make tidy`):\n'
      + '\n'.join(f'  - {item}' for item in conflicts))
  for required in (image, MOCK_IMAGE):
    if _docker(['image', 'inspect', required]).returncode != 0:
      _pull(required)

  certs_dir = Path('/tmp') / f'schemathesis-certs-{uuid.uuid4().hex[:8]}'
  certs_dir.mkdir()
  for command in (
    ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
     '-keyout', str(certs_dir / 'ca.key'), '-out', str(certs_dir / 'ca.crt'),
     '-days', '7', '-subj', '/CN=kavita-schemathesis-ca',
     '-addext', 'basicConstraints=critical,CA:TRUE',
     '-addext', 'keyUsage=critical,keyCertSign'],
    ['openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
     '-keyout', str(certs_dir / 'server.key'), '-out', str(certs_dir / 'server.csr'),
     '-subj', '/CN=api.github.com',
     '-addext', 'subjectAltName=DNS:api.github.com,DNS:raw.githubusercontent.com,IP:127.0.0.1'],
    ['openssl', 'x509', '-req', '-in', str(certs_dir / 'server.csr'),
     '-CA', str(certs_dir / 'ca.crt'), '-CAkey', str(certs_dir / 'ca.key'),
     '-CAcreateserial', '-out', str(certs_dir / 'server.crt'),
     '-days', '7', '-copy_extensions', 'copyall'],
  ):
    subprocess.run(command, check=True, capture_output=True)

  _docker(['network', 'rm', NETWORK])
  if _docker(['network', 'create', '--subnet', SUBNET, NETWORK]).returncode != 0:
    sys.exit('could not create the github mock network')

  config_dir = Path('/tmp') / f'schemathesis-config-{uuid.uuid4().hex[:8]}'
  config_dir.mkdir()
  config_dir.chmod(0o777)
  media_dir = Path('/tmp') / f'schemathesis-media-{uuid.uuid4().hex[:8]}'
  (media_dir / 'Comics' / 'lorem').mkdir(parents=True)
  _write_cbz(media_dir / 'Comics' / 'lorem' / 'Lorem v01.cbz')
  media_dir.chmod(0o777)

  mock_name = f'schemathesis-mock-{uuid.uuid4().hex[:8]}'
  run = _docker(['run', '-d', '--rm', '--name', mock_name,
                 '--network', NETWORK, '--ip', MOCK_IP,
                 '-v', f'{certs_dir}:/certs:ro',
                 '-v', f'{ROOT}/tests/integration/github_mock.py:/github_mock.py:ro',
                 MOCK_IMAGE, 'python3', '/github_mock.py',
                 '--cert', '/certs/server.crt', '--key', '/certs/server.key'])
  if run.returncode != 0:
    sys.exit(f'github mock start failed: {run.stderr.strip()}')

  kavita_name = f'schemathesis-kavita-{uuid.uuid4().hex[:8]}'
  run = _docker(['run', '-d', '--rm', '--name', kavita_name,
                 '--network', NETWORK, '--ip', KAVITA_IP,
                 '--user', f'{os.getuid()}:{os.getgid()}',
                 '-p', f'127.0.0.1::{CONTAINER_PORT}',
                 '-v', f'{config_dir}:/kavita/config',
                 '-v', f'{media_dir}:/media:ro',
                 '-v', f'{certs_dir}/ca.crt:/etc/kavita-ca.crt:ro',
                 '-e', 'SSL_CERT_FILE=/etc/kavita-ca.crt',
                 '-v', f'{ROOT}/tests/data/favicon.ico:/favicon.ico:ro',
                 '--add-host', f'api.github.com:{MOCK_IP}',
                 '--add-host', f'raw.githubusercontent.com:{MOCK_IP}',
                 image])
  if run.returncode != 0:
    down(state_path, silent=True)
    sys.exit(f'kavita start failed: {run.stderr.strip()}')

  try:
    host_port = ''
    for line in _docker(['port', kavita_name, f'{CONTAINER_PORT}/tcp']).stdout.splitlines():
      _, _, host_port = line.rpartition(':')
      if host_port.isdigit():
        break
    if not host_port:
      sys.exit('kavita did not publish a host port')
    base_url = f'http://127.0.0.1:{host_port}'
    deadline = time.monotonic() + float(os.environ.get('KAVITA_READY_TIMEOUT', '240'))
    while time.monotonic() < deadline:
      try:
        if httpx.get(f'{base_url}/api/Health', timeout=5).status_code == 200:
          break
      except httpx.HTTPError:
        pass
      time.sleep(2)
    else:
      sys.exit('kavita did not become ready')

    register = httpx.post(f'{base_url}/api/Account/register', json={
      'username': 'schemathesis-admin', 'password': 'SchemathesisPass123',
      'email': 'schemathesis@example.com',
    }, timeout=30)
    if register.status_code != 200:
      sys.exit(f'admin registration failed: HTTP {register.status_code}')
    login = httpx.post(f'{base_url}/api/Account/login', json={
      'username': 'schemathesis-admin', 'password': 'SchemathesisPass123',
    }, timeout=30)
    login_body = login.json()
    token = login_body.get('token')
    if login.status_code != 200 or not token:
      sys.exit('admin login failed')

    headers = {'Authorization': f'Bearer {token}'}
    providers: dict[str, object] = {}
    try:
      _populate(base_url, headers, login_body, providers)
    except Exception as exc:  # noqa: BLE001
      print(f'warning: populate failed ({exc}); value providers disabled')

    state = {
      'port': host_port,
      'token': token,
      'providers': providers,
      'mock_name': mock_name,
      'kavita_name': kavita_name,
      'network': NETWORK,
      'certs_dir': str(certs_dir),
      'config_dir': str(config_dir),
      'media_dir': str(media_dir),
    }
    Path(state_path).write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(f'canary stack up on http://127.0.0.1:{host_port} '
          f'(providers: {len(providers)} values, state: {state_path})')
  except BaseException:
    down(state_path, silent=True)
    raise


def down(state_path: str, silent: bool = False) -> None:
  state = {}
  try:
    state = json.loads(Path(state_path).read_text(encoding='utf-8'))
  except (OSError, json.JSONDecodeError):
    pass
  for name in (state.get('kavita_name'), state.get('mock_name')):
    if name:
      _docker(['rm', '-f', name])
  if state.get('network'):
    _docker(['network', 'rm', state['network']])
  for directory in (state.get('certs_dir'), state.get('config_dir'), state.get('media_dir')):
    if directory:
      shutil.rmtree(directory, ignore_errors=True)
  Path(state_path).unlink(missing_ok=True)
  if not silent:
    print(f'canary stack down ({state_path})')


def _write_cbz(path: Path) -> None:
  '''A minimal 8-page cbz from tests/data/cover.jpg (one entity of each
  kind is all the value providers need).'''
  import zipfile
  cover = (ROOT / 'tests' / 'data' / 'cover.jpg').read_bytes()
  with zipfile.ZipFile(path, 'w') as archive:
    for page in range(1, 9):
      archive.writestr(f'page_{page:03d}.jpg', cover)


def _populate(base_url: str, headers: dict[str, str], login_body: dict, providers: dict[str, object]) -> None:
  '''Create a library, scan the sample cbz, and collect real entity ids.

  Best-effort: the caller catches failures and runs without providers
  (the Q26/Q27/Q28 exclusions then apply as before).
  '''
  created = httpx.post(f'{base_url}/api/Library/create', headers=headers, json={
    'id': 0, 'name': 'canary', 'type': 0, 'folders': ['/media/Comics'],
    'folderWatching': False, 'includeInDashboard': True, 'includeInSearch': True,
    'manageCollections': False, 'manageReadingLists': False, 'allowScrobbling': False,
    'allowMetadataMatching': False, 'enableMetadata': True,
    'removePrefixForSortName': False, 'inheritWebLinksFromFirstChapter': False,
    'fileGroupTypes': [1], 'excludePatterns': [], 'metadataProvider': 3,
  }, timeout=60)
  if created.status_code != 200 or not isinstance(created.json().get('id'), int):
    raise RuntimeError(f'library create failed: HTTP {created.status_code} {created.text[:200]}')
  library_id = created.json()['id']

  scanned = httpx.post(
    f'{base_url}/api/Library/scan', headers=headers,
    params={'libraryId': library_id, 'force': 'true'}, timeout=60,
  )
  if scanned.status_code != 200:
    raise RuntimeError(f'library scan failed: HTTP {scanned.status_code}')

  series = []
  deadline = time.monotonic() + float(os.environ.get('KAVITA_SCAN_TIMEOUT', '180'))
  while time.monotonic() < deadline:
    listed = httpx.post(
      f'{base_url}/api/Series/v2', headers=headers, json={},
      params={'pageNumber': 1, 'pageSize': 100}, timeout=30,
    )
    if listed.status_code == 200 and listed.json():
      series = listed.json()
      break
    time.sleep(3.0)
  if not series:
    raise RuntimeError('scan did not populate a series')
  series_id = series[0]['id']

  volumes = httpx.get(
    f'{base_url}/api/Series/volumes', headers=headers, params={'seriesId': series_id}, timeout=30,
  )
  if volumes.status_code != 200 or not volumes.json():
    raise RuntimeError(f'volumes failed: HTTP {volumes.status_code}')
  volume = volumes.json()[0]
  chapter_id = volume['chapters'][0]['id'] if volume.get('chapters') else None
  if not isinstance(chapter_id, int):
    raise RuntimeError('volume has no chapters')

  auth_key = httpx.post(
    f'{base_url}/api/Account/create-auth-key', headers=headers,
    json={'keyLength': 32, 'name': 'canary'}, timeout=30,
  )
  if auth_key.status_code != 200 or not auth_key.json().get('key'):
    raise RuntimeError(f'auth key failed: HTTP {auth_key.status_code}')

  providers.update({
    'series_id': series_id,
    'volume_id': volume['id'],
    'chapter_id': chapter_id,
    'library_id': library_id,
    'user_id': login_body['id'],
    'api_key': auth_key.json()['key'],
    'query': 'Lorem',
    'tz_id': 'UTC',
  })


def main() -> None:
  if len(sys.argv) < 3:
    sys.exit('usage: schemathesis_stack.py up|down STATE [--image IMAGE]')
  command, state_path = sys.argv[1], sys.argv[2]
  image = os.environ.get('KAVITA_IMAGE', 'jvmilazz0/kavita:latest')
  if '--image' in sys.argv:
    image = sys.argv[sys.argv.index('--image') + 1]
  if command == 'up':
    up(state_path, image)
  elif command == 'down':
    down(state_path)
  else:
    sys.exit(f'unknown command {command}')


if __name__ == '__main__':
  main()
