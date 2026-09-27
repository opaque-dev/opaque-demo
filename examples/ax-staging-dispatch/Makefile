# Run from this directory. OPAQUE and NATIVE_BIN must use the pinned core build.
PYTHON ?= python3
WORK ?= /private/tmp/opaque-dispatch-work
OPAQUE ?= $(WORK)/core/target/debug/opaque
NATIVE_BIN ?= $(WORK)/core/target/debug
STATE ?= /private/tmp/opaque-dispatch-session
IMAGE ?= opaque-ax-staging-dispatch:2109af7
TOKEN_FILE ?= /private/tmp/opaque-dispatch-github.token

.PHONY: policy build deploy delegate-reviewer delegate-requester scope-review run run-sigkill evidence serve inspect test
policy:
	$(OPAQUE) authority-policy validate policies/staging-dispatch.yaml --json
	$(OPAQUE) authority-policy compile policies/staging-dispatch.yaml --json

build:
	$(PYTHON) -B demo.py build --work $(WORK) --image $(IMAGE)

deploy: policy
	$(PYTHON) -B demo.py --state $(STATE) deploy --image $(IMAGE) --native-bin $(NATIVE_BIN) --github-token-file $(TOKEN_FILE)

# Each of the next three targets opens exactly one native authentication window.
delegate-reviewer:
	$(PYTHON) -B demo.py --state $(STATE) delegate-reviewer
delegate-requester:
	$(PYTHON) -B demo.py --state $(STATE) delegate-requester
scope-review:
	$(PYTHON) -B demo.py --state $(STATE) scope-review

run:
	$(PYTHON) -B demo.py --state $(STATE) run
run-sigkill:
	$(PYTHON) -B demo.py --state $(STATE) run --variant sigkill-second-dispatch
evidence:
	$(PYTHON) -B demo.py --state $(STATE) evidence
serve:
	$(PYTHON) -B demo.py --state $(STATE) serve
inspect:
	$(PYTHON) -B demo.py --state $(STATE) inspect
test:
	cd ../.. && $(PYTHON) -B -m unittest discover -s scripts -p test_ax_staging_dispatch_demo.py -v
	node --check demo.js
