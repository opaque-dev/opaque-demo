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
    && useradd --no-log-init --uid 7582 --gid 7582 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin opaque-agent
COPY --from=build /source/target/debug/opaqued /source/target/debug/opaque /source/target/debug/opaque-evidence /opt/opaque/
COPY --from=build /source/LICENSE /opt/opaque/LICENSE
COPY bootstrap/broker/install.sh /opt/demo/broker/install.sh
COPY bootstrap/fixtures/ /opt/demo/fixtures/
ARG OPAQUE_BUILD_REVISION
LABEL org.opencontainers.image.revision=$OPAQUE_BUILD_REVISION io.opaque.demo.kind=policy-broker
CMD ["sleep", "infinity"]
