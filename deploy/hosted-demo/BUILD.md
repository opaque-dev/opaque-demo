# Build a source-bound hosted artifact

The hosted image uses a separately generated payload instead of an undeclared
`deploy/hosted-demo/bin/opaque-showcase`. Build from this standalone private
repository at a reviewed full commit revision:

```sh
python3 -B scripts/build_hosted_artifact.py build \
  --demo-revision FULL_40_CHARACTER_DEMO_REVISION \
  --output /private/build/opaque-demo --profile release
python3 -B scripts/build_hosted_artifact.py verify \
  --directory /private/build/opaque-demo/payload
```

The build records the demo revision and tracked/untracked source digest, the
`opaque-core` contract revision verified against both Cargo.toml and Cargo.lock,
target triple, binary hash, runtime/Worker code hashes, and hashes of the complete
public asset directory after its privacy gate. Source changes during compilation
or packaging fail. Dirty source is accepted only with `--allow-dirty`, remains
explicitly labeled and does not become a release claim. Generated output stays
outside source control. The manifest is local build provenance, not an external
signed attestation or evidence of deployment.

For native setup checks, `--profile debug` is faster. The Linux image requires a
Linux ELF binary compatible with Debian bookworm's loader/libraries. Build in
that environment, or use a configured compatible cross toolchain with `--target`.
A native macOS build is useful for fixture checks and is rejected for this image.

To build the image, provide only the `payload` directory
as a named BuildKit context:

```sh
docker buildx build --file deploy/hosted-demo/Dockerfile \
  --build-context demo_artifact=/private/build/opaque-demo/payload \
  --tag opaque-demo:local .
```

The Dockerfile verifies every recorded payload, rejects extra files, checks the
Linux format, and runs `opaque-showcase --help` to catch missing runtime libraries.
It retains `/opt/opaque/artifact/artifact-provenance.json` in the image. The build
command above does not push or deploy. Record the resulting immutable image
digest alongside this manifest when qualifying a hosted runtime.

## Public Worker artifact

Wrangler's build hook runs `scripts/build_worker_site.py` and publishes only
`deploy/cloudflare-demo/.public-artifact`. The generator copies the two reviewed
public HTML routes, scans their contents and checks the entire resulting tree.
Private docs, config, runtime reports, search exports and unknown assets are not
publication inputs. Adding a page requires reviewing the independent allowlist.
Unexpected content in an existing output is preserved and causes failure.

```sh
python3 -B scripts/check_site_privacy.py --build
python3 -B scripts/build_worker_site.py
python3 -B scripts/check_site_privacy.py \
  --site-dir deploy/cloudflare-demo/.public-artifact
```

The first command injects private-source canaries and checks the actual generated
artifact in temporary storage. The `--profile docs` checker remains available
for an explicitly supplied external MkDocs artifact; the standalone demo has no
MkDocs source tree. Wrangler's custom-build and assets configuration follow the
[Cloudflare build contract](https://developers.cloudflare.com/workers/wrangler/custom-builds/).

## Acceptance still required on the hosted environment

Before updating a hosted image or Worker, verify the exact artifact manifest,
image digest and Worker version together. Exercise the real login path, a
permitted model request, source-enforced tenant denial, expiry/revocation and
unknown/retry behavior. Observe the result independently. Hosted portfolio data
and the local metrics issuer remain explicitly synthetic; they are not proof of
customer-source custody or canonical broker task approval. For a real GitHub
source and broker receipts, use `examples/github-ci` with its separately configured
core broker and native approval. No local build or CI fixture performs these live
acceptance steps automatically.
