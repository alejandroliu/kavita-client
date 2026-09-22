'''Offline tests for docker_guard (docker is faked out).'''

from __future__ import annotations

import subprocess

import docker_guard


class FakeDocker:
  '''Stands in for docker_guard._docker with canned responses.'''

  def __init__(self, responses: list[tuple[str, int, str]]):
    self.responses = responses  # (args-prefix, returncode, stdout)
    self.calls: list[list[str]] = []

  def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
    self.calls.append(list(args))
    joined = ' '.join(args)
    for prefix, returncode, stdout in self.responses:
      if joined.startswith(prefix):
        return subprocess.CompletedProcess(
          args, returncode, stdout=stdout, stderr='')
    raise AssertionError(f'unexpected docker call: {args!r}')


def _no_docker(monkeypatch) -> None:
  monkeypatch.setattr(
    docker_guard, '_docker',
    lambda args: subprocess.CompletedProcess(args, 1, stdout='', stderr=''))


def test_no_conflicts_when_docker_unavailable(monkeypatch) -> None:
  _no_docker(monkeypatch)
  assert docker_guard.list_conflicts() == []
  assert docker_guard.tidy() == []


def test_conflicts_listed(monkeypatch) -> None:
  fake = FakeDocker([
    ('info', 0, ''),
    ('network ls', 0, 'mynet\nkavita-github-mock\n'),
    ('ps -a', 0,
     'kavita-github-mock-abcd1234\nkavita-client-test-1111\nother-app\n'),
  ])
  monkeypatch.setattr(docker_guard, '_docker', fake)
  conflicts = docker_guard.list_conflicts()
  assert 'network kavita-github-mock' in conflicts
  assert 'container kavita-github-mock-abcd1234' in conflicts
  assert 'container kavita-client-test-1111' in conflicts
  assert not any('other-app' in item for item in conflicts)


def test_overlapping_foreign_network_flagged(monkeypatch) -> None:
  fmt = '{{range .IPAM.Config}}{{.Subnet}} {{end}}'
  fake = FakeDocker([
    ('info', 0, ''),
    ('network ls', 0, 'mynet\nother\n'),
    (f'network inspect --format {fmt} mynet', 0, '172.19.0.0/16'),
    (f'network inspect --format {fmt} other', 0, '172.30.0.0/24'),
    ('ps -a', 0, ''),
  ])
  monkeypatch.setattr(docker_guard, '_docker', fake)
  conflicts = docker_guard.list_conflicts()
  assert any('network other' in item and 'overlaps' in item
             for item in conflicts)
  assert not any('mynet' in item for item in conflicts)


def test_tidy_removes_only_repo_resources(monkeypatch) -> None:
  fake = FakeDocker([
    ('info', 0, ''),
    ('ps -a', 0,
     'kavita-github-mock-aaaa\nkavita-client-test-bbbb\nother-app\n'),
    ('rm -f', 0, ''),
    ('network rm', 0, ''),
  ])
  monkeypatch.setattr(docker_guard, '_docker', fake)
  removed = docker_guard.tidy()
  rm_calls = [c for c in fake.calls if c[:2] == ['rm', '-f']]
  assert len(rm_calls) == 2
  rm_targets = [name for call in rm_calls for name in call[2:]]
  assert sorted(rm_targets) == sorted(
    ['kavita-github-mock-aaaa', 'kavita-client-test-bbbb'])
  assert not any('other-app' in name for name in rm_targets)
  assert 'container kavita-github-mock-aaaa' in removed
  assert 'container kavita-client-test-bbbb' in removed
  assert 'network kavita-github-mock' in removed
