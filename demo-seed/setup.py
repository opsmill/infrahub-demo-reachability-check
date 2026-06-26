"""Bootstrap the reachability-check demo against a running Infrahub 1.10.

Run after `docker compose up -d` is healthy. Requires SDK 1.22+
(`pip install infrahub-sdk==1.22.0`) and a populated `.env` (or
INFRAHUB_ADDRESS / INFRAHUB_API_TOKEN env vars).

Steps:
  1. Load the network schema (InfraDevice, InfraAutonomousSystem, InfraBGPSession).
  2. Load ASNs, devices, BGP sessions.
  3. Load the reachability-check schema (TopologyReachabilityRule + TopologyReachabilityConstraint).
  4. Create the reachability-rules group + two rules with their constraints.

The check itself runs inside Infrahub workers via the `.infrahub.yml`
registration — which is loaded automatically once you register this repo
as a CoreRepository in the Infrahub UI. This script does not register
the repository; it only seeds the data the check operates on.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from infrahub_sdk import Config, InfrahubClient

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMAS = [
    "demo-seed/schemas/network.yml",
    "schemas/reachability.yml",
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
    _load_schemas_and_data()
    asyncio.run(_seed_rules())


if __name__ == "__main__":
    main()
