# opaque-staging-scratch

Private scratch repository that is the dispatch target of the Opaque
`examples/ax-staging-dispatch` demo. It has no product code.

- `.github/workflows/staging.yml` is the only workflow the demo's AuthorityPolicy
  allows. It runs on `workflow_dispatch`, takes one optional input, and echoes the
  run id, run number, ref, commit and actor. The Opaque broker dispatches it with
  `{"ref": "main"}` and no inputs.
- `.github/workflows/production.yml` exists only so the demo can propose an
  out-of-scope target that really exists. The policy never lists it, so the broker
  refuses that proposal before sending any GitHub request. If this workflow ever
  shows a run, the demo failed.

## Reading the Actions tab

Each brokered dispatch that GitHub acknowledged with `204` appears here as one
`staging` run started by the token's owner. The broker sends no inputs, so runs are
correlated to Opaque actions by branch and time only. An Opaque outcome of
`api_accepted` means GitHub accepted the request; it does not mean the run
completed.

## Direct validity check

One `staging` run was started directly with `gh workflow run` when this repository
was created, to prove the workflow file is valid and dispatchable. That run is a
validity check of the target, not a demo step, and no Opaque broker was involved.
Runs after it come from the demo unless their note says otherwise.

## Token

The demo's broker needs a fine-grained personal access token restricted to this
repository with Actions: read and write, Contents: read, Metadata: read. The token
lives only in the broker's private custody file. Never commit it anywhere.
