# Genuine Peering Reachability (via 1.10.1 all-paths mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `atl-to-jfk` / `atl-to-dfw` reachability rules enforce genuine peering through AS64496 (sever the peerings → RED), instead of passing via the shared-AS co-membership shortcut.

**Architecture:** Upgrade the server 1.10.0 → 1.10.1 (which exposes `shortest_paths_only` on `PathTraversalInput`). Switch the check to request **all loopless paths** (`shortest_paths_only: false`) by issuing the `InfrahubPathTraversal` GraphQL directly (the pinned SDK 1.22.0 `traverse_paths` cannot send the flag). Then make `atl1-edge1` a customer edge (AS65001) that reaches the Duff backbone only by peering, so the AS64496 hop appears only on genuine peering paths — which all-paths mode now reliably returns.

**Tech Stack:** Infrahub 1.10.1 (Docker), infrahub-sdk[ctl]==1.22.0 (unchanged), Python check (`checks/path_assertion.py`), GraphQL (`queries/path_check.gql`), YAML object data.

## Global Constraints

- Infrahub server pinned to **1.10.1** (patch upgrade from 1.10.0); persisted Neo4j/Postgres volumes are retained across the restart.
- SDK stays **infrahub-sdk[ctl]==1.22.0** — do NOT pin an unreleased SDK. The check requests all-paths mode via raw GraphQL, not the SDK's `traverse_paths`.
- All three demo scenarios must still behave: Sofia (happy path) GREEN, Chloe (reroute via Colt) RED on forbidden AS8220, Admin (max_depth 3→1) RED "no path within depth 1".
- Rule `max_depth` stays **3**, `max_paths` stays **50**.
- No Claude/Anthropic attribution in any commit.
- Before editing `checks/**/*.py`: read `infrahub-managing-checks` SKILL.md. Before editing any `.gql`: read `infrahub-managing-transforms` rules/queries-union-fragments.md and `infrahub-common/rules/deployment-gql-dry-run.md`.
- Detect the Python env once (`uv run` prefix confirmed for this repo) and reuse it. Export `INFRAHUB_ADDRESS=http://localhost:8000` and `INFRAHUB_API_TOKEN=<INFRAHUB_INITIAL_ADMIN_TOKEN from .env>` for infrahubctl/SDK commands.

---

### Task 1: Upgrade the stack to 1.10.1 and restart

**Files:**
- Modify: `.env` (`VERSION=1.10.0` → `VERSION=1.10.1`)
- Modify: `docker-compose.yml` (default version fallbacks `${VERSION:-1.10.0}` → `${VERSION:-1.10.1}`)

**Interfaces:**
- Produces: a running Infrahub 1.10.1 with the existing demo data intact.

- [ ] **Step 1: Bump the version in `.env`**

Change the first line:
```
VERSION=1.10.1
```

- [ ] **Step 2: Bump the fallback default in `docker-compose.yml`**

Replace every `${VERSION:-1.10.0}` with `${VERSION:-1.10.1}` (lines ~246, ~279, ~325).

- [ ] **Step 3: Pull the new images and restart, retaining volumes**

Run:
```bash
docker compose pull
docker compose up -d
```
Expected: containers recreated on the 1.10.1 image; volumes (Neo4j, Postgres) retained. Do NOT run `docker compose down -v`.

- [ ] **Step 4: Wait for health and confirm version**

Run: `uv run infrahubctl info`
Expected: `Connection Status: ✅` and `Infrahub Version: 1.10.1`. If migrations run on boot, wait until the API answers (retry `infrahubctl info` until ✅).

- [ ] **Step 5: Confirm the API now exposes `shortest_paths_only`**

Run:
```bash
TOKEN=$(grep INFRAHUB_INITIAL_ADMIN_TOKEN .env | cut -d= -f2 | tr -d ' ')
curl -s -X POST http://localhost:8000/graphql -H "Content-Type: application/json" -H "X-INFRAHUB-KEY: $TOKEN" \
  -d '{"query":"query { __type(name:\"PathTraversalInput\"){ inputFields { name } } }"}' | python3 -m json.tool
```
Expected: the `inputFields` list includes `shortest_paths_only`. **GATE:** if absent, stop — the upgrade did not take.

- [ ] **Step 6: Confirm the existing demo data survived the upgrade**

Run:
```bash
curl -s -X POST http://localhost:8000/graphql -H "Content-Type: application/json" -H "X-INFRAHUB-KEY: $TOKEN" \
  -d '{"query":"query { InfraDevice{count} TopologyReachabilityRule{count} }"}'
```
Expected: `InfraDevice.count == 3`, `TopologyReachabilityRule.count == 2`.

- [ ] **Step 7: Commit**

```bash
git add .env docker-compose.yml
git commit -m "Upgrade Infrahub stack to 1.10.1 for path-traversal all-paths mode"
```

---

### Task 2: Verify all-paths mode returns the longer AS paths (GATE — no code changes)

**Files:** none (verification only).

**Interfaces:**
- Consumes: running 1.10.1 from Task 1, current (unchanged) topology where all edge devices are in AS64496.
- Produces: empirical confirmation that `shortest_paths_only: false` returns the depth-3 `atl1 → session → AS64496 → dfw1` path that shortest-mode omitted.

- [ ] **Step 1: Query `atl→dfw` in all-paths mode on current `main`**

Run (resolve ids first, then traverse):
```bash
TOKEN=$(grep INFRAHUB_INITIAL_ADMIN_TOKEN .env | cut -d= -f2 | tr -d ' ')
H=(-H "Content-Type: application/json" -H "X-INFRAHUB-KEY: $TOKEN")
curl -s -X POST http://localhost:8000/graphql "${H[@]}" -d '{"query":"query { atl:InfraDevice(name__value:\"atl1-edge1\"){edges{node{id}}} dfw:InfraDevice(name__value:\"dfw1-edge1\"){edges{node{id}}} }"}' > /tmp/ids.json
ATL=$(python3 -c 'import json;print(json.load(open("/tmp/ids.json"))["data"]["atl"]["edges"][0]["node"]["id"])')
DFW=$(python3 -c 'import json;print(json.load(open("/tmp/ids.json"))["data"]["dfw"]["edges"][0]["node"]["id"])')
curl -s -X POST http://localhost:8000/graphql "${H[@]}" -d "{\"query\":\"query { InfrahubPathTraversal(data:{source_id:\\\"$ATL\\\", destination_id:\\\"$DFW\\\", max_depth:3, max_paths:50, shortest_paths_only:false, excluded_kinds:[\\\"TopologyReachabilityRule\\\",\\\"TopologyReachabilityConstraint\\\"]}){ count paths { depth hops { node { display_label } } } } }\"}" | python3 -c 'import sys,json; r=json.load(sys.stdin)["data"]["InfrahubPathTraversal"]; print("count",r["count"]); [print("  d%s: %s"%(p["depth"]," -> ".join(h["node"]["display_label"] for h in p["hops"]))) for p in r["paths"]]'
```
Expected: **more than one path**, including a depth-3 path containing `AS64496 (Duff)` (e.g. `atl1-edge1 -> atl1-edge1 ↔ jfk1-edge1 -> AS64496 (Duff) -> dfw1-edge1`).
**GATE:** if all-paths mode still omits every AS64496-transiting path for `atl→dfw`, stop and reconsider — the premise of the fix fails. (Contrast: the same query with `shortest_paths_only:true` returns only the direct depth-2 path.)

---

### Task 2.5: Fix InfraBGPSession identity so both atl1↔jfk1 sessions persist (schema)

**Discovered during execution.** `InfraBGPSession.human_friendly_id` is `[local_device__name__value, remote_device__name__value]`, so the two `atl1↔jfk1` sessions (AS64496 and AS8220) collide on HFID and `infrahubctl object load` collapses them into one (last-loaded = Colt/8220). The genuine-peering approach requires the `atl1↔jfk1` AS64496 peering to exist, so the session identity must include `remote_as`.

**Files:**
- Modify: `demo-seed/schemas/network.yml` (BGPSession node)

**Interfaces:**
- Produces: `InfraBGPSession` uniquely identified by `(local_device, remote_device, remote_as)`; all 4 seed sessions persist on `main`.

- [ ] **Step 1: Read the schema skill** — read `infrahub-managing-schemas` SKILL.md (+ reference.md for `human_friendly_id` / `uniqueness_constraints`) before editing.

- [ ] **Step 2: Update the BGPSession identity** — in `demo-seed/schemas/network.yml`, on the `BGPSession` node, change:
```yaml
    human_friendly_id: ["local_device__name__value", "remote_device__name__value", "remote_as__asn__value"]
```
and add (at node level):
```yaml
    uniqueness_constraints:
      - ["local_device", "remote_device", "remote_as"]
```

- [ ] **Step 3: Validate + load the schema**
```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=$(grep INFRAHUB_INITIAL_ADMIN_TOKEN .env | cut -d= -f2 | tr -d ' ')
uv run infrahubctl schema check demo-seed/schemas/network.yml
uv run infrahubctl schema load demo-seed/schemas/network.yml
```
Expected: schema check passes; load succeeds (HFID change is computed — no data migration).

- [ ] **Step 4: Re-seed the sessions so the missing AS64496 peering is created**
```bash
uv run infrahubctl object load demo-seed/data/03-bgp-sessions.yml
```
Expected: the `atl1↔jfk1` AS64496 session is created (new HFID `[atl1-edge1, jfk1-edge1, 64496]`); the Colt session is upserted.

- [ ] **Step 5: Verify all 4 sessions exist**
```bash
curl -s -X POST http://localhost:8000/graphql -H "Content-Type: application/json" -H "X-INFRAHUB-KEY: $INFRAHUB_API_TOKEN" -d '{"query":"query { InfraBGPSession { count edges { node { local_device{node{name{value}}} remote_device{node{name{value}}} remote_as{node{asn{value}}} } } } }"}' | python3 -m json.tool
```
Expected: `count == 4`, including `atl1↔jfk1` with `remote_as 64496` AND `atl1↔jfk1` with `remote_as 8220`.

- [ ] **Step 6: Commit**
```bash
git add demo-seed/schemas/network.yml
git commit -m "Schema: make InfraBGPSession identity include remote_as so parallel sessions coexist"
```

---

### Task 3: Switch the check to all-paths mode (raw GraphQL)

**Files:**
- Modify: `queries/path_check.gql`
- Modify: `checks/path_assertion.py`

**Interfaces:**
- Consumes: 1.10.1 API with `shortest_paths_only`.
- Produces: `PathAssertionCheck` evaluates constraints over **all loopless paths** returned by `InfrahubPathTraversal(..., shortest_paths_only: false)`. `collect_data` stores the raw traversal dict on `self._traversal`; `validate` reads `self._traversal["paths"]` (list of `{hops: [{node: {id, kind, display_label}}], depth}`), `self._traversal["source"|"destination"]["display_label"]`. Helpers `_hop_matches(node_dict, constraint, attr_values)` and `_fetch_attributes(paths, constraints)` consume node **dicts** (`node["kind"]`, `node["id"]`), keyed `(kind, attribute_name, node_id)` as before.

- [ ] **Step 1: Read the governing skills**

Read `infrahub-managing-checks` SKILL.md, `infrahub-managing-transforms` rules/queries-union-fragments.md, and `infrahub-common/rules/deployment-gql-dry-run.md` before editing. Create a todo per rule if they add steps.

- [ ] **Step 2: Add `shortest_paths_only: false` to the stored query**

In `queries/path_check.gql`, inside the `InfrahubPathTraversal(data: {...})` block, add the line:
```graphql
      shortest_paths_only: false
```
(Placed alongside `max_paths` / `excluded_kinds`.) Leave the selection set as-is (it already selects `hops { node { id kind hfid display_label } } depth`, `source/destination { id display_label }`, `count`).

- [ ] **Step 3: Add the query constant and switch `collect_data` to execute it**

In `checks/path_assertion.py`, add a module-level constant (mirrors the stored query, with the all-paths flag — consistent with the existing "check builds its own call" design):
```python
PATH_TRAVERSAL_QUERY = """
query PathCheck($source_id: String!, $destination_id: String!, $max_depth: Int, $max_paths: Int) {
  InfrahubPathTraversal(
    data: {
      source_id: $source_id
      destination_id: $destination_id
      max_depth: $max_depth
      max_paths: $max_paths
      shortest_paths_only: false
      excluded_kinds: ["TopologyReachabilityRule", "TopologyReachabilityConstraint"]
    }
  ) {
    paths { hops { node { id kind display_label } } depth }
    source { id display_label }
    destination { id display_label }
    count
  }
}
"""
```
Replace the `self._traversal_result = await self.client.traverse_paths(...)` block in `collect_data` with:
```python
        response = await self.client.execute_graphql(
            query=PATH_TRAVERSAL_QUERY,
            variables={
                "source_id": source_id,
                "destination_id": destination_id,
                "max_depth": _coerce_int(rule.max_depth.value),
                "max_paths": _coerce_int(rule.max_paths.value),
            },
            branch_name=self.branch_name,
            tracker="reachability-check-path-traversal",
            timeout=CHECK_REQUEST_TIMEOUT,
        )
        self._traversal = (response or {}).get("InfrahubPathTraversal") or {}
        return {"_ok": True}
```
Replace the class attribute `_traversal_result: PathTraversalResult | None = None` with `_traversal: dict | None = None`. Remove the now-unused `from infrahub_sdk.graph_traversal import Path, PathTraversalResult` import.

- [ ] **Step 4: Update `validate` to read the traversal dict**

In `validate`, replace the result-object block with:
```python
        result = self._traversal or {}
        paths = result.get("paths") or []
        source_label = (result.get("source") or {}).get("display_label") or getattr(
            getattr(rule, "source", None), "display_label", None
        )
        dest_label = (result.get("destination") or {}).get("display_label") or getattr(
            getattr(rule, "destination", None), "display_label", None
        )
        max_depth = rule.max_depth.value
```
Replace the per-path scoring loop's hop extraction:
```python
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
```

- [ ] **Step 5: Update `_hop_matches` and `_fetch_attributes` for dict nodes**

`_hop_matches` — change node attribute access to dict keys:
```python
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
```
`_fetch_attributes` — change the hop iteration to dict access:
```python
        ids_by_kind: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            for hop in path.get("hops") or []:
                node = hop["node"]
                if node["kind"] in needed:
                    ids_by_kind[node["kind"]].add(node["id"])
```

- [ ] **Step 6: Dry-run validate the query change**

Per `deployment-gql-dry-run.md`, validate the `.gql` compiles against the server:
```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=$(grep INFRAHUB_INITIAL_ADMIN_TOKEN .env | cut -d= -f2 | tr -d ' ')
uv run infrahubctl query path_check 2>&1 | tail -20
```
Expected: the query executes without a GraphQL validation error (a "variables required" style prompt is fine; a "Cannot query field / unknown argument shortest_paths_only" is a FAIL).

- [ ] **Step 7: Commit**

```bash
git add queries/path_check.gql checks/path_assertion.py
git commit -m "Reachability check: evaluate over all loopless paths (shortest_paths_only=false)"
```

---

### Task 4: Apply the Approach-A topology (atl1 as customer edge)

**Files:**
- Modify: `demo-seed/data/01-asns.yml`
- Modify: `demo-seed/data/02-devices.yml`
- Modify: `demo-seed/data/03-bgp-sessions.yml`
- Modify: `demo-seed/setup.py`

**Interfaces:**
- Consumes: all-paths-mode check from Task 3.
- Produces: `atl1-edge1.asn = AS65001` (customer), jfk1/dfw1 in AS64496; new `Peachtree Metro` AS.

- [ ] **Step 1: Add the customer AS** — in `demo-seed/data/01-asns.yml`, append:
```yaml
    - name: Peachtree Metro
      asn: 65001
      description: "Atlanta edge customer AS — peers into the Duff backbone (AS64496)."
```

- [ ] **Step 2: Repoint atl1** — in `demo-seed/data/02-devices.yml`, change `atl1-edge1`'s `asn: "64496"` to `asn: "65001"` and add a clarifying comment above the `data:` entries:
```yaml
    # atl1 is a customer edge in its own AS (AS65001). It reaches the
    # Duff backbone only by peering, so "transit AS64496" depends on a
    # real BGP session, not shared-AS co-membership.
```

- [ ] **Step 3: Refresh the session comments** — in `demo-seed/data/03-bgp-sessions.yml`, update the header comment and the Colt comment to describe atl1 as a customer peering into AS64496 and that AS8220 is a dormant leaf on `main` (see spec `2026-07-02-genuine-peering-reachability-design.md` §Changes).

- [ ] **Step 4: Update the Chloe scenario text** — in `demo-seed/setup.py`, change the Chloe `pc_description` `atl1-edge1.asn AS64496 -> AS8220` to `AS65001 -> AS8220`.

- [ ] **Step 5: Validate the object files**
```bash
uv run infrahubctl object validate demo-seed/data/01-asns.yml
uv run infrahubctl object validate demo-seed/data/02-devices.yml
```
Expected: all documents `Valid!`.

- [ ] **Step 6: Commit**
```bash
git add demo-seed/data/01-asns.yml demo-seed/data/02-devices.yml demo-seed/data/03-bgp-sessions.yml demo-seed/setup.py
git commit -m "Demo topology: atl1 becomes a customer edge peering into AS64496"
```

---

### Task 5: Load, re-sync the check, and verify all behaviours end-to-end

**Files:** none (data load + verification).

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: verified GREEN/RED verdicts matching the demo intent, now peering-dependent.

- [ ] **Step 1: Load the updated data onto `main`**
```bash
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN=$(grep INFRAHUB_INITIAL_ADMIN_TOKEN .env | cut -d= -f2 | tr -d ' ')
uv run infrahubctl object load demo-seed/data/01-asns.yml
uv run infrahubctl object load demo-seed/data/02-devices.yml
```
Expected: nodes upserted; `atl1-edge1.asn` resolves to `AS65001` (verify with a quick GraphQL query, `InfraDevice(name__value:"atl1-edge1"){edges{node{asn{node{asn{value}}}}}}` → 65001).

- [ ] **Step 2: Re-sync the CoreRepository so the updated check + query install**
```bash
uv run invoke demo.register-repo
```
Expected: repo sync succeeds; the `reachability_assertion` CoreCheckDefinition and `path_check` query update to the new commit. (Requires the changes to be committed — Tasks 3–4 committed them.)

- [ ] **Step 3: Verify `main` — both rules GREEN via a genuine AS64496 path**

Use the all-paths traversal (as in Task 2 Step 1) for `atl→jfk` and `atl→dfw` with `shortest_paths_only:false, max_depth:3`. For each, assert: at least one returned path contains `AS64496 (Duff)`, and none contains `AS8220`.
Expected: both have ≥1 AS64496-transiting path → GREEN. (The co-membership `atl1 → AS64496 → dst` depth-2 path is absent.)

- [ ] **Step 4: Verify the Chloe scenario — RED on forbidden AS8220**

On a throwaway branch, repoint `atl1-edge1.asn → AS8220`, traverse `atl→jfk` (`shortest_paths_only:false, max_depth:3`), assert a returned path transits `AS8220 (Colt)`. Delete the branch.
Expected: forbidden hit present → RED.

- [ ] **Step 5: Verify the sever scenario — RED when peerings removed (the whole point)**

On a throwaway branch, delete BOTH atl1 sessions whose `remote_as` is AS64496 (query `InfraBGPSession(remote_as__asn__value:64496)`, keep those where atl1-edge1 is local or remote device — expect 2 ids), traverse `atl→jfk` (`shortest_paths_only:false, max_depth:3`), assert **no** returned path transits `AS64496`. Delete the branch.
Expected: no AS64496 path → RED. This is the genuine-peering proof.

- [ ] **Step 6: Verify the Admin scenario unaffected — RED "no path within depth 1"**

Traverse `atl→jfk` with `max_depth:1`. Expected: 0 paths (→ the check logs "No path within depth 1").

- [ ] **Step 7: (If reachable) run the check via the pipeline**

Open/refresh the three preloaded proposed changes (or `uv run invoke demo.init --phase scenarios` if branches were reset) and confirm the verdict cards: Sofia ✅✅, Chloe ❌ (forbidden AS8220) + ✅ (dfw), Admin ❌ (no path within depth 1). If the `infrahubctl check` group-lookup path is used, note any runner quirk but rely on the traversal-based verification above as the source of truth.

---

### Task 6: Update docs and the spec

**Files:**
- Modify: `QUICKSTART.md`
- Modify: `README.md` (only if any prose asserts co-membership — verify first)
- Modify: `docs/superpowers/specs/2026-07-02-genuine-peering-reachability-design.md`

**Interfaces:**
- Consumes: verified behaviour + real verdict counts from Task 5.
- Produces: docs consistent with the new topology and verdict output.

- [ ] **Step 1: Update the ASN inventory** — `QUICKSTART.md` line ~38 (`3 ASNs` → `4 ASNs`) and the object-count table row (`InfraAutonomousSystem | 4 | AS64496 (Duff), AS8220 (Colt), AS701, AS65001 (Peachtree Metro)`).

- [ ] **Step 2: Update the Chloe step** — `QUICKSTART.md` line ~112: `atl1-edge1.asn reassigned from AS64496 to AS8220` → `from AS65001 to AS8220`.

- [ ] **Step 3: Update sample verdict counts** — re-run the Task 5 Step 3 traversals, count `valid/total` (paths transiting AS64496 / total paths), and replace the `N/N paths satisfy all constraints` sample lines (~105–106) with the measured values (they will differ from `3/3`).

- [ ] **Step 4: Add a short "How enforcement works" note** — one paragraph in `QUICKSTART.md` or `README.md` Step 5 area: atl1 is a customer edge; the rule passes only because atl1 peers into AS64496; sever the peerings and it goes red; the check reads all loopless paths (`shortest_paths_only=false`, needs Infrahub ≥1.10.1).

- [ ] **Step 5: Update the spec status** — in `docs/superpowers/specs/2026-07-02-genuine-peering-reachability-design.md`, change Status from BLOCKED to `Implemented (via 1.10.1 all-paths mode)` and add a one-line pointer to this plan.

- [ ] **Step 6: Commit**
```bash
git add QUICKSTART.md README.md docs/superpowers/specs/2026-07-02-genuine-peering-reachability-design.md
git commit -m "Docs: reflect customer-edge topology and all-paths reachability enforcement"
```

---

## Notes / risk register

- **Server upgrade migration:** 1.10.0 → 1.10.1 is a patch; migrations (if any) run on boot and retain volumes. Task 1 Step 6 guards against data loss.
- **All-paths premise:** Task 2 is a hard gate — if `shortest_paths_only:false` does not surface the `atl→dfw` AS path, the whole approach is invalid; stop there.
- **Check runs from the registered repo commit** in the pipeline, so Tasks 3–4 must be committed before Task 5 Step 2 (`register-repo`). Local `infrahubctl` verification uses the working-tree files.
- **Rollback:** revert the commits and set `.env` back to `VERSION=1.10.0`, `docker compose up -d`. The live-data restore procedure (repoint `atl1.asn`, delete AS65001) is recorded in the conversation if needed before a full rollback.
