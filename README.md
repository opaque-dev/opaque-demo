# Opaque demo

Private customer-facing demo collateral for Opaque. This repository builds its
`opaque-showcase` gateway independently while consuming the public core contract
as a Git dependency pinned to an exact revision in the root `Cargo.toml`.
No sibling checkout, copied broker implementation or private core dependency is
required. `Cargo.lock` fixes the dependency resolution for reproducible checks.

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

The Python and Node fixture suites require only their standard runtimes. The
Rust gateway fixtures generate disposable RSA signing material in memory; they
do not depend on another repository's private-key fixture. Interactive native
approval tests remain explicitly ignored in unattended runs. Hosted runtime and
controller fixtures use local fake APIs/loopback services; these commands do not
operate a cluster, contact production models, deploy a Worker or publish assets.

The CI workflow repeats Rust build/tests/Clippy/format checks on Linux and macOS
and runs browser, Worker and Python fixtures on Linux. It has read-only repository
permissions and no deployment jobs or credentials.

## Core dependency updates

Change the exact `opaque-core` Git `rev` deliberately in the root `Cargo.toml`,
regenerate `Cargo.lock` with Cargo, and rerun the complete checks above. Review
contract changes and the dependency diff together. This repository consumes the
public core's published contracts; enterprise implementations and private
validation records do not become transitive core dependencies.

## Contents

- `crates/opaque-showcase` — tenant-scoped OAuth MCP metrics gateway and chat UI.
- `assets/brand` — reviewed shared visual assets with their own provenance.
- `deploy/hosted-demo` — demo-specific Kubernetes runtime/controller and fixtures.
- `deploy/cloudflare-demo` — public-facing Worker source, static UI and fixtures.
- `examples/gpu-showcase`, `examples/metrics-chat` — demo workflow walkthroughs.
- `scripts` — demo driving tools, acceptance fixtures and artifact privacy checks.

Standalone compilation does not qualify a production deployment. OAuth/native
review pilots, cluster custody, operational acceptance and the separate security
branch remain independently tracked work. Keep runtime credentials, generated
binaries and session artifacts out of source control. Publication or cluster
changes require their own authorized deployment workflow.
