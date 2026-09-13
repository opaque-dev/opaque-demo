FROM debian:bookworm-slim
RUN apt-get update && apt-get install --no-install-recommends -y python3 openssl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/opaque
# Build with the reviewed source checkout as context and the separately generated
# artifact directory as a named BuildKit context. Nothing is deployed by building.
COPY --from=demo_artifact / /opt/opaque/artifact/
COPY scripts/demo_artifacts.py scripts/build_hosted_artifact.py scripts/check_site_privacy.py /opt/opaque/verify/
RUN python3 -B /opt/opaque/verify/build_hosted_artifact.py verify --directory /opt/opaque/artifact --require-linux \
 && mkdir -p /opt/opaque/bin /opt/opaque/deploy/hosted-demo /opt/opaque/scripts \
 && cp /opt/opaque/artifact/opaque-showcase /opt/opaque/bin/opaque-showcase \
 && cp /opt/opaque/artifact/deploy/hosted-demo/*.py /opt/opaque/deploy/hosted-demo/ \
 && cp /opt/opaque/artifact/scripts/metrics_chat_dogfood.py /opt/opaque/scripts/metrics_chat_dogfood.py \
 && /opt/opaque/bin/opaque-showcase --help >/dev/null
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 7383:7383
EXPOSE 8080
ENTRYPOINT ["python3", "/opt/opaque/deploy/hosted-demo/runtime.py"]
