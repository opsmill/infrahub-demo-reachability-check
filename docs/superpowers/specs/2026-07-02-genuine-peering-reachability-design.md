# Genuine peering enforcement for the atl1→jfk1 reachability rule

Date: 2026-07-02
Status: **IMPLEMENTED** — via Infrahub 1.10.1 all-paths mode. Approach A
(atl1 as customer edge) works once the check evaluates over *all* loopless
paths (`shortest_paths_only: false`), which needs the 1.10.1 API. See the
implementation plan `docs/superpowers/plans/2026-07-02-genuine-peering-upgrade.md`.
The original single-version "Implementation finding" below (why Approach A
fails on 1.10.0 shortest-mode) is retained for context.

## Implementation finding (blocker)

Approach A was implemented and loaded onto the live instance, then
reverted. It **regresses `atl-to-dfw-via-as64496` to RED**.

Root cause: `InfrahubPathTraversal` is deterministic but
**non-exhaustive** — it returns a shortest-path-tree-like subset of
paths, not all simple paths. Measured on the live instance (atl1 as
customer AS65001, `max_depth=3`):

- `atl→jfk`: returns the depth-3 `atl1 → session(atl1↔dfw1) →
  AS64496 → jfk1` path → GREEN.
- `atl→dfw`: returns **only** the depth-2 direct
  `atl1 → session → dfw1` path. The depth-3
  `atl1 → session → AS64496 → dfw1` path exists in the graph but is
  **never returned** → RED. Its AS-transiting path only surfaces at
  `max_depth=4`.

Implication: the shared-`asn` co-membership edge was **load-bearing**,
not merely "trivially true". It placed AS64496 on a *depth-2* path that
the engine reliably returns. Removing it makes every AS-transit path
strictly longer than the direct device↔device session path, and the
engine drops the longer path for at least one rule. With this schema
(BGP sessions connect devices directly, so the shortest path never
transits an AS node) and this engine, "genuine-peering-only" enforcement
cannot be achieved just by removing co-membership.

Open directions to re-brainstorm with the user are recorded in the
conversation; the original design below is retained for context.

---

Date: 2026-07-02
Status: Approved (design) — superseded by the finding above

## Problem

The rule `atl-to-jfk-via-as64496` asserts: *"Does the path from
`atl1-edge1` to `jfk1-edge1` transit AS64496, and never AS8220?"*
It is green today, but for the wrong reason.

Every edge device (`atl1-edge1`, `jfk1-edge1`, `dfw1-edge1`) carries
`asn = AS64496` via the `device.asn` attribute. When **both** endpoints
share that attribute, the path traversal manufactures a trivial hop:

```
atl1-edge1 → AS64496 (Duff) → jfk1-edge1        (depth 2)
```

This path exists purely because both devices are co-members of
AS64496. It does not depend on any BGP session. The rule therefore
stays green even if every peering between the devices is deleted — the
assertion is not actually enforcing peering connectivity through the
backbone.

Verified on the live instance (`main`, `max_depth=3`): three paths
exist, and the load-bearing "transit" one is the co-membership
shortcut above.

## Goal

The rule must reflect **genuine peering connectivity** through
AS64496:

- Green only when `atl1-edge1` actually peers into the AS64496 backbone.
- Sever all of atl1's AS64496 peerings → rule goes **RED**.
- Co-membership alone must **not** satisfy it.
- Never transit AS8220 (forbidden) — preserved.
- All three existing demo scenarios keep working.

## Approach (chosen: minimal)

Make the **source** device a customer edge that reaches the backbone
only through peering. `atl1-edge1` moves into its own customer AS
(AS65001); `jfk1-edge1` and `dfw1-edge1` remain in the Duff backbone
(AS64496). Because atl1 is no longer a co-member of AS64496, the
shortcut disappears and AS64496 can only appear on the path via a real
peering session's `remote_as`.

Resulting topology:

```
atl1-edge1  asn: AS65001  (customer edge — "Peachtree Metro")
jfk1-edge1  asn: AS64496  (Duff backbone)
dfw1-edge1  asn: AS64496  (Duff backbone)

BGP sessions (unchanged):
  atl1-edge1 ↔ jfk1-edge1   remote_as AS64496   (peer into Duff)
  atl1-edge1 ↔ dfw1-edge1   remote_as AS64496   (peer into Duff)
  jfk1-edge1 ↔ dfw1-edge1   remote_as AS64496   (iBGP in Duff)
  atl1-edge1 ↔ jfk1-edge1   remote_as AS8220    (pre-wired Colt, dormant)
```

### Why this works (empirically validated on branches)

- **main**: co-membership shortcut gone; a genuine peering path remains,
  e.g. `atl1 → session(atl1↔dfw1) → AS64496 → jfk1` (depth 3). Rule
  green — because atl1 peers into the backbone. ✅
- **Chloe reroute** (`atl1.asn → AS8220`): path
  `atl1 → AS8220 → session(atl1↔jfk1) → jfk1` (depth 3) transits the
  forbidden AS8220 → rule RED. ✅ (Same `device.asn` mechanism as today;
  Chloe's edit overwrites atl1's asn regardless of its prior value.)
- **Sever peerings**: atl1's only routes to AS64496 are the two
  sessions with `remote_as=AS64496`. Remove both → atl1 isolated from
  the backbone → RED. ✅

### Semantics nuance (accepted)

atl1 reaches the backbone via *either* the jfk1 or the dfw1 peering.
Deleting **one** keeps the rule green (redundancy); it goes red only
when **all** of atl1's AS64496 peerings are gone. This is the correct
reading of "transit reachability" (as opposed to "direct adjacency").

`max_depth` stays 3.

## Changes

1. `demo-seed/data/01-asns.yml` — add customer AS:
   `name: Peachtree Metro`, `asn: 65001`,
   `description: "Atlanta edge customer AS — peers into the Duff backbone (AS64496)."`
2. `demo-seed/data/02-devices.yml` — `atl1-edge1.asn`: `"64496"` → `"65001"`.
3. `demo-seed/data/03-bgp-sessions.yml` — update the header and Colt
   comments: atl1 is now a customer peering into AS64496; the
   co-membership shortcut no longer exists; the forbidden path forms via
   `atl1.asn → AS8220` + the dormant Colt session.
4. `demo-seed/setup.py` — Chloe scenario `pc_description`:
   `atl1-edge1.asn AS64496 -> AS8220` → `AS65001 -> AS8220`. Scenario
   edit logic and the other two scenarios are unchanged.
5. `README.md` — update the topology / mechanism narrative wherever it
   states or implies all devices share AS64496, to reflect atl1 as a
   customer edge peering into the backbone.

No changes to the check code (`checks/path_assertion.py`), the query,
the schema, or the rule definitions — the check logic is already
correct; only the seed topology was misleading it.

## Verification

After `infrahubctl object load` of the updated data on the live
instance:

1. **main**: traverse `atl1→jfk1` (depth 3). Assert the
   `atl1 → AS64496 → jfk1` co-membership path is **absent** and at
   least one `... → AS64496 → jfk1` peering path is **present**.
2. **Chloe branch** (repoint `atl1.asn → AS8220`): traverse; assert a
   path transiting AS8220 exists (rule would go RED).
3. **Sever check** (throwaway branch: delete atl1's two AS64496
   sessions): traverse; assert **no** path transits AS64496 (rule would
   go RED) — proving genuine peering dependence.

Throwaway branches are deleted after each check.
