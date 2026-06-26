"""Reachability path-assertion check.

Each member of the targeted group is a TopologyReachabilityRule node. The runner
fans out one invocation per member and resolves the per-rule path expressions
declared in .infrahub.yml into self.params. This check then loads the rule
with its constraint children and evaluates each path returned by
InfrahubPathTraversal against the required / forbidden / any_of constraints.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

GRAPHQL_VARS = ("source_id", "destination_id", "max_depth", "max_paths")
FALSEY_STRINGS = frozenset({"false", "no", "0", "off"})

# The worker's view of Infrahub (`INFRAHUB_ADDRESS` inside docker-compose)
# is usually not browser-reachable, so we hard-code a sensible default for
# the standard local dev stack and let operators override per-environment
# via INFRAHUB_PUBLIC_URL (e.g. https://infrahub.your-company.com).
PUBLIC_URL_ENV = "INFRAHUB_PUBLIC_URL"
DEFAULT_PUBLIC_URL = "http://localhost:8000"

# Must mirror `excluded_kinds:` in queries/path_check.gql — keeping them in
# sync ensures the UI link the verdict points at shows the same paths the
# check evaluated. Without this, the path-traversal page would include the
# rule/constraint nodes as 1-hop shortcuts between source and destination.
EXCLUDED_KINDS = (
    "TopologyReachabilityRule",
    "TopologyReachabilityConstraint",
    "InfraPlatform",
)


def _is_disabled(value: Any) -> bool:
    """`enabled` survives both Python `False` (from `member.extract`) and string
    forms (from `infrahubctl check key=value`). Treat anything falsy-looking
    as disabled; otherwise default to enabled.
    """
    if value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in FALSEY_STRINGS:
        return True
    return False


def _normalize(value: Any) -> str:
    """Stringify for hop-attribute comparison.

    Booleans render lower-case so a Text attribute_value of "true"/"false"
    matches a Python bool. Everything else uses plain str().
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class PathAssertionCheck(InfrahubCheck):
    query = "path_check"
    timeout = 120

    async def collect_data(self) -> dict:
        source_id = self.params.get("source_id")
        destination_id = self.params.get("destination_id")
        if source_id and source_id == destination_id:
            return {"_self_loop": True}

        variables = {key: self.params[key] for key in GRAPHQL_VARS if key in self.params}
        return await self.client.query_gql_query(
            name=self.query,
            branch_name=self.branch_name,
            variables=variables,
        )

    def _path_traversal_url(self) -> str | None:
        """Build an absolute path-traversal URL for the verdict log line.

        The base URL comes from ``$INFRAHUB_PUBLIC_URL`` (set this in your
        worker environment if Infrahub is not at the default), falling back
        to ``http://localhost:8000`` for the standard ``invoke dev.start``
        stack. Returns None if either endpoint UUID is missing.
        """
        source_id = self.params.get("source_id")
        destination_id = self.params.get("destination_id")
        if not source_id or not destination_id:
            return None
        base = (os.environ.get(PUBLIC_URL_ENV) or DEFAULT_PUBLIC_URL).rstrip("/")
        query = f"source={source_id}&destination={destination_id}"
        for param_key, url_key in (("max_depth", "depth"), ("max_paths", "maxPaths")):
            value = self.params.get(param_key)
            if value is not None:
                query += f"&{url_key}={value}"
        for kind in EXCLUDED_KINDS:
            query += f"&excludedKinds={kind}"
        return f"{base}/path-traversal?{query}"

    async def validate(self, data: dict) -> None:
        if _is_disabled(self.params.get("enabled")):
            self.log_info(message="Rule is disabled; skipping evaluation.")
            return

        if data.get("_self_loop"):
            self.log_error(
                message="Rule source and destination resolve to the same node — assertion is ill-defined.",
            )
            return

        rule_id = self.params.get("rule_id")
        if not rule_id:
            self.log_error(message="Rule id was not extracted into params; cannot load constraints.")
            return

        rule = await self.client.get(
            kind="TopologyReachabilityRule",
            id=rule_id,
            branch=self.branch_name,
            include=["constraints"],
            prefetch_relationships=True,
        )
        constraints = [related.peer for related in rule.constraints.peers]

        result = data.get("InfrahubPathTraversal") or {}
        paths = result.get("paths") or []
        source_label = (result.get("source") or {}).get("display_label") or self.params.get("source_id")
        dest_label = (result.get("destination") or {}).get("display_label") or self.params.get("destination_id")
        max_depth = self.params.get("max_depth", 8)
        url = self._path_traversal_url()
        url_block = f"\n\nInspect in UI:\n{url}" if url else ""

        if not paths:
            self.log_error(
                message=f"No path within depth {max_depth} between '{source_label}' and '{dest_label}'.{url_block}",
            )
            return

        if not constraints:
            self.log_info(
                message=f"No constraints defined; reachability satisfied across {len(paths)} path(s).{url_block}",
            )
            return

        if not self._validate_constraint_authoring(constraints):
            return

        attr_values = await self._fetch_attributes(paths=paths, constraints=constraints)

        required = [c for c in constraints if c.polarity.value == "required"]
        forbidden = [c for c in constraints if c.polarity.value == "forbidden"]
        any_of = [c for c in constraints if c.polarity.value == "any_of"]

        # `forbidden` is a global invariant — a single offending hop on any
        # returned path fails the check, even if other paths satisfy required.
        # `required` and `any_of` keep existence semantics: at least one path
        # must include all required hops and at least one any_of option.
        forbidden_hits: list[str] = []
        requirement_violations: list[str] = []
        valid_count = 0
        for path in paths:
            hops = [hop["node"] for hop in path["hops"]]
            trail = " → ".join(hop["display_label"] for hop in hops)
            for c in forbidden:
                if any(self._hop_matches(hop, c, attr_values) for hop in hops):
                    forbidden_hits.append(f"[{trail}]: forbidden {self._describe(c)} present")

            problems: list[str] = []
            for c in required:
                if not any(self._hop_matches(hop, c, attr_values) for hop in hops):
                    problems.append(f"missing required {self._describe(c)}")
            if any_of and not any(self._hop_matches(hop, c, attr_values) for hop in hops for c in any_of):
                options = " | ".join(self._describe(c) for c in any_of)
                problems.append(f"none of any_of constraints matched: {options}")

            if problems:
                requirement_violations.append(f"[{trail}]: {'; '.join(problems)}")
            else:
                valid_count += 1

        errors: list[str] = list(forbidden_hits)
        if (required or any_of) and valid_count == 0:
            errors.extend(requirement_violations)

        if errors:
            for line in errors:
                self.log_error(message=line)
            if url:
                self.log_error(message=f"Inspect in UI:\n{url}")
            return

        self.log_info(
            message=(
                f"{valid_count}/{len(paths)} paths satisfy all constraints "
                f"(cap: max_paths={self.params.get('max_paths')}).{url_block}"
            ),
        )

    def _validate_constraint_authoring(self, constraints: list[Any]) -> bool:
        """Surface authoring mistakes that would silently make constraints no-ops."""
        ok = True
        for c in constraints:
            polarity = c.polarity.value
            if polarity not in {"required", "forbidden", "any_of"}:
                self.log_error(message=f"Constraint '{c.label.value}': unknown polarity '{polarity}'.")
                ok = False
            if not c.hop_kind.value:
                self.log_error(message=f"Constraint '{c.label.value}': hop_kind is empty.")
                ok = False
            if c.attribute_name.value and c.attribute_value.value is None:
                self.log_error(
                    message=f"Constraint '{c.label.value}': attribute_name set but attribute_value missing.",
                )
                ok = False
        return ok

    def _hop_matches(self, hop_node: dict, constraint: Any, attr_values: dict) -> bool:
        if hop_node["kind"] != constraint.hop_kind.value:
            return False
        attribute_name = constraint.attribute_name.value
        if attribute_name is None:
            return True
        actual = attr_values.get((constraint.hop_kind.value, attribute_name, hop_node["id"]))
        if actual is None:
            return False
        return _normalize(actual) == _normalize(constraint.attribute_value.value)

    def _describe(self, constraint: Any) -> str:
        attribute_name = constraint.attribute_name.value
        if attribute_name is None:
            return f"hop of kind {constraint.hop_kind.value}"
        return (
            f"hop of kind {constraint.hop_kind.value} "
            f"with {attribute_name}={constraint.attribute_value.value}"
        )

    async def _fetch_attributes(
        self,
        paths: list[dict],
        constraints: list[Any],
    ) -> dict[tuple[str, str, str], Any]:
        needed: dict[str, set[str]] = defaultdict(set)
        for constraint in constraints:
            attribute_name = constraint.attribute_name.value
            if attribute_name is not None:
                needed[constraint.hop_kind.value].add(attribute_name)
        if not needed:
            return {}

        async def fetch_kind(kind: str, attribute_names: set[str]) -> dict[tuple[str, str, str], Any]:
            hop_ids = {
                hop["node"]["id"] for path in paths for hop in path["hops"] if hop["node"]["kind"] == kind
            }
            if not hop_ids:
                return {}
            nodes = await self.client.filters(
                kind=kind,
                ids=list(hop_ids),
                branch=self.branch_name,
                include=list(attribute_names),
            )
            sub: dict[tuple[str, str, str], Any] = {}
            for node in nodes:
                for attribute_name in attribute_names:
                    attribute = getattr(node, attribute_name, None)
                    if attribute is not None and getattr(attribute, "value", None) is not None:
                        sub[(kind, attribute_name, node.id)] = attribute.value
            return sub

        sub_results = await asyncio.gather(
            *(fetch_kind(kind, names) for kind, names in needed.items())
        )
        merged: dict[tuple[str, str, str], Any] = {}
        for sub in sub_results:
            merged.update(sub)
        return merged
