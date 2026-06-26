"""Reachability path-assertion check (SDK 1.22 edition).

Uses ``InfrahubClient.traverse_paths()`` (added in SDK 1.22, requires
Infrahub 1.10 or later) to drive the path traversal. The stored
``path_check`` query stays registered because ``InfrahubCheck``
requires ``query`` to be set, but the check never executes it; the
SDK helper builds its own GraphQL call and returns a typed
``PathTraversalResult``.

The check does not emit the click-through URL in its verdict log.
The rule already exposes ``path_traversal_url`` as a read-only,
URL-kind computed attribute (see ``transforms/path_traversal_url.py``),
which the Infrahub UI renders as a clickable hyperlink on the rule
detail page. Pasting a relative URL into a verdict log message would
not survive copy-paste outside the UI, so the verdict stays focused
on what failed and on which path; the navigation lives where it
belongs, on the rule itself.

Each member of the targeted group is a ``TopologyReachabilityRule``.
The runner extracts only the rule id from each member, and the check
fetches the rule together with its constraint children. Source,
destination, max_depth, max_paths, and enabled are then read directly
off the rule node.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from infrahub_sdk.graph_traversal import Path, PathTraversalResult
from infrahub_sdk.node import InfrahubNode

FALSEY_STRINGS = frozenset({"false", "no", "0", "off"})

# Kinds excluded from the traversal. Without this, the rule node itself
# (cardinality-one source and destination) becomes a one-hop shortcut
# between the endpoints and every reachability assertion collapses to a
# trivial "the rule connects them" path.
#
# Adopters whose schema has additional shortcut kinds (for example
# ``InfraPlatform`` in the standard ``models/base`` schemas, or a
# global ``Tag`` / ``Tenant`` / ``Vendor`` node that cardinality-many-
# relates to a large slice of the graph) should add those kinds to
# this tuple AND to the matching list in ``queries/path_check.gql``
# AND to the ``EXCLUDED_KINDS`` tuple in
# ``transforms/path_traversal_url.py``. The GraphQL server rejects
# ``excluded_kinds`` values that are not in the loaded schema, so the
# default list stays minimal; extend it in your fork.
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

        # Guard the relationship access: a cleared or unset
        # ``source`` / ``destination`` makes ``rule.source`` itself
        # ``None`` on the SDK node, so the ``.id`` attribute read would
        # raise ``AttributeError`` before we can return the cleaner
        # ``_missing_endpoints`` sentinel.
        source = getattr(rule, "source", None)
        destination = getattr(rule, "destination", None)
        source_id = getattr(source, "id", None) if source is not None else None
        destination_id = getattr(destination, "id", None) if destination is not None else None

        if not source_id or not destination_id:
            return {"_missing_endpoints": True}

        if source_id == destination_id:
            return {"_self_loop": True}

        self._traversal_result = await self.client.traverse_paths(
            source=source_id,
            destination=destination_id,
            max_depth=_coerce_int(rule.max_depth.value),
            max_paths=_coerce_int(rule.max_paths.value),
            excluded_kinds=list(EXCLUDED_KINDS),
            branch=self.branch_name,
        )
        return {"_ok": True}

    async def validate(self, data: dict) -> None:
        if data.get("_no_rule_id"):
            self.log_error(message="Rule id was not extracted into params; cannot load rule.")
            return

        if data.get("_disabled"):
            self.log_info(message="Rule is disabled; skipping evaluation.")
            return

        if data.get("_missing_endpoints"):
            self.log_error(
                message="Rule is missing source or destination; cannot evaluate.",
            )
            return

        if data.get("_self_loop"):
            self.log_error(
                message="Rule source and destination resolve to the same node; assertion is ill-defined.",
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

        if not paths:
            self.log_error(
                message=f"No path within depth {max_depth} between '{source_label}' and '{dest_label}'.",
            )
            return

        if not constraints:
            self.log_info(
                message=f"No constraints defined; reachability satisfied across {len(paths)} path(s).",
            )
            return

        if not self._validate_constraint_authoring(constraints):
            return

        attr_values = await self._fetch_attributes(paths=paths, constraints=constraints)

        required = [c for c in constraints if c.polarity.value == "required"]
        forbidden = [c for c in constraints if c.polarity.value == "forbidden"]

        # ``forbidden`` is a global invariant. A single offending hop on
        # any returned path fails the check, even if other paths satisfy
        # ``required``. ``required`` keeps existence semantics: at least
        # one path must include all required hops.
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

            if problems:
                requirement_violations.append(f"[{trail}]: {'; '.join(problems)}")
            else:
                valid_count += 1

        errors: list[str] = list(forbidden_hits)
        if required and valid_count == 0:
            errors.extend(requirement_violations)

        if errors:
            for line in errors:
                self.log_error(message=line)
            return

        self.log_info(
            message=(
                f"{valid_count}/{len(paths)} paths satisfy all constraints "
                f"(cap: max_paths={rule.max_paths.value})."
            ),
        )

    def _validate_constraint_authoring(self, constraints: list[Any]) -> bool:
        """Surface authoring mistakes that would silently make constraints no-ops."""
        ok = True
        for c in constraints:
            polarity = c.polarity.value
            if polarity not in {"required", "forbidden"}:
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
        """Fetch every constraint-relevant hop attribute in one GraphQL call.

        For each constraint whose ``attribute_name`` is set, the check
        needs the value of that attribute on every returned hop of the
        constraint's ``hop_kind``. The previous implementation issued one
        ``client.filters`` call per distinct hop kind and ran them
        concurrently. Production deployments fan the check out across
        many rules per proposed change, and at scale even concurrent
        per-kind round-trips dominate the check's wall-clock time.

        This implementation builds one GraphQL query with one aliased
        block per hop kind, drives it with ``InfrahubClient.execute_graphql``,
        and hydrates each result through ``InfrahubNode.from_graphql`` so
        the attribute values are read through the typed SDK node rather
        than off raw dictionaries. Hop ids ride as GraphQL variables;
        kind and attribute names come from the schema and are
        GraphQL-identifier-safe by construction (the schema would reject
        anything else at load time).
        """
        needed: dict[str, set[str]] = defaultdict(set)
        for constraint in constraints:
            attribute_name = constraint.attribute_name.value
            if attribute_name is not None:
                needed[constraint.hop_kind.value].add(attribute_name)
        if not needed:
            return {}

        ids_by_kind: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            for hop in path.hops:
                if hop.node.kind in needed:
                    ids_by_kind[hop.node.kind].add(hop.node.id)
        if not any(ids_by_kind.values()):
            return {}

        kinds_ordered: list[str] = [k for k in needed if ids_by_kind.get(k)]
        variables: dict[str, list[str]] = {}
        blocks: list[str] = []
        for index, kind in enumerate(kinds_ordered):
            var_name = f"ids_{index}"
            variables[var_name] = sorted(ids_by_kind[kind])
            attrs_selection = " ".join(
                f"{name} {{ value }}" for name in sorted(needed[kind])
            )
            blocks.append(
                f"k{index}: {kind}(ids: ${var_name}) "
                f"{{ edges {{ node {{ __typename id {attrs_selection} }} }} }}"
            )
        if not blocks:
            return {}

        declarations = ", ".join(f"${name}: [ID]" for name in variables)
        query = f"query FetchHopAttributes({declarations}) {{ {' '.join(blocks)} }}"

        response = await self.client.execute_graphql(
            query=query,
            variables=variables,
            branch_name=self.branch_name,
            tracker="reachability-check-fetch-hop-attributes",
        )

        out: dict[tuple[str, str, str], Any] = {}
        for index, kind in enumerate(kinds_ordered):
            block = (response or {}).get(f"k{index}") or {}
            edges = block.get("edges") or []
            for edge in edges:
                node = await InfrahubNode.from_graphql(
                    client=self.client, branch=self.branch_name, data=edge
                )
                for attribute_name in needed[kind]:
                    attribute = getattr(node, attribute_name, None)
                    if attribute is not None and getattr(attribute, "value", None) is not None:
                        out[(kind, attribute_name, node.id)] = attribute.value
        return out
