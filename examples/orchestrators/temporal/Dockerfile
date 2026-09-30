ARG OPAQUE_IMAGE=opaque-orchestrators-broker:local
FROM ${OPAQUE_IMAGE}
RUN pip install --no-cache-dir temporalio==1.33.0
COPY shared/ /opt/opaque-demo/shared/
COPY temporal/*.py /opt/opaque-demo/temporal/
ENV PYTHONPATH=/opt/opaque-demo/shared:/opt/opaque-demo/temporal
USER 7582:7582
CMD ["python3", "-B", "/opt/opaque-demo/temporal/worker.py"]
