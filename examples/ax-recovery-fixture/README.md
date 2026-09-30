# Inspect Opaque recovery inside an AX runner on minikube

This local browser demo runs the actual Google AX task runner and Opaque's
public scope-recovery example inside a Kubernetes pod. Start the experiment,
inspect its retained outcomes, and replace the runner pod to verify that the
same persistent volume preserves logical action identity and evidence.

**Boundary:** the scope review signatures and provider effects are synthetic.
There is no native human approval, live broker RPC, model call, live provider,
AX control plane, or Agent Substrate. Kubernetes recreates the runner pod;
this does not qualify Substrate actor scheduling or suspend/resume. The browser
button starts the experiment and is not an authorization ceremony. The process
kill, ledger, runner child command, metadata endpoint, public verifier, pod
replacement, and volume persistence execute actual code.

## Build from pinned sources

Prerequisites: Docker, minikube, Python 3.12+, Git, Go 1.27.1+, and network access
for build dependencies. The demonstrated host is macOS arm64 with Docker Desktop;
the container sources also support a Linux arm64 or amd64 build. Only arm64 was
executed here. Allow 4 CPUs and 8 GiB for the isolated minikube profile.

Clone the public source repositories into new paths. From this demo checkout:

```sh
git clone https://github.com/opaque-dev/opaque.git /tmp/opaque-ax-demo-core
git clone https://github.com/google/ax.git /tmp/opaque-ax-demo-ax
git -C /tmp/opaque-ax-demo-ax checkout --detach f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c
AX_DEMO_BUILD=$(mktemp -d /tmp/opaque-ax-build.XXXXXX)
mkdir "$AX_DEMO_BUILD/source"
git -C /tmp/opaque-ax-demo-core archive 180e66fe6c854d962074e8ff4e694a29689af623 | tar -xf - -C "$AX_DEMO_BUILD/source"
cp examples/ax-recovery-fixture/Dockerfile examples/ax-recovery-fixture/worker.py "$AX_DEMO_BUILD/"
cp /tmp/opaque-ax-demo-ax/LICENSE "$AX_DEMO_BUILD/ax-LICENSE"
# Use GOARCH=amd64 on an amd64 minikube node.
(cd /tmp/opaque-ax-demo-ax && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -o "$AX_DEMO_BUILD/ax-task-runner" ./cmd/ax-task-runner)
docker build --build-arg OPAQUE_BUILD_REVISION=180e66fe6c854d962074e8ff4e694a29689af623 \
  --build-arg AX_REVISION=f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c \
  -t opaque-ax-demo:local "$AX_DEMO_BUILD"
minikube start -p opaque-ax-demo --driver=docker --cpus=4 --memory=8192 \
  --disk-size=30g --kubernetes-version=v1.35.1 --keep-context
python3 -B scripts/ax_recovery_demo.py --state /tmp/opaque-ax-demo-session \
  deploy --image opaque-ax-demo:local
python3 -B scripts/ax_recovery_demo.py --state /tmp/opaque-ax-demo-session serve
```

Open `http://127.0.0.1:19740/`. Use an unused runtime directory outside Git.
`deploy` creates a fresh owned namespace, a 1 GiB PVC, and a single runner
deployment. Every kubectl call explicitly selects `opaque-ax-demo` through
minikube; the default context is not used. No image is pushed to a registry.

This exact public Opaque revision retains BUSL-1.1. The proposed Apache-2.0
licensing change is separate and does not relabel this pinned image. AX uses
Apache-2.0. Both source license files are included in the image.

## Walk through the experience

1. Read the scenario and its synthetic review boundary. Loading the page performs
   no execution. The displayed counts remain unavailable until the run finishes.
2. Choose **Run failure experiment**. Sixteen concurrent proposals share four
   attempts. The example exercises real SIGKILL and scope revocation, then verifies
   the signed export. Expect four charged attempts, one synthetic API acceptance,
   three unknowns, and twelve budget denials. Every action reports no retry authority.
3. Choose **Replace runner pod**. Observe a different pod UID with the same logical
   run ID, action evidence, checkpoint and producer log. The new AX child reads the
   existing result. A local verification record confirms no producer repetition.
4. Expand **Observed verification checks and deployment boundary**. Invalid export
   bytes and substituted effects must be rejected. Review signatures prove the
   synthetic historical bindings, not human presence.

The UI intentionally offers no reset or retry button for an uncertain experiment.
An execution failure is held on the persistent volume. A new experiment requires
a new namespace/session directory. Replacing the pod never erases prior evidence.

The twelve denied proposals have no charged action in the selected export; the
adapter reports `not_observed` and retains a hold. The denials are established by
the complete synthetic producer experiment, not inferred from missing evidence.

## Inspect and stop

```sh
python3 -B scripts/ax_recovery_demo.py --state /tmp/opaque-ax-demo-session inspect
```

The host state directory retains `cluster.json`, `last-view.json`, and, after
replacement, `restart-verification.json`. The volume holds generated signing keys,
the ledger and full experiment records. Do not commit or publish the volume or
runtime directory. The HTTP server serves an explicit asset list and the selected
view only; it cannot browse the volume or source tree. It binds to IPv4 loopback,
checks Host and Origin for mutations, and accepts only run/restart actions.

Ctrl-C stops the browser server. `minikube stop -p opaque-ax-demo` stops this
dedicated cluster and retains its storage for later inspection. Namespace/PVC
deletion destroys the retained experiment; it is deliberately a separate operator
decision. The demo does not alter the existing portfolio or native-review fixtures.

The runner and synthetic producer share a pod and UID. This is not a credential
isolation or independently administered custody demonstration. Minikube's default
network is not claimed to enforce an egress policy. A historical signed checkpoint
does not prove complete history, current authority or business completion.

## Verification

```sh
python3 -B -m unittest discover -s scripts -p test_ax_minikube_demo.py -v
node --check examples/ax-recovery-fixture/demo.js
```

The HTTP tests cover read-only loading, same-origin mutation, private-path refusal,
and refusal to dispatch a completed or uncertain run again. A passing test suite
does not substitute for the browser/cluster walkthrough above.
