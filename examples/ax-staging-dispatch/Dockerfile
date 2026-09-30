# Context: pinned public Opaque archive, pinned AX binary/license, this example.
FROM rust@sha256:d7482085ff5b415f84dba5647ae71606650bdef00db7aeb69f4b3d170c3e4082 AS build
WORKDIR /source
COPY source/ /source/
ARG OPAQUE_BUILD_REVISION
RUN OPAQUE_BUILD_REVISION="$OPAQUE_BUILD_REVISION" cargo build --locked -p opaqued -p opaque --bin opaqued --bin opaque --bin opaque-evidence

FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN pip install --no-cache-dir PyYAML==6.0.3 cryptography==46.0.4 \
    && groupadd --gid 7581 opaque-broker && groupadd --gid 7582 opaque-agent \
    && groupadd --gid 7987 opaque-clients \
    && useradd --no-log-init --uid 7581 --gid 7581 --home-dir /var/lib/opaque --no-create-home --shell /usr/sbin/nologin opaque-broker \
    && useradd --no-log-init --uid 7582 --gid 7582 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin opaque-agent \
    && install -d -m 0755 /opt/opaque/ax-scope
COPY --from=build /source/target/debug/opaqued /source/target/debug/opaque /source/target/debug/opaque-evidence /opt/opaque/
# demo.py builds under umask 077; the non-root runner must still read and execute these.
# The ax-scope directory exists before its COPY because --chmod also applies to a directory COPY creates.
COPY --chmod=0644 --from=build /source/examples/ax-scope/adapter.py /opt/opaque/ax-scope/adapter.py
COPY --chmod=0644 --from=build /source/LICENSE /opt/opaque/LICENSE
COPY --chmod=0755 ax-task-runner /usr/local/bin/ax-task-runner
COPY --chmod=0644 ax-LICENSE /opt/opaque/AX-LICENSE
COPY demo/ /opt/demo/
ARG OPAQUE_BUILD_REVISION
ARG AX_REVISION
ENV OPAQUE_CORE_REVISION=$OPAQUE_BUILD_REVISION
ENV AX_REVISION=$AX_REVISION
LABEL org.opencontainers.image.revision=$OPAQUE_BUILD_REVISION io.opaque.ax.revision=$AX_REVISION io.opaque.demo.kind=staging-dispatch-broker
CMD ["sleep", "infinity"]
