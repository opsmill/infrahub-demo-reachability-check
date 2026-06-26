"""Computed-attribute transform that builds the path-traversal URL for a
TopologyReachabilityRule.

Runs server-side as a ``TransformPython`` computed attribute (Infrahub 1.10+).
The schema declares ``path_traversal_url`` on the rule with
``computed_attribute: { kind: TransformPython, transform: path_traversal_url }``,
and the server invokes this transform whenever the rule (or any attribute it
depends on) changes. The returned string is stored on the rule and read by the
check at evaluation time.

URL parameters:
  source         — UUID of the source endpoint
  destination    — UUID of the destination endpoint
  depth          — max_depth from the rule
  maxPaths       — max_paths from the rule
  excludedKinds  — the rule + constraint kinds (so the traversal page shows
                   the same hops the check evaluated). Constraint hop_kinds
                   are NOT excluded — they are exactly the hops we want to
                   see.

The base URL comes from $INFRAHUB_PUBLIC_URL on the worker (set this in
production to the operator-facing URL); falls back to http://localhost:8000
for the standard local dev stack.
"""

from __future__ import annotations

import os
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

PUBLIC_URL_ENV = "INFRAHUB_PUBLIC_URL"
DEFAULT_PUBLIC_URL = "http://localhost:8000"

# Kinds excluded from the traversal. The main-branch transform also
# includes "InfraPlatform" so the URL points the user at the same hops
# the check used. The live-demo branch runs against a minimal schema
# without InfraPlatform, so we omit it here to keep the URL aligned
# with the check's EXCLUDED_KINDS list (see checks/path_assertion.py).
EXCLUDED_KINDS: tuple[str, ...] = (
    "TopologyReachabilityRule",
    "TopologyReachabilityConstraint",
)


class PathTraversalUrl(InfrahubTransform):
    query = "rule_url"

    async def transform(self, data: dict[str, Any]) -> str:
        edges = data.get("TopologyReachabilityRule", {}).get("edges") or []
        if not edges:
            return ""
        rule = edges[0]["node"]

        source = (rule.get("source") or {}).get("node") or {}
        destination = (rule.get("destination") or {}).get("node") or {}
        source_id = source.get("id")
        destination_id = destination.get("id")
        if not source_id or not destination_id:
            return ""

        max_depth = (rule.get("max_depth") or {}).get("value")
        max_paths = (rule.get("max_paths") or {}).get("value")

        base = (os.environ.get(PUBLIC_URL_ENV) or DEFAULT_PUBLIC_URL).rstrip("/")
        parts: list[str] = [f"source={source_id}", f"destination={destination_id}"]
        if max_depth is not None:
            parts.append(f"depth={max_depth}")
        if max_paths is not None:
            parts.append(f"maxPaths={max_paths}")
        for kind in EXCLUDED_KINDS:
            parts.append(f"excludedKinds={kind}")

        return f"{base}/path-traversal?{'&'.join(parts)}"
