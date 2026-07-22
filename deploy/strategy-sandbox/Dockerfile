ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN useradd --system --uid 65532 --no-create-home strategy-worker
WORKDIR /opt/strategy-worker
COPY --chown=65532:65532 worker.py /opt/strategy-worker/run.py
USER 65532:65532
ENTRYPOINT ["python", "-I", "-S"]
