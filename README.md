# Opaque demo

Customer-facing demo collateral for Opaque, extracted from the private
`opaque-dogfood` workspace on 2026-09-09. Private repo — sales/demo
material, not the core product.

## Contents

- `crates/opaque-showcase` — the tenant-scoped OAuth MCP metrics gateway and
  chat client behind the hosted demo.
- `deploy/hosted-demo/` — the Kubernetes-hosted demo runtime, controller,
  and operations docs.
- `deploy/cloudflare-demo/` — the public-facing Cloudflare Worker in front
  of the demo.
- `examples/gpu-showcase`, `examples/metrics-chat` — standalone example
  walkthroughs of the demo's GPU inference and metrics-chat flows.
- `scripts/demo_*.py`, `scripts/demo_*.sh`, `scripts/metrics_chat_dogfood.py`,
  `scripts/portfolio_exploration_dogfood.py`, `scripts/check_site_privacy.py`,
  `scripts/record_demos.sh` — the scripts that drive and validate the demo.

## Known gap: this doesn't build standalone yet

`crates/opaque-showcase` has a path dependency on `opaque-core`
(`../opaque-core`), which lives in the core product repo
(`kcirtapfromspace/opaque`) and hasn't been vendored or wired up here yet.
Getting this crate building standalone — via a git dependency once the core
repo has the current feature set, a vendored copy, or a workspace that
spans both repos — is follow-up work, not done as part of this extraction.

See `kcirtapfromspace/opaque-dogfood`'s
`docs/product/2026-09-09-repo-topology-plan.md` for the full three-repo
split this came from.
