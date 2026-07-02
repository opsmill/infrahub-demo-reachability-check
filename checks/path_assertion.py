"""Reachability path-assertion check (SDK 1.22 edition).

Drives the path traversal by issuing the ``InfrahubPathTraversal``
GraphQL query directly (``client.execute_graphql`` with
``PATH_TRAVERSAL_QUERY``), because it needs ``shortest_paths_only:
false`` — all loopless paths, not just the shortest-through-each-
intermediate subset — and the SDK 1.22 ``traverse_paths`` helper
cannot send that flag. Requires Infrahub 1.10.1 or later (the release
that exposed ``shortest_paths_only`` on ``PathTraversalInput``). The
stored ``path_check`` query stays registered because ``InfrahubCheck``
requires ``query`` to be set; the check runs its own equivalent query
and consumes the raw response dict.

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

Production tuning
-----------------
Every outbound SDK call carries an explicit ``timeout`` so a slow
database cannot tie up a check worker indefinitely, and a ``tracker``
identifier so the call shows up in the Prefect / Infrahub run tree
under a meaningful name (look for ``reachability-check-*`` in worker
logs). One outbound call per concern:

  ``client.get``              - load the rule + constraints
  ``client.execute_graphql``  - run the path traversal (all loopless paths)
  ``client.execute_graphql``  - batched attribute fetch across all hop kinds

The check therefore costs three network round-trips per rule
regardless of how many constraint kinds the rule references.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck
from infrahub_sdk.node import InfrahubNode

# Tunable per-call timeout for every SDK round-trip the check makes.
# Override via the ``INFRAHUB_REACHABILITY_CHECK_TIMEOUT`` env var if
# your environment needs a different budget; the default of 60s is
# generous for the default depth=8 / max_paths=50 traversal.
import os

CHECK_REQUEST_TIMEOUT: int = int(os.environ.get("INFRAHUB_REACHABILITY_CHECK_TIMEOUT", "60"))

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

# The check issues this query directly (via ``execute_graphql``) instead of
# the SDK's ``traverse_paths`` helper, because ``traverse_paths`` in SDK 1.22
# cannot send ``shortest_paths_only``. All-paths mode (``false``) returns every
# loopless path up to ``max_paths`` — not just the shortest-through-each-
# intermediate subset — so a rule is judged on genuine peering paths rather
# than only a co-membership shortcut. ``shortest_paths_only`` on
# ``PathTraversalInput`` requires Infrahub >= 1.10.1. Mirrors the registered
# ``queries/path_check.gql``; ``excluded_kinds`` rides as a variable so
# ``EXCLUDED_KINDS`` above stays the single source of truth.
PATH_TRAVERSAL_QUERY = """
query PathCheck(
  $source_id: String!
  $destination_id: String!
  $max_depth: Int
  $max_paths: Int
  $excluded_kinds: [String!]
) {
  InfrahubPathTraversal(
    data: {
      source_id: $source_id
      destination_id: $destination_id
      max_depth: $max_depth
      max_paths: $max_paths
      shortest_paths_only: false
      excluded_kinds: $excluded_kinds
    }
  ) {
    paths { hops { node { id kind display_label } } depth }
    source { id display_label }
    destination { id display_label }
    count
  }
}
"""


def _is_disabled(value: Any) -> bool:
    """Return ``True`` when a rule's ``enabled`` flag should be treated as disabled.

    The ``enabled`` field on the rule is a Boolean attribute, so the
    value is normally a Python ``bool``. The same check is also
    invokable through ``infrahubctl check rule_id=... enabled=...``,
    which passes parameters as strings. This helper accepts both
    forms: ``False``, the literal strings ``"false"`` / ``"no"`` /
    ``"0"`` / ``"off"`` (case-insensitive, with surrounding
    whitespace stripped) all mean disabled. Everything else, including
    a missing ``None`` value, means enabled.
    """
    if value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in FALSEY_STRINGS:
        return True
    return False


def _normalize(value: Any) -> str:
    """Stringify a value the same way on both sides of an attribute comparison.

    Constraints store ``attribute_value`` as text (the schema's
    ``attribute_value`` field is ``kind: Text``). Hop attribute values
    fetched from the graph come back with their native types - bool,
    int, str, etc. To compare apples to apples, both sides are passed
    through this helper before equality: booleans render as lowercase
    ``"true"`` / ``"false"`` to match the text the user typed; every
    other type uses ``str()`` as-is.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as an ``int`` if convertible, else ``None``.

    Used to massage the rule's ``max_depth`` and ``max_paths`` values
    before they are sent as traversal query variables. The server
    accepts ``None`` (and falls back to its own defaults) but reasonably
    rejects Boolean / non-numeric inputs, so any unconvertible value is
    mapped to ``None`` rather than raising mid-check.
    """
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
    """Per-rule check: assert that the rule's path predicates hold on this branch.

    Registered as a ``CoreCheckDefinition`` with
    ``targets: reachability-rules`` and parameters
    ``rule_id: "id"`` in ``.infrahub.yml``. The Infrahub check runner
    fans the check out one invocation per ``TopologyReachabilityRule``
    member of the group, passing only the rule id into
    ``self.params``. The check then loads the rule, runs the path
    traversal, fetches the hop attributes the constraints reference,
    and writes one log entry per finding plus a summary line.
    """

    # ``query`` is required by ``InfrahubCheck.__init__`` and the
    # ``CoreCheckDefinition`` repo-sync flow. A stored ``path_check``
    # query is registered in ``.infrahub.yml`` so the check passes
    # validation, but ``collect_data`` does not execute it; it runs
    # ``PATH_TRAVERSAL_QUERY`` (with ``shortest_paths_only: false``) instead.
    query = "path_check"
    timeout = 120

    # The check fetches the rule once in collect_data and stashes both
    # the rule and the traversal result so validate() does not have to
    # round-trip again.
    _rule: Any | None = None
    _traversal: dict | None = None

    async def collect_data(self) -> dict:
        """Load the rule and run the path traversal.

        Returns a small ``dict`` whose keys act as sentinels for
        ``validate``. A successful run returns ``{"_ok": True}`` and
        leaves the loaded rule on ``self._rule`` and the traversal
        result dict on ``self._traversal`` for ``validate`` to read.
        Early-exit sentinels:

          ``_no_rule_id``       - the runner did not pass ``rule_id``.
          ``_disabled``         - the rule's ``enabled`` flag is off.
          ``_missing_endpoints`` - source or destination is null on the rule.
          ``_self_loop``        - source and destination resolve to the same node.

        Each sentinel maps one-to-one to a branch in ``validate``.
        """
        rule_id = self.params.get("rule_id")
        if not rule_id:
            return {"_no_rule_id": True}

        rule = await self.client.get(
            kind="TopologyReachabilityRule",
            id=rule_id,
            branch=self.branch_name,
            include=["constraints"],
            prefetch_relationships=True,
            timeout=CHECK_REQUEST_TIMEOUT,
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

        response = await self.client.execute_graphql(
            query=PATH_TRAVERSAL_QUERY,
            variables={
                "source_id": source_id,
                "destination_id": destination_id,
                "max_depth": _coerce_int(rule.max_depth.value),
                "max_paths": _coerce_int(rule.max_paths.value),
                "excluded_kinds": list(EXCLUDED_KINDS),
            },
            branch_name=self.branch_name,
            tracker="reachability-check-path-traversal",
            timeout=CHECK_REQUEST_TIMEOUT,
        )
        self._traversal = (response or {}).get("InfrahubPathTraversal") or {}
        return {"_ok": True}

    async def validate(self, data: dict) -> None:
        """Evaluate the constraints against the paths and emit verdict log entries.

        ``data`` is the dict returned by ``collect_data``. Early-exit
        sentinels are handled first (rule id missing, rule disabled,
        endpoints missing, self-loop). Otherwise the constraints are
        partitioned into ``required`` and ``forbidden`` and each
        returned path is scored:

          - any path whose hops include a ``forbidden`` match fails the
            check immediately (``forbidden`` is a global invariant);
          - if at least one path satisfies every ``required`` hop, the
            rule passes; otherwise per-path requirement violations are
            emitted.

        Verdict cards in the Infrahub UI aggregate ``log_info`` and
        ``log_error`` entries from this method.
        """
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

        result = self._traversal or {}
        paths = result.get("paths") or []
        source_label = (result.get("source") or {}).get("display_label") or getattr(
            getattr(rule, "source", None), "display_label", None
        )
        dest_label = (result.get("destination") or {}).get("display_label") or getattr(
            getattr(rule, "destination", None), "display_label", None
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
            hops = [hop["node"] for hop in path.get("hops") or []]
            trail = " → ".join(node["display_label"] for node in hops)
            for c in forbidden:
                if any(self._hop_matches(node, c, attr_values) for node in hops):
                    forbidden_hits.append(f"[{trail}]: forbidden {self._describe(c)} present")

            problems: list[str] = []
            for c in required:
                if not any(self._hop_matches(node, c, attr_values) for node in hops):
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
        """Surface authoring mistakes that would silently turn constraints into no-ops.

        A constraint with an unknown polarity, an empty ``hop_kind``,
        or an ``attribute_name`` set without an ``attribute_value``
        would match nothing at evaluation time and the operator would
        wonder why the rule appeared to pass. This helper logs each
        such mistake as an error so the verdict card calls it out
        explicitly. Returns ``False`` when at least one mistake was
        found so ``validate`` aborts before attempting evaluation.
        """
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

    def _hop_matches(self, hop_node: dict, constraint: Any, attr_values: dict) -> bool:
        """Return ``True`` if a single hop satisfies a single constraint.

        ``hop_node`` is the GraphQL node dict (``{"id", "kind",
        "display_label"}``). A constraint matches a hop when the hop's
        schema kind equals ``constraint.hop_kind``. If the constraint
        also names an ``attribute_name``, the hop node's value for that
        attribute (looked up in the prefetched ``attr_values`` table)
        must equal ``constraint.attribute_value`` after both sides are
        passed through ``_normalize``.
        """
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
        """Render a constraint as a short, human-readable phrase.

        Used to compose verdict messages such as
        "forbidden hop of kind InfraAutonomousSystem with asn=8220 present".
        When ``attribute_name`` is unset the description collapses to
        "hop of kind <kind>".
        """
        attribute_name = constraint.attribute_name.value
        if attribute_name is None:
            return f"hop of kind {constraint.hop_kind.value}"
        return (
            f"hop of kind {constraint.hop_kind.value} "
            f"with {attribute_name}={constraint.attribute_value.value}"
        )

    async def _fetch_attributes(
        self,
        paths: list,
        constraints: list[Any],
    ) -> dict[tuple[str, str, str], Any]:
        """Fetch every constraint-relevant hop attribute in one GraphQL call.

        For each constraint whose ``attribute_name`` is set, the check
        needs the value of that attribute on every returned hop of the
        constraint's ``hop_kind``. A naive implementation would issue
        one ``client.filters`` call per distinct hop kind and run them
        concurrently. Production deployments fan the check out across
        many rules per proposed change, so this implementation
        consolidates all kinds into a single GraphQL operation with one
        aliased block per kind, drives it through
        ``InfrahubClient.execute_graphql``, and hydrates each result
        through ``InfrahubNode.from_graphql`` for typed attribute
        access. Hop ids ride as bound ``[ID]`` variables; kind and
        attribute names come from the schema and are
        GraphQL-identifier-safe by construction.

        Returns a mapping ``(kind, attribute_name, node_id) -> value``
        that ``_hop_matches`` looks up by tuple.
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
            for hop in path.get("hops") or []:
                node = hop["node"]
                if node["kind"] in needed:
                    ids_by_kind[node["kind"]].add(node["id"])
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
            timeout=CHECK_REQUEST_TIMEOUT,
        )

        out: dict[tuple[str, str, str], Any] = {}
        for index, kind in enumerate(kinds_ordered):
            block = (response or {}).get(f"k{index}") or {}
            edges = block.get("edges") or []
            for edge in edges:
                node = await InfrahubNode.from_graphql(
                    client=self.client,
                    branch=self.branch_name,
                    data=edge,
                    timeout=CHECK_REQUEST_TIMEOUT,
                )
                for attribute_name in needed[kind]:
                    attribute = getattr(node, attribute_name, None)
                    if attribute is not None and getattr(attribute, "value", None) is not None:
                        out[(kind, attribute_name, node.id)] = attribute.value
        return out
