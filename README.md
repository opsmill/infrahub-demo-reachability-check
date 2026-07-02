# Reachability Check via Graph Traversal

## What is it?

A way to write down the rule *"this must connect to that, only
through these approved points, and never through those"* once, and
have it re-checked automatically every time anything in your
environment changes. Before a change goes live, you get a yes/no per
rule and a one-click view of exactly where a "no" would happen.

## Why should I care?

Most organizations carry these rules only in senior people's heads:
*"customer traffic must never bypass the inspection layer"*, *"EU
data must stay in the EU"*, *"the audit data store is never reachable
from the guest network"*. When a change quietly breaks one, the
failure shows up late: a midnight outage, a failed audit, a regulator
letter, a customer call. This pattern catches it at review time,
before anyone hits "merge", so the rule survives staff turnover and
stays current as the business grows.

**Reachability is a topology notation, not a network-only idea.** Any
graph source of truth (network topology, regulatory scope, security
zones, agent delegation, service dependencies, capacity) admits the
question *"does X reach Y subject to these constraints?"* This
repository makes that question a first-class Infrahub object,
evaluated on every proposed change.

📺 **Walkthrough video:** [Reachability Check via Graph Traversal on YouTube](https://www.youtube.com/watch?v=guyEHTsqruI).

> **Where this came from.** Infrahub 1.10 introduced path traversal
> at AutoCon. The follow-up questions were uniformly *"can this also
> be used for impact assessment? firewall compliance? maintenance
> drain?"* The answer is yes, every one of them, with primitives
> Infrahub already ships. This repository is the worked example.

> **Works in Infrahub today, no product changes.** The schema, check,
> transform, query, and menu in this repository are user content. The
> path-traversal engine, proposed-change pipeline, object permissions,
> and computed attributes are the existing Infrahub 1.10 surface.
> Nothing to fork, patch, or wait for.

**Where to go from here:**

- *Just want to run it?* Switch to [`live-demo`](../../tree/live-demo)
  and follow [`QUICKSTART.md`](../../tree/live-demo/QUICKSTART.md).
  One command brings up Infrahub 1.10.1 with three preloaded proposed
  changes to inspect.
- *Want to deploy it yourself?* See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)
  for schema, check, transform, sequence diagrams, RBAC, deployment.
- *Want to understand the value first?* Read on.

---

## The use case

### What teams are trying to do

A rule of the form *source → destination + hop predicates* expresses
a wide family of operational questions:

| Domain                       | Concrete question the rule asserts                                                |
| ---------------------------- | --------------------------------------------------------------------------------- |
| **Routing / transit**        | "Atlanta-to-NYC must transit AS64496 and never AS8220." (The example in this repo.) |
| **Firewall & compliance**    | "Every customer-to-database flow must transit the inspection zone and never bypass it." |
| **Capacity reachability**    | "Atlanta must reach NYC with at least 10 Gb/s of usable capacity along the path." |
| **Tenant & zone segmentation** | "Tenant A's data plane is never reachable from tenant B's, at any depth." |
| **Service & dependency graphs** | "Order-service must reach payment-service via approved internal APIs only." |
| **Regulatory-scope segmentation** | "No out-of-scope system can reach the cardholder-data store at any depth, and Level-4 IT never reaches a Level-1 PLC except via the IDMZ." |
| **Lateral-movement containment** | "From any compromised endpoint, the crown-jewel database remains unreachable without transiting a PAM jump host." |
| **AI agent capability delegation** | "An untrusted agent cannot reach a privileged tool (payment, prod-deploy, file-write) through any MCP/A2A delegation chain." |
| **Continuous compliance audits** | "Every rule, every proposed change, with a full diffable history." |

### Why it is hard today

In the standard network-automation playbook, an invariant like
*"Atlanta-to-NYC must transit AS64496 and never AS8220"* is enforced
by a Python script that re-derives the topology from flat tables on
every run: fetch devices, interfaces, BGP sessions, prefixes; rebuild
adjacency in memory; iterate every source / destination / path
permutation; apply the rule. You reconstruct the graph in code, by
hand, with no help from the system that already holds it, and the
moment the schema grows or a new device kind appears, the script
silently stops covering cases.

A graph source of truth flips this. Relationships between devices,
sessions, zones, and tenants are first-class edges; *"which paths
exist between A and B subject to these constraints?"* becomes a
traversal query, not a nested loop. The engine walks the graph, the
rule declares the predicate, no user code enumerates permutations. A
script cannot ask that question without re-deriving the graph; the
graph answers it natively.

Without that, even with sound data, the invariants live in people,
not in the system:

- **Slack threads and tribal knowledge.** A senior engineer eyeballs
  the diff and remembers the rule. The newer engineer does not even
  know to ask.
- **Post-deploy fire-fighting.** The change merges, monitoring catches
  the broken path twenty minutes later, somebody rolls back.
- **One-off scripts.** A Python script in someone's home directory,
  run against a snapshot that drifted from production three releases
  ago, re-deriving the same graph the source of truth already holds.
- **Diagrams in Confluence.** Out of date the day after they are
  drawn, disconnected from the data that drives the deployment.

The failure mode: the invariant is not authored where the change is
reviewed, and verification logic is re-implemented from flat data
every time. By the time anyone notices, the change is in.

### What Infrahub delivers

1. **Rules are graph data.** A `TopologyReachabilityRule` sits in the
   same database as the topology it asserts on, same branch diff,
   same review workflow.
2. **Graph traversal answers reachability natively.**
   `InfrahubPathTraversal` returns the actual hops between two nodes,
   branch-aware and time-aware. Real paths, not approximations.
3. **One verdict per rule on every proposed change.** The check fans
   out per rule and emits a PASS/FAIL card, each linking directly to
   the offending path.

## How to use Infrahub

### Step 1: Author the rule once, in the graph

The operations team creates a `TopologyReachabilityRule` with a
`source`, `destination`, two knobs (`max_depth`, `max_paths`), and
one or more `TopologyReachabilityConstraint` children. Constraints
are `required` (path must include a matching hop) or `forbidden` (no
returned path may include one). Both endpoints accept any node kind
via `peer: CoreNode`, so a rule can be device-to-device,
flow-to-firewall-zone, tenant-to-tenant, or service-to-service.
Adding, tightening, or retiring a rule is itself a reviewable graph
diff: one place to look for "what does this rule assert?"

### Step 2: Graph traversal answers the reachability question

When a proposed change opens, `InfrahubPathTraversal` finds every
path from source to destination on the proposed-change branch, up to
`max_depth` hops, capped at `max_paths`, excluding rule and
constraint nodes themselves so the rule does not appear as a 1-hop
shortcut. The check evaluates the actual graph after the change,
branch-aware and time-aware. No approximation.

### Step 3: Fan out, one verdict per rule

The `reachability_assertion` `CoreCheckDefinition` registers
`targets: reachability-rules` with parameter mapping `rule_id: "id"`.
The runner fans the check out once per `TopologyReachabilityRule`
member, runs the traversal, evaluates every returned path against
every constraint, and emits one verdict card per rule. The reviewer
sees "atl-to-jfk-via-AS64496 ✅" next to "atl-to-dfw-via-AS64496 ❌"
rather than a single boolean over the whole set.

### Step 4: One click from verdict to failing path

Every rule carries a `path_traversal_url` attribute, computed
server-side by a Python transform whenever the rule's inputs change.
The UI renders it as a hyperlink on the rule detail page that opens
`/path-traversal` pre-filtered to the same source, destination,
depth, and excluded-kinds the check evaluated. A reviewer reading
"❌ forbidden hop of kind InfraAutonomousSystem with asn=8220 present"
clicks once and sees the offending path.

### Step 5: Separation of duties

Three roles, enforced by object permissions:

- **Automation specialist** builds the check once (this repo).
- **Operations team** owns rules and constraints, sole holders of
  create / update / delete on `Topology:ReachabilityRule` and
  `Topology:ReachabilityConstraint`.
- **Network engineers** change the topology freely but cannot edit
  rules, so they cannot loosen the assertion that catches their own
  change.

Network engineers stop carrying the worry about whether their change
broke a topology guarantee. The check absorbs it. Rules live on a
smaller team, slower cadence; topology lives on the engineers, day
to day.

## Beyond reachability: same notation, many invariants

Same `source → destination + hop predicates` rule shape, different
parts of the graph. Adding a domain typically means a new
hop-predicate vocabulary (e.g., `hop_attribute_ge` for capacity,
`disjoint_paths` for redundancy), not rebuilding the pipeline:

| Use case                       | Rule shape                                                                 |
| ------------------------------ | -------------------------------------------------------------------------- |
| **Routing / transit (this demo)** | source: device · destination: device · constraints: AS hops               |
| **Firewall compliance**        | source: flow endpoint · constraints: must transit fw-zone, never bypass    |
| **Capacity reachability**      | constraints: hop attribute (bandwidth) ≥ N                                 |
| **Tenant & zone segmentation** | forbidden: reach destination at all                                        |
| **Path redundancy**            | require: 2 disjoint paths                                                  |
| **Latency & SLA bounds**       | within: N hops · constraints: transit low-latency                          |
| **Maintenance drain safety**   | require: reroute before drain                                              |
| **Service & dependency graphs** | constraints: only via approved internal APIs                              |
| **Regulatory-scope segmentation (PCI · HIPAA · OT/Purdue)** | forbidden: any path from out-of-scope kind reaches regulated-data store    |
| **Lateral-movement blast radius** | from: compromised node · forbidden: reach crown-jewel kind without PAM/PEP transit |
| **AI agent capability delegation** | source: agent · destination: privileged tool · forbidden: untrusted delegation hop |
| **Dependency & blast-radius**  | reachable from: node · target kinds                                        |
| **Change impact assessment**   | diff: reachable paths · before vs after                                    |
| **Continuous compliance**      | policy holds · on every change                                             |

> **Future demos.** Only the routing / transit scenario currently
> ships with a runnable demo (the `live-demo` branch). Additional
> branches (`live-demo-firewall-compliance`,
> `live-demo-capacity-reachability`, etc.) will follow for any
> use-case with demand. Vote via GitHub issue or OpsMill Discord
> (`discord.gg/opsmill`).

## Try it

Switch to [`live-demo`](../../tree/live-demo) and run:

```bash
uv run invoke demo.up
```

This brings up Infrahub 1.10.1 in docker, registers this repository as
a `CoreRepository`, seeds the topology, creates the rules, and opens
three proposed changes mirroring the recorded demo (Sofia, Chloe,
Administrator). Full walkthrough in
[`QUICKSTART.md`](../../tree/live-demo/QUICKSTART.md).

## Infrahub in 30 seconds

[Infrahub](https://infrahub.opsmill.com/) is a graph-based source of
truth for infrastructure data with git-style branching, a typed
schema you extend, and a proposed-change pipeline that runs checks
against the branched data before merge. This repository is one
worked example: a user-extended schema (`TopologyReachabilityRule` +
`TopologyReachabilityConstraint`), a stored GraphQL query, a Python
transform producing a computed-attribute URL, and a Python check that
evaluates the rule against `InfrahubPathTraversal` results on every
proposed change.

For architecture, deployment, RBAC, and adoption guide, continue to
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).
