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
REPOSITORY_REF = "live-demo"
REPOSITORY_LOCATION = "file:///srv/reachability"
TRANSFORM_NAME = "path_traversal_url"
POLL_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5


def _register_via_ctl(location: str) -> None:
    cmd = [
        "infrahubctl",
        "repository",
        "add",
        REPOSITORY_NAME,
        location,
        "--ref",
        REPOSITORY_REF,
    ]
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


async def _existing_repo(client: InfrahubClient) -> Any | None:
    matches = await client.filters(kind="CoreRepository", name__value=REPOSITORY_NAME)
    return matches[0] if matches else None


async def _wait_for_transform(client: InfrahubClient) -> None:
    print(
        f"Waiting up to {POLL_TIMEOUT_SECONDS}s for Python transform "
        f"'{TRANSFORM_NAME}' to install...",
        flush=True,
    )
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            transforms = await client.filters(
                kind="CoreTransformPython", name__value=TRANSFORM_NAME
            )
        except GraphQLError:
            transforms = []
        if transforms:
            print(f"Transform '{TRANSFORM_NAME}' is installed.")
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(
        f"Timed out after {POLL_TIMEOUT_SECONDS}s waiting for transform "
        f"'{TRANSFORM_NAME}'. Check the task-worker logs with "
        f"`docker compose logs task-worker` to diagnose the repository sync."
    )


async def main() -> None:
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

    await _wait_for_transform(client)


if __name__ == "__main__":
    asyncio.run(main())
