ARG OPAQUE_IMAGE=opaque-orchestrators-broker:local
FROM ${OPAQUE_IMAGE} AS opaque
FROM apache/airflow@sha256:9df9c8be4096b9cc626bd7cb1f2b8c712eef66c59c615f8c7e6200871ca10bd1
USER root
RUN useradd --uid 7582 --gid 0 --home-dir /opt/airflow --no-create-home opaque-workflow
COPY --from=opaque /opt/opaque/opaque /opt/opaque/opaque
COPY --from=opaque /opt/opaque/LICENSE /opt/opaque/LICENSE
COPY --from=opaque /opt/demo/fixtures/client.py /opt/demo/fixtures/client.py
COPY shared/ /opt/opaque-demo/shared/
COPY airflow/dags/ /opt/opaque-demo/dags/
ENV PYTHONPATH=/opt/opaque-demo/shared:/home/airflow/.local/lib/python3.12/site-packages
ENV AIRFLOW__CORE__DAGS_FOLDER=/opt/opaque-demo/dags
ENV AIRFLOW__CORE__LOAD_EXAMPLES=False
ENV AIRFLOW__CORE__EXECUTOR=LocalExecutor
ENV AIRFLOW__CORE__PARALLELISM=2
ENV AIRFLOW__API__WORKERS=1
ENV AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=reviewer:admin
ENV AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=False
ENV AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK=True
ENV AIRFLOW__LOGGING__LOGGING_LEVEL=WARNING
ENV HOME=/opt/airflow
USER 7582:0
ENTRYPOINT []
CMD ["airflow", "standalone"]
