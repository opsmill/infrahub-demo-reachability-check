"""Invoke tasks for the reachability-check demo branch.

Run with `uv run invoke <task>`. Verbs mirror Infrahub's own
`dev.start` / `dev.init` convention so the muscle memory transfers:

  uv run invoke demo.start                       # prepare bare clone + docker compose up + wait healthy
  uv run invoke demo.register-repo               # register the CoreRepository so the Python transform installs
  uv run invoke demo.init --phase data           # load schemas + menu + seed + rules group
  uv run invoke demo.init --phase rules          # create the reachability rules + constraints
  uv run invoke demo.init --phase scenarios      # create the demo branches + proposed changes
  uv run invoke demo.up                          # all of the above, in order
  uv run invoke demo.status                      # ping the running stack
  uv run invoke demo.logs                        # tail infrahub-server logs
  uv run invoke demo.stop                        # docker compose down (preserves volumes)
  uv run invoke demo.reset                       # docker compose down -v (wipes everything)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import urllib.error
import urllib.request

from invoke.collection import Collection
from invoke.tasks import task

ROOT = Path(__file__).resolve().parent
BARE_REPO_PATH = ROOT / ".demo-bare"
SERVED_BRANCH = "live-demo"

# Defaults match docker-compose.yml + .env. The local-only
# docker-compose.override.yml can remap the host port; the tasks honor
# INFRAHUB_HOST_PORT if it is set.
DEFAULT_PORT = os.environ.get("INFRAHUB_HOST_PORT", "8000")
INFRAHUB_URL = f"http://localhost:{DEFAULT_PORT}"
ADMIN_TOKEN = "06438eb2-8019-4776-878c-0941b1f1d1ec"


def _docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("INFRAHUB_ADDRESS", INFRAHUB_URL)
    env.setdefault("INFRAHUB_API_TOKEN", ADMIN_TOKEN)
    return env


def _prepare_bare_clone() -> None:
    """(Re)create the single-branch bare clone that backs file:///srv/reachability.

    Infrahub task workers see this directory through the read-only bind
    mount declared on the ``task-worker`` service. Two design decisions
    keep the worker happy:

    1. **Single-branch clone.** The bare repo only contains
       ``live-demo``'s commits. The worker does not see any other branch
       and therefore cannot try (and fail) to import one.
    2. **Branch renamed to ``main``.** Inside the bare clone the branch
       is renamed from ``live-demo`` to ``main`` so it matches the
       Infrahub default branch. The worker maps Infrahub's ``main``
       branch onto the only git branch present and the sync completes
       cleanly with no extra configuration.
    """
    if BARE_REPO_PATH.exists():
        shutil.rmtree(BARE_REPO_PATH)
    subprocess.run(
        [
            "git",
            "clone",
            "--bare",
            "--single-branch",
            "--branch",
            SERVED_BRANCH,
            str(ROOT),
            str(BARE_REPO_PATH),
        ],
        check=True,
    )
    # Rename the only branch to ``main`` so it lines up with the
    # Infrahub default branch and the worker has no other branches it
    # could try to track.
    subprocess.run(
        [
            "git",
            "-C",
            str(BARE_REPO_PATH),
            "branch",
            "-m",
            SERVED_BRANCH,
            "main",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(BARE_REPO_PATH),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        ],
        check=True,
    )


@task
def prepare_repo_source(c):
    """Build the single-branch bare clone consumed by the task-worker bind mount."""
    _prepare_bare_clone()
    print(f"Bare clone at {BARE_REPO_PATH} now exposes only the {SERVED_BRANCH} branch.")


@task(help={"wait": "Wait until infrahub-server returns 200 on /api/config."})
def start(c, wait=True):
    """Bring the Infrahub 1.10 stack up."""
    _prepare_bare_clone()
    c.run("docker compose up -d", pty=False, env=_docker_env())
    if not wait:
        return
    print(f"waiting for {INFRAHUB_URL}/api/config ...", flush=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{INFRAHUB_URL}/api/config", timeout=5) as r:
                if r.status == 200:
                    print("infrahub is up.")
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, TimeoutError):
            pass
        time.sleep(5)
    raise SystemExit(f"infrahub did not become healthy within 5 minutes at {INFRAHUB_URL}")


@task
def stop(c):
    """docker compose down (preserves volumes)."""
    c.run("docker compose down", pty=False, env=_docker_env())


@task
def reset(c):
    """docker compose down -v (wipes the database, storage, workflow state)."""
    c.run("docker compose down -v", pty=False, env=_docker_env())


@task
def status(c):
    """Print docker compose ps + a /api/config probe."""
    c.run("docker compose ps", pty=False, env=_docker_env())
    print()
    try:
        with urllib.request.urlopen(f"{INFRAHUB_URL}/api/config", timeout=5) as r:
            print(f"GET {INFRAHUB_URL}/api/config -> {r.status}")
    except Exception as exc:
        print(f"GET {INFRAHUB_URL}/api/config -> {exc}")


@task
def logs(c, service="infrahub-server", tail=200, follow=False):
    """Tail logs for one of the stack services."""
    flag = "-f " if follow else ""
    c.run(f"docker compose logs {flag}--tail={tail} {service}", pty=True, env=_docker_env())


@task(help={
    "address": "Override INFRAHUB_ADDRESS (default: http://localhost:<port>).",
    "token": "Override INFRAHUB_API_TOKEN (default: admin token from .env).",
})
def register_repo(c, address=None, token=None):
    """Register this branch as a CoreRepository.

    Required for the path_traversal_url Python transform to install
    on the running stack. The gitserver container in docker-compose
    serves this repository read-only on git://gitserver/reachability;
    this task points a CoreRepository at it and waits for the worker
    to finish parsing .infrahub.yml.
    """
    env = _docker_env()
    if address:
        env["INFRAHUB_ADDRESS"] = address
    if token:
        env["INFRAHUB_API_TOKEN"] = token
    c.run("uv run python demo-seed/register_repo.py", pty=False, env=env)


@task(help={
    "phase": "One of: data (default), rules, all. Phase 1 (data) loads schemas, the menu, and seed data; phase 2 (rules) creates the rules + constraints. Run register-repo between them.",
    "address": "Override INFRAHUB_ADDRESS (default: http://localhost:<port>).",
    "token": "Override INFRAHUB_API_TOKEN (default: admin token from .env).",
})
def init(c, phase="data", address=None, token=None):
    """Run a seed phase. Phase 1 (data) loads schemas, the menu, and seed data; phase 2 (rules) creates the rules."""
    env = _docker_env()
    if address:
        env["INFRAHUB_ADDRESS"] = address
    if token:
        env["INFRAHUB_API_TOKEN"] = token
    env["DEMO_INIT_PHASE"] = phase
    c.run("uv run python demo-seed/setup.py", pty=False, env=env)


@task
def up(c):
    """Convenience: start + init(data) + register-repo + init(rules) in one go."""
    start(c)
    init(c, phase="data")
    register_repo(c)
    init(c, phase="rules")


demo = Collection("demo")
demo.add_task(prepare_repo_source, name="prepare-repo-source")
demo.add_task(start)
demo.add_task(stop)
demo.add_task(reset)
demo.add_task(status)
demo.add_task(logs)
demo.add_task(register_repo, name="register-repo")
demo.add_task(init)
demo.add_task(up)

namespace = Collection()
namespace.add_collection(demo)
