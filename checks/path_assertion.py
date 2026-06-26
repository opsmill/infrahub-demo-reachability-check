"""Reachability path-assertion check (SDK 1.22 edition).

Same semantics as the ``main`` branch version, but uses
``InfrahubClient.traverse_paths()`` (added in SDK 1.22, requires Infrahub
1.10 or later) instead of a stored ``CoreGraphQLQuery``. The stored
``path_check`` query still exists as a minimal placeholder because
``InfrahubCheck`` requires ``query`` to be set, but it is never
executed. The SDK helper builds its own GraphQL call and returns a
typed ``PathTraversalResult``.

The click-through URL is not built here. It is computed server-side by
the ``path_traversal_url`` Python transform (registered in
``.infrahub.yml``) and stored on the rule as a read-only attribute.
The check just reads ``rule.path_traversal_url.value`` when assembling
the verdict log line.

Each member of the targeted group is a ``TopologyReachabilityRule``.
The runner extracts only the rule id from each member, and the check
fetches the rule together with its constraint children. Source,
destination, max_depth, max_paths, and enabled are then read directly
off the rule node.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from infrahub_sdk.graph_traversal import Path, PathTraversalResult

FALSEY_STRINGS = frozenset({"false", "no", "0", "off"})

# Kinds excluded from the traversal. Without this, the rule node itself
# (cardinality-one source and destination) becomes a one-hop shortcut
# between the endpoints and every reachability assertion collapses to a
# trivial "the rule connects them" path.
#
# On the ``main`` branch this tuple also includes ``InfraPlatform``
# because every device on the same vendor stack would otherwise share
# a platform node and the traversal would prefer that two-hop shortcut.
# The live-demo branch ships a minimal schema (no InfraPlatform) so we
# drop it here. The GraphQL server rejects ``excluded_kinds`` values
# that are not in the loaded schema.
EXCLUDED_KINDS: tuple[str, ...] = (
    "TopologyReachabilityRule",
    "TopologyReachabilityConstraint",
)


def _is_disabled(value: Any) -> bool:
    """Return True when ``enabled`` should be treated as disabled.

    The ``enabled`` field is a Boolean attribute on the rule, but the
    check can also be invoked through ``infrahubctl check key=value``
    where it arrives as a string. Both forms are handled.
    """
    if value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in FALSEY_STRINGS:
        return True
    return False


def _normalize(value: Any) -> str:
    """Stringify for hop-attribute comparison.

    Booleans render lower-case so a Text ``attribute_value`` of
    ``"true"`` or ``"false"`` matches a Python bool. Everything else
    uses plain ``str()``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PathAssertionCheck(InfrahubCheck):
    # ``query`` is required by ``InfrahubCheck.__init__`` and the
    # ``CoreCheckDefinition`` repo-sync flow. A stored ``path_check``
    # query is registered in ``.infrahub.yml`` so the check passes
    # validation, but ``collect_data`` does not execute it.
    # ``traverse_paths()`` is called directly instead.
    query = "path_check"
    timeout = 120

    # The check fetches the rule once in collect_data and stashes both
    # the rule and the traversal result so validate() does not have to
    # round-trip again.
    _rule: Any | None = None
    _traversal_result: PathTraversalResult | None = None

    async def collect_data(self) -> dict:
        rule_id = self.params.get("rule_id")
        if not rule_id:
            return {"_no_rule_id": True}

        rule = await self.client.get(
            kind="TopologyReachabilityRule",
            id=rule_id,
            branch=self.branch_name,
            include=["constraints"],
            prefetch_relationships=True,
        )
        self._rule = rule

        if _is_disabled(rule.enabled.value):
            return {"_disabled": True}

        source_id = rule.source.id
        destination_id = rule.destination.id
        if source_id and source_id == destination_id:
            return {"_self_loop": True}

        if not source_id or not destination_id:
            return {"_missing_endpoints": True}

        self._traversal_result = await self.client.traverse_paths(
            source=source_id,
            destination=destination_id,
            max_depth=_coerce_int(rule.max_depth.value),
            max_paths=_coerce_int(rule.max_paths.value),
            excluded_kinds=list(EXCLUDED_KINDS),
            branch=self.branch_name,
        )
        return {"_ok": True}

    @staticmethod
    def _rule_url(rule: Any) -> str | None:
        """Return the rule's computed ``path_traversal_url`` value, if any.

        The URL is built server-side by the ``path_traversal_url`` Python
        transform (see ``transforms/path_traversal_url.py``) and stored on
        the rule as a read-only computed attribute. The check just reads it.
        """
        attribute = getattr(rule, "path_traversal_url", None)
        if attribute is None:
            return None
        value = getattr(attribute, "value", None)
        if not value:
            return None
        return str(value)

    async def validate(self, data: dict) -> None:
        if data.get("_no_rule_id"):
            self.log_error(message="Rule id was not extracted into params; cannot load rule.")
            return

        if data.get("_disabled"):
            self.log_info(message="Rule is disabled; skipping evaluation.")
            return

        if data.get("_self_loop"):
            self.log_error(
                message="Rule source and destination resolve to the same node; assertion is ill-defined.",
            )
            return

        if data.get("_missing_endpoints"):
            self.log_error(
                message="Rule is missing source or destination; cannot evaluate.",
            )
            return

        rule = self._rule
        if rule is None:
            self.log_error(message="Rule was not loaded; cannot evaluate constraints.")
            return

        constraints = [related.peer for related in rule.constraints.peers]

        result = self._traversal_result
        paths: list[Path] = list(result.paths) if result else []
        source_label = (
            result.source.display_label if result and result.source else getattr(rule.source, "display_label", None)
        )
        dest_label = (
            result.destination.display_label
            if result and result.destination
            else getattr(rule.destination, "display_label", None)
        )
        max_depth = rule.max_depth.value
        url = self._rule_url(rule)
        url_block = f"\n\nInspect in UI:\n{url}" if url else ""

        if not paths:
            self.log_error(
                message=f"No path within depth {max_depth} between '{source_label}' and '{dest_label}'.{url_block}",
            )
            return

        if not constraints:
            self.log_info(
                message=(
                    f"No constraints defined; reachability satisfied across {len(paths)} path(s).{url_block}"
                ),
            )
            return

        if not self._validate_constraint_authoring(constraints):
            return

        attr_values = await self._fetch_attributes(paths=paths, constraints=constraints)

        required = [c for c in constraints if c.polarity.value == "required"]
        forbidden = [c for c in constraints if c.polarity.value == "forbidden"]
        any_of = [c for c in constraints if c.polarity.value == "any_of"]

        # ``forbidden`` is a global invariant. A single offending hop on
        # any returned path fails the check, even if other paths satisfy
        # ``required``. ``required`` and ``any_of`` keep existence
        # semantics: at least one path must include all required hops and
        # at least one any_of option.
        forbidden_hits: list[str] = []
        requirement_violations: list[str] = []
        valid_count = 0
        for path in paths:
            hops = [hop.node for hop in path.hops]
            trail = " → ".join(hop.display_label for hop in hops)
            for c in forbidden:
                if any(self._hop_matches(hop, c, attr_values) for hop in hops):
                    forbidden_hits.append(f"[{trail}]: forbidden {self._describe(c)} present")

            problems: list[str] = []
            for c in required:
                if not any(self._hop_matches(hop, c, attr_values) for hop in hops):
                    problems.append(f"missing required {self._describe(c)}")
            if any_of and not any(
                self._hop_matches(hop, c, attr_values) for hop in hops for c in any_of
            ):
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
                f"(cap: max_paths={rule.max_paths.value}).{url_block}"
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

    def _hop_matches(self, hop_node: Any, constraint: Any, attr_values: dict) -> bool:
        if hop_node.kind != constraint.hop_kind.value:
            return False
        attribute_name = constraint.attribute_name.value
        if attribute_name is None:
            return True
        actual = attr_values.get((constraint.hop_kind.value, attribute_name, hop_node.id))
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
        paths: list[Path],
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
                hop.node.id for path in paths for hop in path.hops if hop.node.kind == kind
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
