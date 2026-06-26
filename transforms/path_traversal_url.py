"""Computed-attribute transform that builds the path-traversal URL for a
TopologyReachabilityRule.

Runs server-side as a ``TransformPython`` computed attribute (Infrahub
1.10 or later). The schema declares ``path_traversal_url`` on the rule
with ``computed_attribute: { kind: TransformPython, transform:
path_traversal_url }``. The server invokes this transform whenever the
rule (or any attribute it depends on) changes. The returned string is
stored on the rule and read by the check at evaluation time.

The transform returns a **relative URL** (no scheme or host). The
Infrahub UI resolves it against the current page, so the same value
works on ``http://localhost:8000``, ``https://infrahub.your-company.com``,
or any other deployment URL without any environment configuration. The
URL is also noticeably shorter than the equivalent absolute form, which
matters because the value renders on the rule detail page and in every
verdict log message the check emits.

URL parameters:
  source         UUID of the source endpoint.
  destination    UUID of the destination endpoint.
  depth          ``max_depth`` from the rule.
  maxPaths       ``max_paths`` from the rule.
  excludedKinds  The rule and constraint kinds, so the traversal page
                 shows the same hops the check evaluated. Constraint
                 hop_kinds are NOT excluded; they are exactly the
                 hops the rule cares about.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

# Kinds excluded from the traversal URL. Mirrors the check's own
# ``EXCLUDED_KINDS`` so the link opens the same set of hops the check
# evaluated. Adopters whose schema has additional "shortcut" kinds (for
# example ``InfraPlatform`` in the standard ``models/base`` schemas, or
# a global ``Tag`` / ``Tenant`` / ``Vendor`` node) should add those
# kinds to this tuple AND to the matching list in
# ``queries/path_check.gql``.
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

        parts: list[str] = [f"source={source_id}", f"destination={destination_id}"]
        if max_depth is not None:
            parts.append(f"depth={max_depth}")
        if max_paths is not None:
            parts.append(f"maxPaths={max_paths}")
        for kind in EXCLUDED_KINDS:
            parts.append(f"excludedKinds={kind}")

        return f"/path-traversal?{'&'.join(parts)}"
