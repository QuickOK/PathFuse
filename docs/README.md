# PathFuse documentation

PathFuse makes one dependable connection out of several unreliable WAN uplinks — link liveness,
automatic failover, cross-link redundancy, and adaptive forward error correction over a relay.
(Redundancy + failover + FEC for uptime and low loss — **not** bandwidth aggregation.)

- **[architecture.md](architecture.md)** — components, data + control planes, failover/egress/FEC
  behavior, and the port map. Start here to understand how it works.
- **[design-notes.md](design-notes.md)** — the key design decisions and their rationale.

Other entry points:
- **Run / read the code** — the top-level [`../README.md`](../README.md) (quickstart + tests).
- **Deploy it** — [`../deploy/README.md`](../deploy/README.md) (manual), or open the repo in Claude
  Code and run `/pathfuse-setup` for guided / SSH-assisted setup.
