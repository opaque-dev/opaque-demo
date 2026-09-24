# Run from this directory. OPAQUE and NATIVE_BIN must use the pinned core build.
OPAQUE ?= opaque
NATIVE_BIN ?= /tmp/opaque-ax-demo-core/target/debug
STATE ?= /tmp/opaque-ax-policy-session
IMAGE ?= opaque-ax-demo:policy
PYTHON ?= python3

.PHONY: policy deploy review serve inspect
policy:
	$(OPAQUE) authority-policy validate policies/support-status.yaml --json
	$(OPAQUE) authority-policy compile policies/support-status.yaml --json

deploy: policy
	$(PYTHON) -B ../../scripts/ax_minikube_demo.py --state $(STATE) deploy --image $(IMAGE) --native-bin $(NATIVE_BIN)

# This explicit operator command opens native authentication windows.
review:
	$(PYTHON) -B ../../scripts/ax_minikube_demo.py --state $(STATE) review

serve:
	$(PYTHON) -B ../../scripts/ax_minikube_demo.py --state $(STATE) serve

inspect:
	$(PYTHON) -B ../../scripts/ax_minikube_demo.py --state $(STATE) inspect
