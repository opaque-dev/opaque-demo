# Build context: exact public Opaque archive, cross-compiled pinned AX runner,
# and this worker only. Build instructions are in README.md.
FROM rust@sha256:d7482085ff5b415f84dba5647ae71606650bdef00db7aeb69f4b3d170c3e4082 AS build
WORKDIR /source
COPY source/ /source/
ARG OPAQUE_BUILD_REVISION
RUN OPAQUE_BUILD_REVISION="$OPAQUE_BUILD_REVISION" cargo build --locked -p opaqued -p opaque --bin opaqued --bin opaque --bin opaque-evidence
RUN OPAQUE_BUILD_REVISION="$OPAQUE_BUILD_REVISION" cargo build --locked -p opaque --example scope-recovery

FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
RUN pip install --no-cache-dir PyYAML==6.0.3 \
    && mkdir /ax /workspace && chown 7582:7582 /ax /workspace
COPY --from=build /source/target/debug/examples/scope-recovery /source/target/debug/opaque-evidence /opt/opaque/
COPY --from=build /source/examples/ax-scope/ /opt/opaque/ax-scope/
COPY --from=build /source/LICENSE /opt/opaque/LICENSE
COPY ax-task-runner /usr/local/bin/ax-task-runner
COPY ax-LICENSE /opt/opaque/AX-LICENSE
COPY worker.py /opt/opaque/worker.py
ARG OPAQUE_BUILD_REVISION
ARG AX_REVISION
ENV OPAQUE_CORE_REVISION=$OPAQUE_BUILD_REVISION
LABEL org.opencontainers.image.revision=$OPAQUE_BUILD_REVISION io.opaque.ax.revision=$AX_REVISION
USER 7582:7582
ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
