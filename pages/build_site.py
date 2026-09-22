#!/usr/bin/env python3
'''Static site build (docs/TODO-gh-pages.md, work item A1).

Renders `gh-pages/` from the repo's own artifacts, no framework:

- index.html — links + version stamp + build timestamp
- status/release.html — the quirks registry (how the release client
  differs from the published spec) + the release test run
  (`reports/junit-release.xml` + `reports/schemathesis-release-junit.xml`)
- status/nightly.html — the nightly test run
  (`reports/junit-nightly.xml` + `reports/schemathesis-nightly-junit.xml`),
  with the "upstream fixed" flags computed against the previous nightly
  junit (`reports/junit-nightly.previous.xml`) if present
- docs/ — a copy of the sphinx output (sphinx/_build/html)

Run through ./pys.sh:  pages/build_site.py [--version VERSION]
'''

from __future__ import annotations

import argparse
import html
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'gh-pages'
REPORTS = ROOT / 'reports'
SPHINX_HTML = ROOT / 'sphinx' / '_build' / 'html'
UPSTREAM_ISSUE = 'https://github.com/Kareadita/Kavita/issues/4934'

STYLE = '''
  body { font-family: sans-serif; margin: 2em auto; max-width: 60em; line-height: 1.4; }
  table { border-collapse: collapse; margin: 1em 0; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 0.35em 0.6em; text-align: left; vertical-align: top; }
  th { background: #f4f4f4; }
  .flag { background: #fff3cd; border: 1px solid #e0b400; padding: 0.6em; margin: 0.6em 0; }
  .fail { color: #a00; font-weight: bold; }
  .pass { color: #0a0; }
  code { background: #f4f4f4; padding: 0 0.25em; }
'''


def esc(value: Any) -> str:
  return html.escape(str(value))


def _page(title: str, body: str, stamp: str) -> str:
  return f'''<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>{STYLE}</style></head>
<body>
<h1>{esc(title)}</h1>
<p class="meta">{esc(stamp)}</p>
{body}
<p><a href="../index.html">back to index</a></p>
</body></html>
'''


def registry_body(registry: dict) -> str:
  fixes = registry.get('fixes', [])
  quirks = registry.get('quirks', [])

  fix_rows = []
  for fix in fixes:
    endpoints = ', '.join(f"{esc(e['path'])} ({esc(e['method'])})" for e in fix.get('endpoints', []))
    fields = ', '.join(f"{esc(f['schema'])}.{esc(f['field'])}" for f in fix.get('fields', []))
    fix_rows.append(
      f"<tr><td>{esc(fix['id'])}</td><td>{esc(fix.get('title', ''))}</td>"
      f"<td>{endpoints or '—'}</td><td>{fields or '—'}</td></tr>")

  quirk_rows = []
  for quirk in quirks:
    covered = ', '.join(esc(ref) for ref in quirk.get('covered_by', [])) or '—'
    note = esc(quirk.get('note', '')).replace('\n', ' ')
    endpoints = len(quirk.get('endpoints', []))
    quirk_rows.append(
      f"<tr><td>{esc(quirk['id'])}</td><td>{esc(quirk['category'])}</td>"
      f"<td>{esc(quirk['status'])}</td><td>{esc(quirk.get('title', ''))}</td>"
      f"<td>{esc(quirk.get('fix_id') or '—')}</td><td>{endpoints}</td>"
      f"<td>{covered}</td><td>{note}</td></tr>")

  open_quirks = sum(1 for q in quirks if q.get('status') == 'open')
  body = f'''
<p>This page is generated from <code>kavita_quirks.yaml</code> — the single
source of truth for how the release client differs from the published
OpenAPI document. {len(fixes)} fixes patch the spec before code
generation; {len(quirks)} quirks document the server behaviors behind
them ({open_quirks} still open upstream).</p>
<h2>Fixes (applied to the release client)</h2>
<table>
<tr><th>Fix</th><th>Title</th><th>Endpoints</th><th>Fields</th></tr>
{''.join(fix_rows)}
</table>
<h2>Quirk ledger</h2>
<table>
<tr><th>Id</th><th>Category</th><th>Status</th><th>Title</th><th>Fix</th>
<th>Endpoints</th><th>covered_by</th><th>Note</th></tr>
{''.join(quirk_rows)}
</table>
'''
  return body


def release_page(registry: dict, root: Path, stamp: str) -> str:
  '''Release status page: the quirks registry plus the release test run.'''
  reports = root / 'reports'
  curated = _junit_stats(reports / 'junit-release.xml')
  canary = _junit_stats(reports / 'schemathesis-release-junit.xml')
  body = f'''{registry_body(registry)}
<h2>Release test run</h2>
{_junit_block('Curated suite (reports/junit-release.xml)', curated)}
{_junit_block('Schemathesis canary (reports/schemathesis-release-junit.xml)', canary)}
'''
  return _page('Release status — spec differences + test run', body, stamp)


def _junit_stats(path: Path) -> dict[str, Any]:
  # (reports dir is resolved at call time so tests can pass a tmp root)
  if not path.is_file():
    return None
  root = ET.parse(path).getroot()
  counts = {'passed': 0, 'failed': 0, 'xfailed': 0}
  failures: list[tuple[str, str]] = []
  xfails: dict[str, str] = {}
  for case in root.iter('testcase'):
    name = case.get('name', '?')
    classname = case.get('classname', '')
    test_id = f'{classname}::{name}'
    failed = case.find('failure')
    skipped = case.find('skipped')
    if failed is not None:
      counts['failed'] += 1
      failures.append((test_id, (failed.get('message') or '')[:160]))
    elif skipped is not None and 'known upstream spec bug' in (skipped.get('message') or ''):
      counts['xfailed'] += 1
      xfails[test_id] = (skipped.get('message') or '')[:160]
    else:
      counts['passed'] += 1
  return {'counts': counts, 'failures': failures, 'xfails': xfails, 'mtime': path.stat().st_mtime}


def _junit_block(title: str, stats: dict[str, Any] | None) -> str:
  if stats is None:
    return f'<h2>{esc(title)}</h2><p>no report found</p>'
  counts = stats['counts']
  stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats['mtime']))
  failures_html = ''.join(
    f'<tr class="fail"><td>{esc(tid)}</td><td>{esc(msg)}</td></tr>'
    for tid, msg in stats['failures'])
  xfails_html = ''.join(
    f'<tr><td>{esc(tid)}</td><td>{esc(msg)}</td></tr>'
    for tid, msg in sorted(stats['xfails'].items()))
  return f'''
<h2>{esc(title)}</h2>
<p>Report from {esc(stamp)}: <span class="pass">{counts['passed']} passed</span>,
{counts['xfailed']} xfailed, <span class="fail">{counts['failed']} failed</span>.</p>
<p>On nightly, <em>green means the known upstream bugs are still present</em> —
the xfails below are the fix_spec bugs the release client patches. The day one
of them starts passing, upstream fixed it and the corresponding fix can be
retired.</p>
<h3>Known upstream bugs (xfail)</h3>
<table><tr><th>Test</th><th>Reason</th></tr>{xfails_html}</table>
<h3>Failures</h3>
<table><tr><th>Test</th><th>Message</th></tr>{failures_html}</table>
'''


def nightly_page(root: Path, out: Path, stamp: str) -> str:
  reports = root / 'reports'
  curated = _junit_stats(reports / 'junit-nightly.xml')
  previous = _junit_stats(reports / 'junit-nightly.previous.xml')
  canary = _junit_stats(reports / 'schemathesis-nightly-junit.xml')

  flags_html = ''
  if curated is not None and previous is not None:
    fixed = sorted(set(previous['xfails']) - set(curated['xfails']))
    if fixed:
      items = ''.join(f'<li><strong>{esc(tid)}</strong> — was xfail in the previous report</li>' for tid in fixed)
      flags_html = (
        f'<div class="flag"><strong>Upstream fixed {len(fixed)} bug(s) since the previous run</strong> — '
        f'time to retire the corresponding fix_spec fix:'
        f'<ul>{items}</ul></div>')
    else:
      flags_html = '<p>No upstream-fixed flags vs the previous report.</p>'
  else:
    flags_html = (
      '<p><em>No previous report at reports/junit-nightly.previous.xml — copy the '
      'last run there (the CI job will) to enable upstream-fixed flags.</em></p>')

  body = f'''
{flags_html}
{_junit_block('Curated suite (reports/junit-nightly.xml)', curated)}
{_junit_block('Schemathesis canary (reports/schemathesis-nightly-junit.xml)', canary)}
'''
  return _page('Nightly test status', body, stamp)


def build(version: str, root: Path = ROOT, out: Path = OUT,
          docs_from: Path | None = None) -> None:
  stamp = f'kavita-client {esc(version)} — built {time.strftime("%Y-%m-%d %H:%M:%S")}'
  out.mkdir(parents=True, exist_ok=True)
  (out / 'status').mkdir(exist_ok=True)

  registry = yaml.safe_load((root / 'kavita_quirks.yaml').read_text(encoding='utf-8'))
  (out / 'status' / 'release.html').write_text(release_page(registry, root, stamp), encoding='utf-8')
  (out / 'status' / 'nightly.html').write_text(nightly_page(root, out, stamp), encoding='utf-8')

  sphinx_docs = out / 'docs'
  # `docs_from` (CI: the unpacked docs.zip attached to the GitHub Release)
  # wins over the local sphinx output.
  docs_source = (docs_from if docs_from is not None and docs_from.is_dir()
                 else root / 'sphinx' / '_build' / 'html')
  if docs_source.is_dir():
    if sphinx_docs.exists():
      shutil.rmtree(sphinx_docs)
    shutil.copytree(docs_source, sphinx_docs)
  docs_link = '<li><a href="docs/">API documentation (sphinx)</a></li>' if sphinx_docs.is_dir() else ''

  index = f'''<!doctype html>
<html><head><meta charset="utf-8"><title>kavita-client</title>
<style>{STYLE}</style></head>
<body>
<h1>kavita-client</h1>
<p class="meta">{stamp}</p>
<ul>
  <li><a href="status/release.html">Release status — spec differences + test run</a></li>
  <li><a href="status/nightly.html">Nightly test status</a></li>
  <li><a href="https://github.com/Kareadita/Kavita">Kavita (upstream)</a></li>
  {docs_link}
</ul>
</body></html>
'''
  (out / 'index.html').write_text(index, encoding='utf-8')
  print(f'site written to {out}')


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument('--version', default='dev')
  parser.add_argument('--root', type=Path, default=ROOT)
  parser.add_argument('--out', type=Path, default=None)
  parser.add_argument('--docs-from', type=Path, default=None,
                      help='import pre-built API docs from this directory '
                           '(CI: the unpacked docs.zip from the release)')
  args = parser.parse_args()
  build(args.version, root=args.root, out=args.out or args.root / 'gh-pages',
        docs_from=args.docs_from)


if __name__ == '__main__':
  main()
