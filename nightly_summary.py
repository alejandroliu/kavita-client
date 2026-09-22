#!/usr/bin/env python3
'''Summarize the nightly junit reports for the tracking issue.

Reads `reports/junit-release.xml` and `reports/junit-nightly.xml`, prints a
markdown breakdown to stdout; the last line is `FAILED` or `GREEN` so the
CI job knows whether to post to the tracking issue.
'''

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
  stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
  owner = os.environ.get('GITHUB_REPOSITORY_OWNER', 'owner')
  repo = os.environ.get('GITHUB_REPOSITORY_NAME', 'repo')
  site = f'https://{owner}.github.io/{repo}/'
  print(f'## Nightly run — {stamp} UTC')
  print()
  print(f'Site: [{site}]({site})')
  any_failed = False
  for label, path in (('Stable line', 'reports/junit-release.xml'),
                      ('Dev line', 'reports/junit-nightly.xml')):
    print(f'### {label}')
    print()
    report = Path(path)
    if not report.is_file():
      print('No report.')
      print()
      continue
    root = ET.parse(report).getroot()
    passed = failed = xfailed = 0
    failures: list[str] = []
    for case in root.iter('testcase'):
      f = case.find('failure')
      e = case.find('error')
      s = case.find('skipped')
      if f is not None or e is not None:
        failed += 1
        node = f if f is not None else e
        failures.append(
          f"- `{case.get('classname')}::{case.get('name')}` — "
          f"{((node.get('message') or '').replace(chr(10), ' '))[:200]}")
      elif s is not None and 'known upstream spec bug' in (s.get('message') or ''):
        xfailed += 1
      else:
        passed += 1
    any_failed = any_failed or failed > 0
    print(f'{passed} passed, {xfailed} xfailed, **{failed} failed**.')
    print()
    if failures:
      print('\n'.join(failures[:30]))
      if len(failures) > 30:
        print(f'… and {len(failures) - 30} more.')
      print()
  print('FAILED' if any_failed else 'GREEN')
  return 0


if __name__ == '__main__':
  sys.exit(main())
