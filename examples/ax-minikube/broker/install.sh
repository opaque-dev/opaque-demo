#!/bin/sh
# Runs only in the trusted init container, before the broker process starts.
set -eu
install -d -o 7581 -g 7581 -m 0700 /state/custody
install -d -o 0 -g 0 -m 0700 /state/fixture
install -d -o 7581 -g 7987 -m 0750 /run/opaque
install -d -o 7581 -g 7581 -m 0700 /broker-tmp
install -d -o 7582 -g 7987 -m 0700 /human-tmp
python3 -B -c 'import sys; sys.path.insert(0,"/opt/demo/fixtures"); import services; from pathlib import Path; services.ROOT=Path("/state/fixture"); services.initialize()'
install -o 7581 -g 7581 -m 0600 /input/config.toml /state/custody/config.toml
if test -f /state/custody/tenant.binding.json; then
  install -d -o 7581 -g 7581 -m 0700 /state/custody/.opaque
fi
# Fresh tenant custody must contain only its sealed config until the broker
# creates its immutable tenant binding. Install provider files at policy activation.
if grep -q '^\[authority_policy\]' /input/config.toml; then
  test -f /state/custody/tenant.binding.json
  install -o 7581 -g 7581 -m 0600 /state/fixture/provider.token /state/custody/provider.token
  install -o 7581 -g 7581 -m 0600 /state/fixture/provider-ca.pem /state/custody/provider-ca.pem
fi
# The second mount exposes this same custody directory at its final path.
setpriv --reset-env --reuid=7581 --regid=7581 --clear-groups env \
  OPAQUE_CONFIG=/var/lib/opaque/config.toml /opt/opaque/opaque setup --seal
