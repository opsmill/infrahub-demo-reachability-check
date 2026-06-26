# Reachability Check via Graph Traversal

**Reachability is a topology notation, not a network-only idea.** Any
time your source of truth is a graph (network topology, compliance
flows, security zones, link capacity, service dependencies), the
question "does *X* reach *Y* subject to these constraints?" has the
same shape. This repository turns that question into a first-class
Infrahub object, evaluated on every proposed change.

📺 **Walkthrough video:** [Reachability Check via Graph Traversal on YouTube](https://www.youtube.com/watch?v=guyEHTsqruI).

> **Where this came from.** Infrahub 1.10 introduced path traversal
> at AutoCon. The audience reception was strong, and the follow-up
> questions were uniformly *"can this also be used for impact
> assessment? firewall compliance? maintenance drain scenarios?
> physical and logical topology evaluation?"* The answer is yes,
> every one of them, with the primitives Infrahub already ships.
> This repository is the worked example.

> **Works in Infrahub today, with no product changes.** The schema,
> check, transform, stored query, and menu in this repository are
> user content. The path-traversal engine, the proposed-change
> pipeline, role-based object permissions, and computed attributes
> are the existing Infrahub 1.10 surface. There is nothing to fork,
> patch, or wait for.

**Where to go from here:**

- *Just want to run it?* Switch to the [`live-demo`](../../tree/live-demo)
  branch and follow [`QUICKSTART.md`](../../tree/live-demo/QUICKSTART.md).
  One command brings up Infrahub 1.10 with the pattern installed and
  three preloaded proposed changes ready to inspect.
- *Want to deploy it in your own Infrahub?* See
  [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the full
  technical reference: schema, check, transform, sequence diagrams,
  RBAC recipe, deployment steps.
- *Want to understand the value first?* Read the rest of this page.

---

## The use case

### What teams are trying to do

A rule of the form *source → destination + hop predicates* expresses
a wide family of operational questions. Some examples:

| Domain                       | Concrete question the rule asserts                                                |
| ---------------------------- | --------------------------------------------------------------------------------- |
| **Routing / transit**        | "Atlanta-to-NYC must transit AS64496 and never AS8220." (The example in this repo.) |
| **Firewall & compliance**    | "Every customer-to-database flow must transit the inspection zone and never bypass it." |
| **Capacity reachability**    | "Atlanta must reach NYC with at least 10 Gb/s of usable capacity along the path." |
| **Tenant & zone segmentation** | "Tenant A's data plane is never reachable from tenant B's, at any depth." |
| **Service & dependency graphs** | "Order-service must reach payment-service via approved internal APIs only." |
| **Continuous compliance audits** | "Every rule, every proposed change, with a full diffable history." |

All of these are the same notation pointed at different parts of the
graph. Only the source kind, destination kind, and hop predicates
change. The rest of the machinery is shared.

### Why it is hard today

Without something like this pattern, those invariants live in people,
not in the system:

- **Slack threads and tribal knowledge.** A senior engineer eyeballs
  the diff and remembers the rule. The newer engineer does not even
  know to ask.
- **Post-deploy fire-fighting.** The change merges, monitoring catches
  the broken path twenty minutes later, somebody rolls back.
- **One-off scripts.** A Python script in someone's home directory,
  run against a snapshot of the source of truth that drifted from
  production three releases ago.
- **Diagrams in Confluence.** Out of date the day after they are
  drawn, disconnected from the data that drives the deployment.

The common failure mode is the same: the invariants are not authored
where the change is reviewed. By the time anyone notices, the change
is in.

### What Infrahub delivers

Three things, all of which the pattern in this repository takes
advantage of:

1. **Rules are graph data, not configuration files.** Each
   `TopologyReachabilityRule` is a node in the same Infrahub database
   as the topology it asserts on. It diffs on a branch like any other
   object. The PR that authors or tightens a rule is reviewed by the
   same workflow that reviews the topology changes the rule guards.
2. **Graph traversal answers a question CMDB tools cannot.**
   Infrahub 1.10's `InfrahubPathTraversal` returns the actual hops
   between two nodes, branch-aware and time-aware. The check
   evaluates real paths, not approximations.
3. **One verdict per assertion on every proposed change.** A
   `CoreCheckDefinition` with `targets: reachability-rules` fans the
   check out once per rule and emits a PASS/FAIL verdict card per
   rule on the PC. Investigators click straight from the rule to
   the failing path through a server-computed URL attribute on the
   rule.

## How to use Infrahub

This is the demo flow as a value walkthrough. Each step explains
what is happening and what the team gains from it.

### Step 1: Author the rule once, in the graph

The operations team creates a `TopologyReachabilityRule` with a
`source`, a `destination`, two tuning knobs (`max_depth`,
`max_paths`), and one or more `TopologyReachabilityConstraint`
children. Constraints are either `required` (the path must include a
matching hop) or `forbidden` (no returned path may include one).
Both endpoints accept any node kind via `peer: CoreNode`, so a rule
can be device-to-device, flow-to-firewall-zone, tenant-to-tenant, or
service-to-service.

**Value at this step:** the rule is a graph object. Adding,
tightening, or retiring a rule is itself a reviewable diff. There is
exactly one place to look for "what does this rule assert?" and it is
the same place the topology lives.

### Step 2: Graph traversal answers the reachability question

When a proposed change opens, Infrahub's
`InfrahubPathTraversal` finds every path from the rule's source to
its destination on the proposed-change branch, up to `max_depth`
hops, capped at `max_paths` paths, and excluding rule/constraint
nodes themselves so the rule does not appear as a 1-hop shortcut.

**Value at this step:** the check evaluates the actual graph after
the change. No approximation, no "what we think the topology will
look like." Branch-aware, so the question can be re-asked at any
point in time without writing custom queries.

### Step 3: Fan out, one verdict per rule

A `CoreCheckDefinition` named `reachability_assertion` is registered
with `targets: reachability-rules` and a single
parameter mapping (`rule_id: "id"`). The Infrahub check runner fans
the check out one invocation per `TopologyReachabilityRule` member
of the group, loads the rule with its constraints, runs the
traversal, evaluates every returned path against every constraint,
and emits one verdict card per rule.

**Value at this step:** the assertion granularity matches the rule
granularity. The PC reviewer sees "atl-to-jfk-via-AS64496 ✅" next
to "atl-to-dfw-via-AS64496 ❌" rather than a single boolean over the
whole set.

### Step 4: One click from the verdict to the failing path

Every rule carries a `path_traversal_url` attribute, computed
server-side by a Python transform whenever the rule's inputs change.
The Infrahub UI renders it as a clickable hyperlink on the rule
detail page. Clicking opens `/path-traversal` pre-filtered to the
same source, destination, depth, and excluded-kinds the check
evaluated.

**Value at this step:** the investigation surface and the assertion
surface are the same surface. A reviewer who reads "❌ forbidden hop
of kind InfraAutonomousSystem with asn=8220 present" clicks the link
on the rule and sees the offending path immediately.

### Step 5: Separation of duties

Three roles, with object permissions enforcing the separation:

- **Automation specialist** builds the path-traversal check once
  (this repo).
- **Operations team** authors the rules and constraints. They are
  the only role with create / update / delete on
  `Topology:ReachabilityRule` and `Topology:ReachabilityConstraint`.
- **Network engineers** change the topology graph freely. They
  cannot edit rules, so they cannot loosen the assertion that catches
  their own change.

**Value at this step:** network engineers do not have to worry
about whether their change broke a topology guarantee. The check
absorbs that worry. The operations team sets constraints in stone
that the network engineer cannot change no matter what, and every
change a network engineer makes is checked against those rules
before merge. The rule surface is reviewable separately, by a
smaller team, on a slower cadence.

## Beyond reachability — same notation, many invariants

Every domain in the table above is the same `source → destination +
hop predicates` rule shape pointed at different parts of the graph.
Adding a new domain typically means adding a new hop predicate vocabulary
(for example `hop_attribute_ge` for capacity, `disjoint_paths` for
redundancy), not rebuilding the pipeline:

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
| **Dependency & blast-radius**  | reachable from: node · target kinds                                        |
| **Change impact assessment**   | diff: reachable paths · before vs after                                    |
| **Continuous compliance**      | policy holds · on every change                                             |

> **Future demos.** Today only the routing / transit scenario ships
> with a runnable docker-compose demo (the `live-demo` branch). If
> there is demand for any of the other use-cases above, additional
> demo branches (`live-demo-firewall-compliance`,
> `live-demo-capacity-reachability`, etc.) will follow. Open an issue
> or drop a note in the OpsMill Discord (`discord.gg/opsmill`) to
> vote on which one comes next.

## Try it

Switch to the [`live-demo`](../../tree/live-demo) branch and run:

```bash
uv run invoke demo.up
```

This brings up Infrahub 1.10 in docker, registers this repository as
a `CoreRepository`, seeds the topology, creates the rules, and opens
three proposed changes mirroring the recorded demo (Sofia, Chloe,
Administrator). The full walkthrough lives in
[`QUICKSTART.md`](../../tree/live-demo/QUICKSTART.md) on that branch.

## Infrahub in 30 seconds

[Infrahub](https://infrahub.opsmill.com/) is a graph-based source of
truth for infrastructure data with git-style branching, a typed
schema you extend, and a proposed-change pipeline that runs checks
against the branched data before merge. This repository is one
worked example of that machinery: a user-extended schema
(`TopologyReachabilityRule` + `TopologyReachabilityConstraint`), a
stored GraphQL query, a Python transform that produces a
computed-attribute URL, and a Python check that evaluates the rule
against `InfrahubPathTraversal` results on every proposed change.

For the architecture, deployment recipe, RBAC details, and adoption
guide, continue to [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).
