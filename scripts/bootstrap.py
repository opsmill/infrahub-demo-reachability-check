"""Bootstrap reachability rules + constraints from human-friendly references.

`TopologyReachabilityRule.source` and `.destination` are `peer: CoreNode`, so the
YAML loader (`infrahubctl object load`) cannot resolve them by hfid — it
needs UUIDs. This script accepts a list of rules with `kind` + `hfid` for
source and destination, resolves them via the SDK, and creates / upserts
the rule and constraint nodes.

The forbidden ASN here is AS8220 (Colt Communications) — already wired
into the standard demo data on both atl1 and jfk1 via existing BGP
sessions. With `atl1-edge1.asn = AS64496` on main, no depth<=3 path
between atl1 and jfk1 transits AS8220, so the baseline check passes.
The recorded demo PC only needs to repoint atl1-edge1.asn to AS8220
to surface a 3-hop path through the forbidden ASN.

Edit the RULES list below to describe your topology, then run:

    INFRAHUB_ADDRESS=... INFRAHUB_API_TOKEN=... \
        uv run python scripts/bootstrap.py
"""

from __future__ import annotations

import asyncio
import os

from infrahub_sdk import Config, InfrahubClient

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


async def resolve(client: InfrahubClient, ref: dict) -> str:
    node = await client.get(kind=ref["kind"], hfid=[ref["hfid"]])
    return node.id


async def main() -> None:
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
        source_id = await resolve(client, spec["source"])
        destination_id = await resolve(client, spec["destination"])

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


if __name__ == "__main__":
    asyncio.run(main())
