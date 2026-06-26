"""Invoke tasks for the reachability-check demo branch.

Run with `uv run invoke <task>`. Verbs mirror Infrahub's own
`dev.start` / `dev.init` convention so the muscle memory transfers:

  uv run invoke demo.start    # docker compose up + wait for healthy
  uv run invoke demo.init     # load schema + data + create rules
  uv run invoke demo.up       # start + init in one go
  uv run invoke demo.status   # ping the running stack
  uv run invoke demo.logs     # tail infrahub-server logs
  uv run invoke demo.stop     # docker compose down (preserves volumes)
  uv run invoke demo.reset    # docker compose down -v (wipes everything)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from invoke.collection import Collection
from invoke.tasks import task

ROOT = Path(__file__).resolve().parent

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


@task(help={"wait": "Wait until infrahub-server returns 200 on /api/config."})
def start(c, wait=True):
    """Bring the Infrahub 1.10 stack up."""
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
def init(c, address=None, token=None):
    """Load network + reachability schemas, seed data, create rules."""
    env = _docker_env()
    if address:
        env["INFRAHUB_ADDRESS"] = address
    if token:
        env["INFRAHUB_API_TOKEN"] = token
    c.run("uv run python demo-seed/setup.py", pty=False, env=env)


@task(pre=[start, init])
def up(c):
    """Convenience: start + init in one go."""


demo = Collection("demo")
demo.add_task(start)
demo.add_task(stop)
demo.add_task(reset)
demo.add_task(status)
demo.add_task(logs)
demo.add_task(init)
demo.add_task(up)

namespace = Collection()
namespace.add_collection(demo)
