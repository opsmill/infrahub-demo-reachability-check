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
from typing import Any

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
    """Echo a shell command and run it through ``subprocess.run(check=True)``.

    Used to drive ``infrahubctl`` subprocesses during phase 1
    (``data``). The command is printed verbatim before it runs so
    operators can see exactly what the script is doing if the demo
    bootstrap fails mid-step.
    """
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


def _load_schemas_and_data() -> None:
    """Phase 1 entry point: install schemas, the menu, seed data, and the group.

    Drives ``infrahubctl schema load`` for the network schema and the
    reachability schema, ``infrahubctl menu load`` for the sidebar
    entry, ``infrahubctl object load`` for the demo ASNs / devices /
    BGP sessions, and finally creates the ``reachability-rules``
    ``CoreStandardGroup`` over the SDK so the
    ``CoreCheckDefinition`` repo-sync that runs in phase
    ``register-repo`` finds the group already in place.
    """
    for schema in SCHEMAS:
        _run(["infrahubctl", "schema", "load", schema])
    for menu in MENUS:
        _run(["infrahubctl", "menu", "load", menu])
    for data in DATA_FILES:
        _run(["infrahubctl", "object", "load", data])
    asyncio.run(_ensure_group())


async def _ensure_group() -> None:
    """Create the reachability-rules CoreStandardGroup if it does not exist.

    The CoreCheckDefinition installed by the CoreRepository sync
    references this group as its ``targets:``. The group must therefore
    exist before ``demo.register-repo`` runs, otherwise the sync's
    CoreCheckDefinitionCreate mutation fails with NODE_NOT_FOUND.
    """
    client = InfrahubClient(
        config=Config(
            address=os.environ["INFRAHUB_ADDRESS"],
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )
    existing = await client.filters(kind="CoreStandardGroup", name__value=GROUP_NAME)
    if existing:
        print(f"CoreStandardGroup {GROUP_NAME!r} already exists; skipping create.")
        return
    group = await client.create(kind="CoreStandardGroup", name=GROUP_NAME)
    await group.save()
    print(f"created CoreStandardGroup {GROUP_NAME!r}")


async def _resolve(client: InfrahubClient, ref: dict) -> str:
    """Resolve a ``{kind, hfid}`` reference into the matching node's UUID.

    Demo rules reference their source / destination by hfid
    (``atl1-edge1`` etc.) because that is the form a human would
    type. The SDK's ``client.get(hfid=...)`` does the lookup and
    returns the typed node; this helper just unwraps ``.id``.
    """
    node = await client.get(kind=ref["kind"], hfid=[ref["hfid"]])
    return node.id


async def _seed_rules() -> None:
    """Phase 2 entry point: create the demo rules and their constraints.

    For every entry in ``RULES`` the function resolves the source and
    destination hfids to UUIDs, upserts the
    ``TopologyReachabilityRule`` with the rule's tuning knobs
    (max_depth / max_paths / enabled), adds the rule to the
    ``reachability-rules`` group, deletes any pre-existing
    constraints attached to it, then creates one
    ``TopologyReachabilityConstraint`` per entry under
    ``spec['constraints']``. Idempotent: re-runnable after a previous
    seed without producing duplicates.
    """
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


# Three demo proposed changes mirroring slide 15 of the deck. The third
# one (admin tightens max_depth) is included alongside the two the user
# explicitly asked for because the cost is negligible and it produces
# the "no path within depth 1" failure mode in the recorded demo.
SCENARIOS: list[dict] = [
    {
        "branch": "shernandez-doc-tweak",
        "pc_name": "Sofia Hernandez: atl1 documentation tweak (happy path)",
        "pc_description": (
            "Engineering Team member makes a benign description change "
            "to atl1-edge1. Both reachability rules stay green."
        ),
        "edits": [
            {
                "kind": "InfraDevice",
                "hfid": "atl1-edge1",
                "attributes": {
                    "description": "documentation tweak: updated maintenance window",
                },
            },
        ],
    },
    {
        "branch": "cobrian-reroute-via-colt",
        "pc_name": "Chloe O'Brian: reroute atl1 via Colt (AS8220)",
        "pc_description": (
            "Single-field change: atl1-edge1.asn AS65001 -> AS8220 (Colt). "
            "The atl-to-jfk reachability rule fails on the forbidden hop."
        ),
        "edits": [
            {
                "kind": "InfraDevice",
                "hfid": "atl1-edge1",
                "relationships": {
                    "asn": {"kind": "InfraAutonomousSystem", "hfid": "8220"},
                },
            },
        ],
    },
    {
        "branch": "admin-tighten-depth",
        "pc_name": "Administrator: tighten atl-to-jfk max_depth 3 to 1",
        "pc_description": (
            "Operations Team tightens the rule. The atl-to-jfk rule "
            "fails with 'No path within depth 1'."
        ),
        "edits": [
            {
                "kind": "TopologyReachabilityRule",
                "hfid": "atl-to-jfk-via-as64496",
                "attributes": {"max_depth": 1},
            },
        ],
    },
]


async def _existing_branch(client: InfrahubClient, name: str) -> bool:
    """Return ``True`` if an Infrahub branch with this name already exists.

    Used by ``_seed_scenarios`` so re-running the scenarios phase
    after a successful first run is a no-op instead of an error.
    Tolerates both the dict-return and list-return shapes of
    ``client.branch.all()`` across SDK versions.
    """
    branches = await client.branch.all()
    if isinstance(branches, dict):
        return name in branches
    return any(getattr(b, "name", None) == name for b in branches)


async def _apply_edit(client: InfrahubClient, branch: str, edit: dict) -> None:
    """Apply a single-field edit to a node on a given branch.

    Each ``edit`` is a dict that names the target node by
    ``{kind, hfid}`` and supplies the changes as ``attributes``
    (scalar attribute writes) and / or ``relationships``
    (cardinality-one relationship reassignments by peer hfid). The
    function loads the node on the branch, applies every field, and
    saves once. Demo scenarios use the single-field variant; the
    helper supports multi-field edits for future scenarios.
    """
    node = await client.get(kind=edit["kind"], hfid=[edit["hfid"]], branch=branch)
    for attribute_name, value in edit.get("attributes", {}).items():
        attribute = getattr(node, attribute_name)
        attribute.value = value
    for relationship_name, ref in edit.get("relationships", {}).items():
        peer = await client.get(kind=ref["kind"], hfid=[ref["hfid"]], branch=branch)
        setattr(node, relationship_name, peer.id)
    await node.save()


async def _existing_pc(client: InfrahubClient, name: str) -> Any | None:
    """Return the existing ``CoreProposedChange`` with this name, or ``None``.

    Lookup by ``name__value`` because ``name`` is the only field the
    scenarios bootstrap controls. Used to make ``_seed_scenarios``
    idempotent: a second pass skips PCs that already exist instead of
    creating duplicates.
    """
    matches = await client.filters(kind="CoreProposedChange", name__value=name)
    return matches[0] if matches else None


async def _seed_scenarios() -> None:
    """Create the demo branches, apply the single-field edits, and open the PCs.

    Mirrors slide 15 of the deck. Each scenario is a one-field change on
    its own Infrahub branch; opening the proposed change kicks off the
    standard validation pipeline, which fans the reachability check out
    across the rules group and produces one verdict card per rule.
    """
    client = InfrahubClient(
        config=Config(
            address=os.environ["INFRAHUB_ADDRESS"],
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )

    for scenario in SCENARIOS:
        branch_name = scenario["branch"]
        if await _existing_branch(client, branch_name):
            print(f"branch {branch_name} already exists")
        else:
            # ``sync_with_git=False``: these demo branches mutate data
            # only; no git commit is authored on them, so triggering a
            # git import workflow per branch is pure latency.
            await client.branch.create(branch_name=branch_name, sync_with_git=False)
            print(f"created branch {branch_name}")

        for edit in scenario["edits"]:
            await _apply_edit(client, branch_name, edit)
        print(f"  applied {len(scenario['edits'])} edit(s) on {branch_name}")

        existing = await _existing_pc(client, scenario["pc_name"])
        if existing:
            print(f"proposed change '{scenario['pc_name']}' already exists")
            continue
        pc = await client.create(
            kind="CoreProposedChange",
            name=scenario["pc_name"],
            description=scenario["pc_description"],
            source_branch=branch_name,
            destination_branch="main",
        )
        await pc.save()
        print(f"opened proposed change '{scenario['pc_name']}'")


def main() -> None:
    """Entry point. Dispatches to the phase named by ``DEMO_INIT_PHASE``.

    Recognised phases:
      ``data``       Load schemas + menu + seed data + the rules group.
      ``rules``      Create the demo rules and their constraints.
      ``scenarios``  Create the demo branches and proposed changes.
      ``all``        All three, in order.

    Driven from ``tasks.py``'s ``demo.init`` invoke task, which sets
    ``DEMO_INIT_PHASE`` based on the ``--phase`` argument. Requires
    ``INFRAHUB_ADDRESS`` and ``INFRAHUB_API_TOKEN`` in the
    environment; the invoke task exports both from ``.env`` by default.
    """
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
    elif phase == "scenarios":
        asyncio.run(_seed_scenarios())
    elif phase == "all":
        _load_schemas_and_data()
        asyncio.run(_seed_rules())
        asyncio.run(_seed_scenarios())
    else:
        print(
            f"ERROR: DEMO_INIT_PHASE='{phase}' is not recognised. "
            "Expected one of: data, rules, scenarios, all.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
