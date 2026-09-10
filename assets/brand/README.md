# Pinned Opaque brand assets

This directory vendors the shared brand foundation from
[`kcirtapfromspace/opaque`](https://github.com/kcirtapfromspace/opaque),
`assets/brand`. The palette, typography, glyph, embedded allowlist and upstream
font manifest are shared with the core product. Components and interaction
styles remain in the showcase. `provenance.json` records the canonical revision
and exact vendored file hashes; `manifest.json` records the ten public assets.

`opaque.css` defaults to dark. `data-op-color-scheme="light"` selects the paper
palette used by the credit portfolio experience. Fonts are served locally,
without third-party browser requests.

The six font files are unmodified TrueType files from the official Google Fonts
repository, pinned to commit `8e44913e4ff26fc997e6856c1ec40ff4791c98c5`.
Archivo supplies variable normal and italic faces; IBM Plex Mono supplies
regular and bold in both styles. Both families use the SIL Open Font License.
Their original license files remain byte-for-byte unchanged, including line
endings, and are served alongside the fonts.

The Rust server embeds only the ten explicit `embedded.rs` entries under
`/brand/`. README, manifest, provenance and Rust source are not browser assets.
The gateway brand test verifies exact response bytes, MIME types, cache and
security headers, disallowed paths, origin/host checks and API authentication.
Do not replace the allowlist with directory serving.

For updates, select an explicit core revision, copy the canonical public assets,
`embedded.rs` and `manifest.json`, verify every recorded hash, and update
`provenance.json`. Run the showcase gateway and UI tests before publication.
The demo repository's existing standalone Cargo workspace/dependency gap still
applies; vendoring static assets does not resolve it.
