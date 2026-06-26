# Quickstart

Run the reachability-check pattern end-to-end against a clean
Infrahub 1.10 stack on your laptop. This branch (`live-demo`) ships
a `docker-compose.yml` pinned to Infrahub 1.10, a minimal topology
seed, the SDK 1.22 check, and an `invoke` task that puts it all
together.

Looking for the value walkthrough instead of the steps? See
[`../README.md`](README.md). For the architecture, schema reference,
and adoption guide, see
[`../docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with `docker compose`
  v2 available on the path.
- [`uv`](https://docs.astral.sh/uv/) for driving the invoke tasks.
- Ports `8000` (Infrahub), `15692` (RabbitMQ management), `2004` and
  `6362` (Neo4j) free on the host. If you are running another
  Infrahub on the same machine, drop a local
  `docker-compose.override.yml` in the repo root to remap host
  ports; the file is gitignored.

## One command

```bash
uv run invoke demo.up
```

This runs five steps in order:

1. **`demo.start`** — builds the single-branch bare clone the
   task-worker bind-mounts at `/srv/reachability`, then
   `docker compose up -d` for Infrahub 1.10. Waits up to five
   minutes for `/api/config` to return 200.
2. **`demo.init --phase data`** — loads the network schema, the
   reachability schema and menu, the topology seed (3 ASNs, 3
   devices, 4 BGP sessions), and creates the `reachability-rules`
   `CoreStandardGroup`.
3. **`demo.register-repo`** — registers this repository as a
   `CoreRepository` via `file:///srv/reachability` and polls until
   the worker has installed `CoreTransformPython(path_traversal_url)`
   and `CoreCheckDefinition(reachability_assertion)`.
4. **`demo.init --phase rules`** — creates the two demo rules and
   their constraints. The Python transform fires automatically on
   every rule save, so `path_traversal_url` populates on each rule.
5. **`demo.init --phase scenarios`** — creates three Infrahub
   branches plus the matching proposed changes (Sofia, Chloe,
   Administrator) so the demo is ready to inspect.

On a healthy machine, `demo.up` takes 1–2 minutes; the register-repo
poll dominates.

## What you get

Open <http://localhost:8000>. Default admin token:
`06438eb2-8019-4776-878c-0941b1f1d1ec` (or sign in as
`admin` / `infrahub`).

| Kind                              | Count | Notes                                                       |
| --------------------------------- | ----: | ----------------------------------------------------------- |
| `InfraAutonomousSystem`           |     3 | AS64496 (Duff), AS8220 (Colt), AS701                        |
| `InfraDevice`                     |     3 | atl1-edge1, jfk1-edge1, dfw1-edge1                          |
| `InfraBGPSession`                 |     4 | Full mesh on AS64496 plus atl1↔jfk1 on AS8220               |
| `TopologyReachabilityRule`        |     2 | `atl-to-jfk-via-as64496`, `atl-to-dfw-via-as64496`          |
| `TopologyReachabilityConstraint`  |     3 | "Required AS64496" ×2, "Forbidden AS8220" ×1                |
| `CoreProposedChange`              |     3 | Sofia (PASS), Chloe (FAIL forbidden), Administrator (FAIL no path) |

The two reachability rules:

```text
atl-to-jfk-via-as64496
  source:      atl1-edge1
  destination: jfk1-edge1
  max_depth:   3
  max_paths:   50
  constraints:
    - Required hop:  InfraAutonomousSystem.asn = 64496 (Duff)
    - Forbidden hop: InfraAutonomousSystem.asn = 8220  (Colt)

atl-to-dfw-via-as64496
  source:      atl1-edge1
  destination: dfw1-edge1
  max_depth:   3
  max_paths:   50
  constraints:
    - Required hop:  InfraAutonomousSystem.asn = 64496
```

## The three scenarios

Each preloaded proposed change is a one-field change on its own
branch. Mirror of slide 15 of the recorded demo.

### 01. PASS — Sofia Hernandez, documentation tweak

- **Branch:** `shernandez-doc-tweak`
- **Change:** `atl1-edge1.description` updated. No topology change.
- **Open the PC:** Proposed Changes → "Sofia Hernandez: atl1
  documentation tweak (happy path)" → **Checks** tab.
- **Expected verdict (both rule cards green):**

  ```text
  atl-to-jfk-via-as64496  ✅  3/3 paths satisfy all constraints (cap: max_paths=50)
  atl-to-dfw-via-as64496  ✅  3/3 paths satisfy all constraints (cap: max_paths=50)
  ```

### 02. FAIL — Chloe O'Brian, reroute via Colt (AS8220)

- **Branch:** `cobrian-reroute-via-colt`
- **Change:** `atl1-edge1.asn` reassigned from AS64496 to AS8220.
- **Open the PC:** Proposed Changes → "Chloe O'Brian: reroute atl1
  via Colt (AS8220)" → **Checks** tab.
- **Expected verdict:**

  ```text
  atl-to-dfw-via-as64496  ✅  Paths via AS64496 still exist.
  atl-to-jfk-via-as64496  ❌  [atl1-edge1 → AS8220 (Colt) → BGPSession → jfk1-edge1]:
                                forbidden hop of kind InfraAutonomousSystem
                                with asn=8220 present.
  ```

### 03. FAIL — Administrator, tighten max_depth

- **Branch:** `admin-tighten-depth`
- **Change:** `atl-to-jfk-via-as64496.max_depth` lowered from `3`
  to `1`.
- **Open the PC:** Proposed Changes → "Administrator: tighten
  atl-to-jfk max_depth 3 to 1" → **Checks** tab.
- **Expected verdict:**

  ```text
  atl-to-jfk-via-as64496  ❌  No path within depth 1 between 'atl1-edge1' and 'jfk1-edge1'.
  atl-to-dfw-via-as64496  ✅  3/3 paths satisfy all constraints.
  ```

## Inspect the failing path

The check does not embed the URL in the verdict log. Instead, every
rule carries a server-computed `path_traversal_url` attribute. On
the FAIL card you just opened, click into the rule on its detail
page and click the `path_traversal_url` link. The Infrahub UI opens
`/path-traversal` pre-filtered to the same source, destination,
depth, and excluded-kinds the check evaluated. The offending hop
(AS8220 for Chloe's scenario, or the depth-1 result for the
Administrator scenario) is right there.

## Tear down

```bash
uv run invoke demo.stop      # stop containers, preserve volumes
uv run invoke demo.reset     # stop and wipe the database + storage
```

`demo.stop` is the right call between sessions: a later
`uv run invoke demo.start` brings everything back as you left it.
`demo.reset` wipes the docker volumes and lets you start over from
a clean Infrahub.

## Where to go next

- [`README.md`](README.md) — the value walkthrough, the use-case
  table beyond reachability, and the future-demos note.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) — schema,
  check, transform, sequence diagram, RBAC recipe, deployment steps.
- Recorded walkthrough: [Reachability Check via Graph Traversal on YouTube](https://www.youtube.com/watch?v=guyEHTsqruI).
