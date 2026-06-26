"""Register this live-demo branch as a CoreRepository on the running stack.

Without a registered CoreRepository, Infrahub never processes
.infrahub.yml on this branch, which means the ``path_traversal_url``
Python transform is never installed and the rule's
``path_traversal_url`` attribute stays ``null``.

The docker-compose ``task-worker`` service mounts this repository's
working tree read-only at ``/srv/reachability``. Registering a
CoreRepository with ``location=file:///srv/reachability`` and
``--ref live-demo`` is enough for the worker to clone, parse
``.infrahub.yml``, and install the Python transform. No external
git server, no daemon, no public push required.

After the worker finishes the sync (we poll for
``CoreTransformPython.name=path_traversal_url``), every rule create
or update populates the ``path_traversal_url`` attribute automatically.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from infrahub_sdk import Config, InfrahubClient
from infrahub_sdk.exceptions import GraphQLError

REPO_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_NAME = "reachability-check"
REPOSITORY_LOCATION = "file:///srv/reachability"
TRANSFORM_NAME = "path_traversal_url"
CHECK_DEFINITION_NAME = "reachability_assertion"
POLL_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5


def _register_via_ctl(location: str) -> None:
    """Shell out to ``infrahubctl repository add`` for the live-demo repo.

    The served bare clone (see ``tasks.py:_prepare_bare_clone``)
    exposes a single git branch renamed to ``main``, so no ``--ref``
    argument is needed; Infrahub's default branch maps to the only
    branch present in the served repo. The subprocess is invoked
    with ``check=True`` so any non-zero exit from ``infrahubctl``
    surfaces as a ``CalledProcessError`` and stops the script.
    """
    cmd = [
        "infrahubctl",
        "repository",
        "add",
        REPOSITORY_NAME,
        location,
    ]
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


async def _existing_repo(client: InfrahubClient) -> Any | None:
    """Return the existing ``reachability-check`` repository, or ``None``.

    Used to make the register step idempotent: a re-run after the
    repository is already registered prints a "skipping add" notice
    and proceeds straight to waiting on the sync artefacts.
    """
    matches = await client.filters(kind="CoreRepository", name__value=REPOSITORY_NAME)
    return matches[0] if matches else None


async def _wait_for_sync_artifacts(client: InfrahubClient) -> None:
    """Poll until both the Python transform and the check definition exist.

    The CoreRepository sync installs many artefacts in order:
    schema, menu, GraphQL queries, transforms, check definitions. We
    wait specifically on the last two we need, so we can be sure the
    sync has reached the check definition step before this script
    returns.
    """
    print(
        f"Waiting up to {POLL_TIMEOUT_SECONDS}s for the repository sync "
        f"to install '{TRANSFORM_NAME}' and '{CHECK_DEFINITION_NAME}'...",
        flush=True,
    )
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            transforms = await client.filters(
                kind="CoreTransformPython", name__value=TRANSFORM_NAME
            )
            checks = await client.filters(
                kind="CoreCheckDefinition", name__value=CHECK_DEFINITION_NAME
            )
        except GraphQLError:
            transforms = []
            checks = []
        if transforms and checks:
            print(
                f"Transform '{TRANSFORM_NAME}' and check definition "
                f"'{CHECK_DEFINITION_NAME}' are installed."
            )
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(
        f"Timed out after {POLL_TIMEOUT_SECONDS}s waiting for the sync to "
        f"install '{TRANSFORM_NAME}' and '{CHECK_DEFINITION_NAME}'. Check "
        f"the task-worker logs with `docker compose logs task-worker` to "
        f"diagnose the repository sync."
    )


async def main() -> None:
    """Entry point. Registers the CoreRepository and waits for the sync.

    Steps, in order:
      1. Validate that ``INFRAHUB_ADDRESS`` and
         ``INFRAHUB_API_TOKEN`` are exported.
      2. If a ``CoreRepository`` named ``reachability-check``
         already exists, skip the create.
      3. Otherwise run ``infrahubctl repository add`` against the
         ``LIVE_DEMO_REPO_URL`` (defaulting to
         ``file:///srv/reachability``, served by the task-worker
         bind mount).
      4. Poll until ``CoreTransformPython(path_traversal_url)``
         AND ``CoreCheckDefinition(reachability_assertion)`` both
         exist, which is the signal that the task worker has
         finished parsing ``.infrahub.yml`` on the synced branch.

    Driven by the ``uv run invoke demo.register-repo`` task.
    """
    address = os.environ.get("INFRAHUB_ADDRESS")
    token = os.environ.get("INFRAHUB_API_TOKEN")
    if not address or not token:
        print(
            "ERROR: INFRAHUB_ADDRESS and INFRAHUB_API_TOKEN must be exported.",
            file=sys.stderr,
        )
        sys.exit(2)

    location = os.environ.get("LIVE_DEMO_REPO_URL", REPOSITORY_LOCATION)
    client = InfrahubClient(config=Config(address=address, api_token=token))

    if await _existing_repo(client):
        print(f"CoreRepository {REPOSITORY_NAME!r} already registered; skipping add.")
    else:
        _register_via_ctl(location)

    await _wait_for_sync_artifacts(client)


if __name__ == "__main__":
    asyncio.run(main())
