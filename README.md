# Opaque demo

Build and test the [Opaque demo](https://demo.opaque.info/). Visitors review and
approve a task that reads fictional loan-application metrics, run it once, inspect
the result, and try again to see the repeat blocked. Portfolio chat has separate
session limits and permissions.

This private repository contains the demo gateway, browser UI, and hosting code.
The `opaque-showcase` gateway consumes the public core contract through a Git
dependency pinned to an exact revision in [Cargo.toml](Cargo.toml). Build it from
this checkout; no sibling core checkout is required. `Cargo.lock` fixes dependency
resolution.

## Build and validate

Install Rust using the pinned `rust-toolchain.toml` (1.95.0), Node.js 26.8.1 and
Python 3.12.11. Cargo needs GitHub/crates.io access for the initial dependency
fetch. On Linux, the WebAuthn dependencies require the OpenSSL development
headers and standard C/C++ build tools available on GitHub's Ubuntu runner.

```sh
cargo build --locked --workspace
cargo test --locked --workspace
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
node --test crates/opaque-showcase/tests/*.test.cjs deploy/cloudflare-demo/tests/*.test.mjs
python3 -B -m unittest discover -s scripts -p 'test_*.py'
python3 -B -m unittest discover -s deploy/hosted-demo -p 'test_*.py'
```

The Python and Node fixtures use only their standard runtimes. Rust gateway
fixtures generate disposable RSA signing material in memory. Interactive native
approval tests are explicitly ignored in unattended runs. Hosted runtime and
controller fixtures use local fake APIs and loopback services. The checks above
do not change a cluster, contact production models, deploy a Worker, or publish
assets.

The [CI workflow](.github/workflows/ci.yml) runs Rust checks on Linux and macOS,
and browser, Worker, and Python fixtures on Linux. It has read-only repository
permissions and no deployment jobs or credentials.

## Core dependency updates

Update the exact `opaque-core` Git `rev` in the root `Cargo.toml`, regenerate
`Cargo.lock` with Cargo, and rerun all checks above. Review the contract changes
and dependency diff together. Keep this dependency on public core contracts;
enterprise implementations and private validation records belong in their own
repositories.

## Contents

- [crates/opaque-showcase](crates/opaque-showcase) — OAuth MCP metrics gateway and chat UI, scoped to each tenant.
- [assets/brand](assets/brand) — shared visual assets and their provenance.
- [deploy/hosted-demo](deploy/hosted-demo) — Kubernetes runtime, controller, and fixtures.
- [deploy/cloudflare-demo](deploy/cloudflare-demo) — public Worker, static UI, and fixtures.
- [examples/gpu-showcase](examples/gpu-showcase) and [examples/metrics-chat](examples/metrics-chat) — local workflow walkthroughs and setup prerequisites.
- [scripts](scripts) — demo runners, acceptance fixtures, and artifact privacy checks.

## Deployment boundary

Builds and fixture tests do not establish production readiness. Validate approval,
credential custody, and runtime behavior for the intended deployment. Keep runtime
credentials, generated binaries, and session artifacts out of source control.
Publish assets or change a cluster through the authorized deployment workflow.
