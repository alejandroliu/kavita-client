'''Shared test configuration.

Makes the repo root (for `fix_spec`) and the generated client importable.
The generated client is imported from an *installed* package when present, so
a release can be verified against the exact built artifact; otherwise the
generated source tree in the repository is used.
'''

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))


def _is_docker_backed(item: pytest.Item) -> bool:
  '''True for tests that need the Kavita docker fixtures.'''
  if 'integration' in item.keywords:
    return True
  return 'tests/integration/' in item.nodeid


def pytest_collection_finish(session: pytest.Session) -> None:
  '''Abort before any test runs if docker-backed tests were collected and a
  previous run left docker resources behind (see `docker_guard.py`).

  Offline-only sessions (e.g. `make test-fix_spec`) skip the check.
  '''
  if not any(_is_docker_backed(item) for item in session.items):
    return
  from docker_guard import list_conflicts
  conflicts = list_conflicts()
  if conflicts:
    pytest.exit(
      'docker resource conflict '
      '(stop any in-progress run first, then `make tidy`):\n'
      + '\n'.join(f'  - {item}' for item in conflicts))

try:
  import kavita_client  # noqa: F401  (installed package wins)
except ImportError:
  # Fall back to a generated source tree: the patched build first, then the
  # raw (nightly) build.
  for pattern in ('*-fixed/kavita-client', '*/kavita-client'):
    matches = sorted(ROOT.glob(pattern))
    if matches:
      sys.path.insert(0, str(matches[0]))
      break

