# Reachability Check — demo branch

A minimal, self-contained walkthrough of the reachability-check pattern
running against Infrahub 1.10 and SDK 1.22. Everything you need lives
in this branch:

- `docker-compose.yml` pinned to Infrahub `1.10.0`.
- `demo-seed/` — a tiny network schema (3 kinds) and just enough data
  (3 devices, 3 ASNs, 4 BGP sessions) for two rules to traverse.
- `checks/path_assertion.py` — rewritten on top of SDK 1.22's
  `InfrahubClient.traverse_paths()` instead of a stored `.gql` query.
- `tasks.py` — `uv run invoke demo.start / demo.seed / …` to drive it.

## Quickstart

```bash
# 1. Bring up Infrahub 1.10 (database + workers + server). Healthcheck
#    waits up to 5 minutes for /api/config to return 200.
uv run invoke demo.start

# 2. Load the network schema, seed devices/ASNs/BGP sessions, register
#    the two reachability rules + their constraints.
uv run invoke demo.init

# 3. Open the UI:
open http://localhost:8000
#    Admin token: 06438eb2-8019-4776-878c-0941b1f1d1ec
#    Or sign in as user: admin / infrahub
```

After step 2 you have:

| Kind                          | Count | Examples                              |
| ----------------------------- | ----: | ------------------------------------- |
| `InfraAutonomousSystem`       | 3     | AS64496 (Duff), AS8220 (Colt), AS701  |
| `InfraDevice`                 | 3     | atl1-edge1, jfk1-edge1, dfw1-edge1    |
| `InfraBGPSession`             | 4     | full mesh on AS64496 + atl1↔jfk1 on AS8220 |
| `TopologyReachabilityRule`        | 2     | atl-to-jfk-via-as64496, atl-to-dfw-via-as64496 |
| `TopologyReachabilityConstraint`  | 3     | required AS64496 ×2, forbidden AS8220 ×1 |

## The rules

```text
atl-to-jfk-via-as64496
  source:        atl1-edge1
  destination:   jfk1-edge1
  max_depth:     3
  max_paths:     50
  constraints:
    - required:  InfraAutonomousSystem.asn = 64496   (Duff)
    - forbidden: InfraAutonomousSystem.asn = 8220    (Colt)

atl-to-dfw-via-as64496
  source:        atl1-edge1
  destination:   dfw1-edge1
  max_depth:     3
  max_paths:     50
  constraints:
    - required:  InfraAutonomousSystem.asn = 64496
```

## Register this repo as a CoreRepository

The check only fires inside the proposed-change pipeline once Infrahub
knows about this git repo. Push this branch to any git server the
Infrahub workers can reach, then either:

- In the UI: **Object Management → CoreRepository → + Add**, fill the
  URL, branch `demo`, leave credentials empty for public access.
- Or via `infrahubctl object load` with a `CoreRepository` YAML you
  author yourself.

Once the repo is registered, `.infrahub.yml` is parsed on every commit
and the `path_check` query + `reachability_assertion` check + schema
extension + menu show up automatically.

## Try it

### Happy-path PC (should pass)

1. Create a branch in the UI, name `tweak-atl1-description`,
   **Sync with Git** ON.
2. Open `InfraDevice` → `atl1-edge1` → **Edit** → set description to
   anything (e.g. "demo tweak") → **Save**.
3. Open a proposed change `tweak-atl1-description` → `main`.
4. **Checks** tab — both rule cards turn green:

   ```text
   atl-to-jfk-via-as64496  ✅ 1/1 paths satisfy all constraints (cap: max_paths=50)
   atl-to-dfw-via-as64496  ✅ 1/1 paths satisfy all constraints (cap: max_paths=50)
   ```

### Forbidden-hop PC (should fail)

1. Create a branch `repoint-atl1-to-colt`, **Sync with Git** ON.
2. Open `atl1-edge1`, **Edit**, change `asn` relationship from `AS64496`
   to `AS8220`, **Save**.
3. Open a PC against `main`. **Checks** tab:

   ```text
   atl-to-dfw-via-as64496  ✅  paths via AS64496 still exist
   atl-to-jfk-via-as64496  ❌  forbidden hop of kind InfraAutonomousSystem with asn=8220 present
   ```

4. Click the `Inspect in UI:` URL on the FAIL card — opens
   `/path-traversal` with the same source/destination/depth/excluded-kinds
   the check evaluated.

### Tightening PC (should fail with "no path")

1. Branch `tighten-jfk-depth`, **Sync with Git** ON.
2. Edit rule `atl-to-jfk-via-as64496`, set `max_depth = 1`, **Save**.
3. Open a PC. **Checks**:

   ```text
   atl-to-jfk-via-as64496  ❌  No path within depth 1 between 'atl1-edge1' and 'jfk1-edge1'.
   ```

## RBAC — locking the rule surface (optional)

The community pattern works without this; it's worth showing because
the rule surface is sensitive. The shape:

- Engineers keep their existing read-write role across the rest of
  the network graph.
- The "Global read-write" role gets six `CoreObjectPermission` entries,
  all `decision: Deny`:
  - `Topology:ReachabilityRule:{create,update,delete}`
  - `Topology:ReachabilityConstraint:{create,update,delete}`
- Super Administrators retain authoring access via their wildcard ALLOW.

To wire this up in the demo: in the UI, navigate to
`/objects/CoreAccountRole`, edit **Global read-write**, add six
`CoreObjectPermission` rows under `permissions`, and try editing a rule
as a non-admin user — the toast quotes the exact missing permission.

This is the same Infrahub permission mechanism you'd use for any other
schema kind; the example simply scopes the deny to the rule surface.

## What changed from the `main` branch

`main` ships the pattern as a plain stored-`.gql` check (works on
Infrahub 1.10 with no SDK changes). This `demo` branch:

| What                          | `main`                              | `demo`                                  |
| ----------------------------- | ----------------------------------- | --------------------------------------- |
| Query mechanism               | Stored `path_check.gql`             | `client.traverse_paths()` (SDK 1.22)    |
| `path_check.gql`              | Active                              | Kept (required field) but not executed  |
| Infrahub server               | Any 1.10+                           | Pinned to `1.10.0` via docker-compose   |
| SDK                           | Any                                 | Pinned to `1.22.0` via `pyproject.toml` |
| Topology                      | Bring your own                      | Tiny seed bundled in `demo-seed/`       |
| Runner                        | n/a                                 | `uv run invoke demo.start / demo.init`  |

The check semantics are identical: required is existence-based,
forbidden is global, any_of is existence-based per path.

## Tear down

```bash
uv run invoke demo.stop      # stop containers, keep volumes
uv run invoke demo.reset     # stop + wipe the database
```
