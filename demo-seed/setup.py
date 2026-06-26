"""Bootstrap the reachability-check demo against a running Infrahub 1.10.

Run after ``docker compose up -d`` is healthy. Requires SDK 1.22 or
later. ``INFRAHUB_ADDRESS`` and ``INFRAHUB_API_TOKEN`` must be exported.

The script is split into two phases so the CoreRepository registration
can run between them:

  Phase 1 ("data", default):
      - Load schemas (network, reachability).
      - Load the menu.
      - Load ASNs, devices, BGP sessions, and the reachability-rules group.

  Phase 2 ("rules"):
      - Create the two TopologyReachabilityRule instances and their
        TopologyReachabilityConstraint children.

The recommended order is::

    uv run invoke demo.start             # boots Infrahub 1.10
    uv run invoke demo.init              # phase 1: data
    uv run invoke demo.register-repo     # CoreRepository sync installs the transform
    uv run invoke demo.init --phase rules
        # phase 2: rules. Rule creation now fires the path_traversal_url
        # transform automatically and populates the URL on every rule.

``demo.up`` performs the four steps in order.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from infrahub_sdk import Config, InfrahubClient

REPO_ROOT = Path(__file__).resolve().parent.parent

# Phase 1 loads everything except the rules. The schemas and menu are
# also installed by the CoreRepository sync, but loading them manually
# (and idempotently) up-front guarantees they exist before the data
# load and before any rule create, regardless of the order the user
# runs the invoke tasks in.
SCHEMAS = [
    "demo-seed/schemas/network.yml",
    "schemas/reachability.yml",
]
MENUS = [
    "menus/reachability.yml",
]
DATA_FILES = [
    "demo-seed/data/01-asns.yml",
    "demo-seed/data/02-devices.yml",
    "demo-seed/data/03-bgp-sessions.yml",
    "data/group.yml",
]

GROUP_NAME = "reachability-rules"

RULES: list[dict] = [
    {
        "name": "atl-to-jfk-via-as64496",
        "description": "atl1-edge1 ↔ jfk1-edge1 must transit AS64496.",
        "source": {"kind": "InfraDevice", "hfid": "atl1-edge1"},
        "destination": {"kind": "InfraDevice", "hfid": "jfk1-edge1"},
        "max_depth": 3,
        "max_paths": 50,
        "enabled": True,
        "constraints": [
            {"polarity": "required", "hop_kind": "InfraAutonomousSystem", "attribute_name": "asn", "attribute_value": "64496"},
            {"polarity": "forbidden", "hop_kind": "InfraAutonomousSystem", "attribute_name": "asn", "attribute_value": "8220"},
        ],
    },
    {
        "name": "atl-to-dfw-via-as64496",
        "description": "atl1-edge1 ↔ dfw1-edge1 must transit AS64496.",
        "source": {"kind": "InfraDevice", "hfid": "atl1-edge1"},
        "destination": {"kind": "InfraDevice", "hfid": "dfw1-edge1"},
        "max_depth": 3,
        "max_paths": 50,
        "enabled": True,
        "constraints": [
            {"polarity": "required", "hop_kind": "InfraAutonomousSystem", "attribute_name": "asn", "attribute_value": "64496"},
        ],
    },
]


def _run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


def _load_schemas_and_data() -> None:
    for schema in SCHEMAS:
        _run(["infrahubctl", "schema", "load", schema])
    for menu in MENUS:
        _run(["infrahubctl", "menu", "load", menu])
    for data in DATA_FILES:
        _run(["infrahubctl", "object", "load", data])


async def _resolve(client: InfrahubClient, ref: dict) -> str:
    node = await client.get(kind=ref["kind"], hfid=[ref["hfid"]])
    return node.id


async def _seed_rules() -> None:
    client = InfrahubClient(
        config=Config(
            address=os.environ["INFRAHUB_ADDRESS"],
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )

    group_results = await client.filters(kind="CoreStandardGroup", name__value=GROUP_NAME)
    if not group_results:
        group = await client.create(kind="CoreStandardGroup", name=GROUP_NAME)
        await group.save()
    else:
        group = group_results[0]
    await group.members.fetch()

    for spec in RULES:
        source_id = await _resolve(client, spec["source"])
        destination_id = await _resolve(client, spec["destination"])

        existing = await client.filters(kind="TopologyReachabilityRule", name__value=spec["name"])
        if existing:
            rule = existing[0]
            rule.description.value = spec.get("description")
            rule.max_depth.value = spec.get("max_depth", 8)
            rule.max_paths.value = spec.get("max_paths", 50)
            rule.enabled.value = spec.get("enabled", True)
            rule.source = source_id
            rule.destination = destination_id
            await rule.save()
            print(f"updated rule {rule.name.value}")
        else:
            rule = await client.create(
                kind="TopologyReachabilityRule",
                name=spec["name"],
                description=spec.get("description"),
                max_depth=spec.get("max_depth", 8),
                max_paths=spec.get("max_paths", 50),
                enabled=spec.get("enabled", True),
                source=source_id,
                destination=destination_id,
            )
            await rule.save()
            print(f"created rule {rule.name.value}")

        group.members.add(rule)
        await group.save()

        await rule.constraints.fetch()
        for existing_constraint in list(rule.constraints.peers):
            await existing_constraint.peer.delete()

        for c in spec.get("constraints", []):
            constraint = await client.create(
                kind="TopologyReachabilityConstraint",
                rule=rule.id,
                polarity=c["polarity"],
                hop_kind=c["hop_kind"],
                attribute_name=c.get("attribute_name"),
                attribute_value=c.get("attribute_value"),
            )
            await constraint.save()
        print(f"  constraints: {len(spec.get('constraints', []))}")


def main() -> None:
    if "INFRAHUB_ADDRESS" not in os.environ or "INFRAHUB_API_TOKEN" not in os.environ:
        print(
            "ERROR: INFRAHUB_ADDRESS and INFRAHUB_API_TOKEN must be exported.\n"
            "  e.g.  export INFRAHUB_ADDRESS=http://localhost:8000\n"
            "        export INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec",
            file=sys.stderr,
        )
        sys.exit(2)

    phase = (os.environ.get("DEMO_INIT_PHASE") or "data").strip().lower()
    if phase == "data":
        _load_schemas_and_data()
    elif phase == "rules":
        asyncio.run(_seed_rules())
    elif phase == "all":
        _load_schemas_and_data()
        asyncio.run(_seed_rules())
    else:
        print(
            f"ERROR: DEMO_INIT_PHASE='{phase}' is not recognised. "
            "Expected one of: data, rules, all.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
