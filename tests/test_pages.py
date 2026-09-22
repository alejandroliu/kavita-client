'''Offline tests for the static site generator (docs/TODO-gh-pages.md).'''

from __future__ import annotations

import json
from pathlib import Path

import yaml

from pages.build_site import build

MINIMAL_REGISTRY = {
  'fixes': [
    {'id': 'F1', 'title': 'invite response', 'endpoints': [{'path': '/api/Account/invite', 'method': 'post'}]},
  ],
  'quirks': [
    {
      'id': 'Q01', 'category': 'fix-dependent', 'status': 'open', 'title': 'invite object',
      'fix_id': 'F1', 'endpoints': [], 'covered_by': ['tests/integration/test_invite.py'],
      'schemathesis': 'exclude-nightly',
      'note': 'upstream issue #4934',
    },
  ],
}

PREVIOUS_JUNIT = '''<testsuites><testsuite name="prev">
<testcase classname="tests.integration.test_sweep" name="test_x">
<skipped message="known upstream spec bug still present (Fix 3 (bare string bodies) not applied)" />
</testcase></testsuite></testsuites>'''

NIGHTLY_JUNIT = '''<testsuites><testsuite name="cur">
<testcase classname="tests.integration.test_sweep" name="test_a" />
<testcase classname="tests.integration.test_sweep" name="test_y"><failure message="boom" /></testcase>
<testcase classname="tests.integration.test_sweep" name="test_z">
<skipped message="known upstream spec bug still present (Fix 2 (nullable enums) not applied)" />
</testcase>
</testsuite></testsuites>'''

RELEASE_JUNIT = '''<testsuites><testsuite name="rel">
<testcase classname="tests.integration.test_server" name="test_rel" />
<testcase classname="tests.integration.test_sweep" name="test_rel_fail"><failure message="rel boom" /></testcase>
</testsuite></testsuites>'''


def _fixture_root(tmp_path: Path, previous: bool = False) -> Path:
  (tmp_path / 'reports').mkdir()
  (tmp_path / 'kavita_quirks.yaml').write_text(
    yaml.safe_dump(MINIMAL_REGISTRY), encoding='utf-8')
  (tmp_path / 'reports' / 'junit-nightly.xml').write_text(NIGHTLY_JUNIT, encoding='utf-8')
  (tmp_path / 'reports' / 'junit-release.xml').write_text(RELEASE_JUNIT, encoding='utf-8')
  if previous:
    (tmp_path / 'reports' / 'junit-nightly.previous.xml').write_text(PREVIOUS_JUNIT, encoding='utf-8')
  return tmp_path


def test_build_produces_index_and_status_pages(tmp_path: Path) -> None:
  root = _fixture_root(tmp_path)
  build('9.9.9', root=root, out=tmp_path / 'gh-pages')
  assert (tmp_path / 'gh-pages' / 'index.html').is_file()
  release = (tmp_path / 'gh-pages' / 'status' / 'release.html').read_text(encoding='utf-8')
  assert 'F1' in release and 'Q01' in release and 'upstream issue #4934' in release
  assert 'Release test run' in release
  assert 'test_rel' in release and 'rel boom' in release
  nightly = (tmp_path / 'gh-pages' / 'status' / 'nightly.html').read_text(encoding='utf-8')
  assert '1 passed' in nightly and '1 failed' in nightly and 'Fix 2' in nightly
  assert 'test_rel' not in nightly


def test_upstream_fixed_flag_from_previous_junit(tmp_path: Path) -> None:
  root = _fixture_root(tmp_path, previous=True)
  build('9.9.9', root=root, out=tmp_path / 'gh-pages')
  nightly = (tmp_path / 'gh-pages' / 'status' / 'nightly.html').read_text(encoding='utf-8')
  assert 'Upstream fixed 1 bug(s)' in nightly
  assert 'test_x' in nightly


def test_no_flag_without_previous_junit(tmp_path: Path) -> None:
  root = _fixture_root(tmp_path, previous=False)
  build('9.9.9', root=root, out=tmp_path / 'gh-pages')
  nightly = (tmp_path / 'gh-pages' / 'status' / 'nightly.html').read_text(encoding='utf-8')
  assert 'No previous report' in nightly
  assert 'junit-nightly.previous.xml' in nightly


def test_docs_from_imports_prebuilt_docs(tmp_path: Path) -> None:
  root = _fixture_root(tmp_path)
  (tmp_path / 'docs-src').mkdir()
  (tmp_path / 'docs-src' / 'index.html').write_text('<p>release docs</p>', encoding='utf-8')
  build('9.9.9', root=root, out=tmp_path / 'gh-pages', docs_from=tmp_path / 'docs-src')
  docs = (tmp_path / 'gh-pages' / 'docs' / 'index.html').read_text(encoding='utf-8')
  assert 'release docs' in docs
  index = (tmp_path / 'gh-pages' / 'index.html').read_text(encoding='utf-8')
  assert 'API documentation' in index
