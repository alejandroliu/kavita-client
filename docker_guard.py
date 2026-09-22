#!/usr/bin/env python3
'''Docker resource guard for the Kavita test suite.

The pytest integration suite (`tests/integration/conftest.py`) and the
schemathesis stack (`schemathesis_stack.py`) share a fixed docker network
name and subnet.  An interrupted run (Ctrl-C / kill) leaves those
resources behind, and they block the next run's setup (the suite skips
github-mock tests with "could not create the github mock network").

Commands:
  check   list conflicting resources; exit 1 if any exist
  tidy    remove the leftover containers/networks that belong to this repo

`make tidy` runs the tidy command; `conftest.py` runs `check` before any
docker-backed pytest session starts and aborts the run on conflicts.
'''

from __future__ import annotations

import ipaddress
import subprocess
import sys

MOCK_NETWORK = 'kavita-github-mock'
MOCK_SUBNET = ipaddress.ip_network('172.30.0.0/29')
# Container name prefixes created by this repo's docker fixtures.
CONTAINER_PREFIXES = (
  'kavita-github-mock-',
  'kavita-client-',
  'schemathesis-mock-',
  'schemathesis-kavita-',
)


def _docker(args: list[str]) -> subprocess.CompletedProcess:
  return subprocess.run(['docker', *args], capture_output=True, text=True)


def _docker_available() -> bool:
  return _docker(['info']).returncode == 0


def _network_names() -> list[str]:
  return _docker(['network', 'ls', '--format', '{{.Name}}']).stdout.split()


def _network_subnets(name: str) -> list[ipaddress.IPv4Network]:
  '''IPv4 subnets of a docker network (empty for non-IPv4 / missing).'''
  run = _docker(['network', 'inspect', '--format',
                 '{{range .IPAM.Config}}{{.Subnet}} {{end}}', name])
  subnets: list[ipaddress.IPv4Network] = []
  for raw in run.stdout.split():
    try:
      net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
      continue  # e.g. 'null' for networks without an IPAM subnet
    if net.version == 4:
      subnets.append(net)
  return subnets


def _container_names() -> list[str]:
  return _docker(['ps', '-a', '--format', '{{.Names}}']).stdout.split()


def list_conflicts() -> list[str]:
  '''Human-readable list of leftover resources that would block a new run.'''
  if not _docker_available():
    return []
  conflicts: list[str] = []
  names = _network_names()
  if MOCK_NETWORK in names:
    conflicts.append(f'network {MOCK_NETWORK}')
  else:
    # A foreign network overlapping our subnet also blocks `network create`
    # ("Pool overlaps with other one on this address space").
    for name in names:
      for subnet in _network_subnets(name):
        if subnet.overlaps(MOCK_SUBNET):
          conflicts.append(
            f'network {name} (subnet {subnet} overlaps {MOCK_SUBNET})')
  for name in _container_names():
    if name.startswith(CONTAINER_PREFIXES):
      conflicts.append(f'container {name}')
  return conflicts


def tidy() -> list[str]:
  '''Remove leftover repo-owned containers and network; report what changed.'''
  if not _docker_available():
    return []
  removed: list[str] = []
  for name in _container_names():
    if name.startswith(CONTAINER_PREFIXES):
      if _docker(['rm', '-f', name]).returncode == 0:
        removed.append(f'container {name}')
  if _docker(['network', 'rm', MOCK_NETWORK]).returncode == 0:
    removed.append(f'network {MOCK_NETWORK}')
  return removed


def main(argv: list[str] | None = None) -> int:
  argv = sys.argv[1:] if argv is None else argv
  if argv and argv[0] == 'check':
    conflicts = list_conflicts()
    if conflicts:
      print('conflicting docker resources '
            '(stop any in-progress run first, then `make tidy`):')
      for item in conflicts:
        print(f'  - {item}')
      return 1
    print('no conflicting docker resources')
    return 0
  if argv and argv[0] == 'tidy':
    removed = tidy()
    if removed:
      print('removed:')
      for item in removed:
        print(f'  - {item}')
    else:
      print('nothing to tidy')
    return 0
  print(__doc__)
  return 2


if __name__ == '__main__':
  sys.exit(main())
