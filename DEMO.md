# Reachability Check: Live Demo

A self-contained walkthrough of the reachability-check pattern running
against Infrahub 1.10 and the Infrahub SDK 1.22. This branch contains
everything required to reproduce the recorded demo on a local
machine, with no dependencies on a hosted Infrahub instance.

Bundled in this branch:

- `docker-compose.yml` pinned to Infrahub `1.10.0`.
- `demo-seed/`, a small topology schema (three kinds) plus seed data
  (three devices, three ASNs, four BGP sessions) so the two rules
  have something meaningful to traverse.
- `checks/path_assertion.py`, rewritten against the Infrahub SDK 1.22
  `InfrahubClient.traverse_paths()` method.
- `transforms/path_traversal_url.py`, the Python computed-attribute
  transform that backs the rule's click-through URL.
- `tasks.py`, exposing `uv run invoke demo.start / demo.init / demo.up`
  to drive the stack.

## Quickstart

```bash
# 1. Bring up Infrahub 1.10 (database, workers, server). The healthcheck
#    waits up to 5 minutes for /api/config to return 200.
uv run invoke demo.start

# 2. Load the topology schema, seed devices, ASNs, and BGP sessions,
#    and create the two reachability rules with their constraints.
uv run invoke demo.init

# 3. Open the UI:
open http://localhost:8000
#    Admin token: 06438eb2-8019-4776-878c-0941b1f1d1ec
#    Or sign in as user: admin / infrahub
```

After step 2 the graph contains:

| Kind                              | Count | Examples                                       |
| --------------------------------- | ----: | ---------------------------------------------- |
| `InfraAutonomousSystem`           | 3     | AS64496 (Duff), AS8220 (Colt), AS701           |
| `InfraDevice`                     | 3     | atl1-edge1, jfk1-edge1, dfw1-edge1             |
| `InfraBGPSession`                 | 4     | full mesh on AS64496 plus atl1↔jfk1 on AS8220  |
| `TopologyReachabilityRule`        | 2     | atl-to-jfk-via-as64496, atl-to-dfw-via-as64496 |
| `TopologyReachabilityConstraint`  | 3     | "Must transit AS64496" ×2, "Never transit AS8220" ×1 |

## The two rules

```text
atl-to-jfk-via-as64496
  source:        atl1-edge1
  destination:   jfk1-edge1
  max_depth:     3
  max_paths:     50
  constraints:
    - Must transit:  InfraAutonomousSystem.asn = 64496   (Duff)
    - Never transit: InfraAutonomousSystem.asn = 8220    (Colt)

atl-to-dfw-via-as64496
  source:        atl1-edge1
  destination:   dfw1-edge1
  max_depth:     3
  max_paths:     50
  constraints:
    - Must transit:  InfraAutonomousSystem.asn = 64496
```

These match the deck. The Atlanta-to-NYC rule asserts that the path
must transit AS64496 and must never transit AS8220. The Atlanta-to-Dallas
rule asserts that the path must transit AS64496.

## Register this repository as a CoreRepository

The check fires inside the proposed-change pipeline once Infrahub
knows about this git repository. Push this branch to any git server
the Infrahub workers can reach, then either:

- In the UI, navigate to **Object Management → CoreRepository → + Add**,
  enter the URL, set the branch to `live-demo`, and leave credentials
  empty for public access.
- Or load a `CoreRepository` YAML through `infrahubctl object load`.

Once the repository is registered, `.infrahub.yml` is parsed on every
commit. The `path_check` query, the `path_traversal_url` Python
transform, the `reachability_assertion` check, the schema extension,
and the menu entry all appear automatically.

## The three scenarios (matching the demo deck, slide 15)

Each scenario in the recorded demo is a single-field change. Each
produces a different verdict. Reproduce them locally as follows.

### 01. PASS, Benign documentation tweak, by Sofia Hernandez (Network Engineering)

**The change.** `atl1-edge1.description` is updated. No topology change.

1. Create a branch named `shernandez-doc-tweak` with **Sync with Git** ON.
2. Open `InfraDevice` → `atl1-edge1` → **Edit**, set:

   ```
   description: documentation tweak: updated maintenance window
   ```

3. Open a proposed change named
   "Sofia Hernandez: atl1 documentation tweak", source branch
   `shernandez-doc-tweak`, destination `main`.
4. On the **Checks** tab, both rule cards turn green:

   ```text
   atl-to-jfk-via-as64496  ✅  3/3 paths satisfy all constraints (cap: max_paths=50)
   atl-to-dfw-via-as64496  ✅  3/3 paths satisfy all constraints (cap: max_paths=50)
   ```

   Both rules stay green: the AS64496 path is intact and no AS8220
   hop appears on any returned path.

### 02. FAIL, Reroute via Colt, by Chloe O'Brian (Network Engineering)

**The change.** `atl1-edge1.asn` is repointed from AS64496 (Duff) to
AS8220 (Colt). One field. No new objects.

1. Create a branch named `cobrian-reroute-via-colt`, **Sync with Git** ON.
2. Open `atl1-edge1` → **Edit**, change the `asn` relationship from
   `AS64496` to `AS8220`, then **Save**.
3. Open a proposed change named
   "Chloe O'Brian: reroute atl1 via Colt (AS8220)".
4. On the **Checks** tab:

   ```text
   atl-to-dfw-via-as64496  ✅  Paths via AS64496 still exist.
   atl-to-jfk-via-as64496  ❌  [atl1-edge1 → AS8220 (Colt) → BGPSession → jfk1-edge1]:
                                forbidden hop of kind InfraAutonomousSystem
                                with asn=8220 present.
   ```

5. Click the `Inspect in UI:` URL on the FAIL card. It opens
   `/path-traversal` pre-filtered to the same source, destination,
   depth, and excluded-kinds the check evaluated. The URL is the value
   of `rule.path_traversal_url`, computed server-side by the
   `path_traversal_url` Python transform.

### 03. FAIL, Tighten the rule, by Administrator (Operations Team)

**The change.** The reachability rule itself is tightened.
`TopologyReachabilityRule.max_depth` is lowered from `3` to `1`.

1. Sign in as Administrator (or the operations-team account).
2. Create a branch named `admin-tighten-depth`, **Sync with Git** ON.
3. Open rule `atl-to-jfk-via-as64496` → **Edit**, set `max_depth = 1`,
   then **Save**.
4. Open a proposed change named
   "Administrator: tighten atl-to-jfk max_depth 3 to 1".
5. On the **Checks** tab:

   ```text
   atl-to-jfk-via-as64496  ❌  No path within depth 1 between 'atl1-edge1'
                                and 'jfk1-edge1'.
   atl-to-dfw-via-as64496  ✅  3/3 paths satisfy all constraints.
   ```

   No path that short exists between the two edge devices, so the rule
   fails on depth. The same tightening change is itself a proposed
   change against `main`, fully PR-reviewable.

## Separation of duties

The three actors above map onto the three roles described in the
recorded demo:

| Role                  | Persona             | What they touch                                                 |
| --------------------- | ------------------- | --------------------------------------------------------------- |
| Automation specialist | (off-camera)        | The check, the query, the transform, and the schema.            |
| Operations team       | Administrator       | The reachability rules and constraints.                         |
| Network engineers     | Sofia, Chloe        | The topology graph: devices, ASNs, BGP sessions, descriptions.  |

In production, the rule surface is locked down with object
permissions so that only the operations team can create or modify
rules. Sofia and Chloe receive a 403 when they try to edit a rule;
the toast quotes the exact missing permission, for example
`object:Topology:ReachabilityRule:update:allow_default`.

To wire this up on the live-demo stack: navigate to
`/objects/CoreAccountRole`, edit **Global read-write**, and add six
`CoreObjectPermission` rows, all `decision: Deny`:

- `Topology:ReachabilityRule:{create, update, delete}`
- `Topology:ReachabilityConstraint:{create, update, delete}`

Then sign in as Sofia or Chloe and confirm the lockdown.

## What changed from the `main` branch

The `main` branch ships the pattern as a stored-`.gql` check that
works on Infrahub 1.10 with no SDK changes. This `live-demo` branch
differs in the following ways:

| Item                          | `main`                              | `live-demo`                                |
| ----------------------------- | ----------------------------------- | ------------------------------------------ |
| Query mechanism               | Stored `path_check.gql`             | `client.traverse_paths()` (SDK 1.22)       |
| `path_check.gql`              | Active                              | Kept (required field) but not executed     |
| Infrahub server               | Any 1.10 or later                   | Pinned to `1.10.0` via `docker-compose`    |
| Infrahub SDK                  | Any compatible release              | Pinned to `1.22.0` via `pyproject.toml`    |
| Topology                      | Bring your own                      | Small bundled seed in `demo-seed/`         |
| Runner                        | n/a                                 | `uv run invoke demo.start / demo.init`     |

The check semantics are identical. `Must transit` (required) is
existence-based, `Never transit` (forbidden) is global, and `Any of`
is existence-based per path.

## Tear down

```bash
uv run invoke demo.stop      # stop containers, preserve volumes
uv run invoke demo.reset     # stop containers, wipe the database
```
