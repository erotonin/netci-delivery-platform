# Built on the netCI golden base, which is where patching happens. This image only
# adds application code, so it needs no RUN step and therefore no user namespace on
# the build agent -- see sample-apps/base-python/Dockerfile.
ARG PYTHON_IMAGE=netci/python-base:3.12-alpine
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080
WORKDIR /app
# No --chown: the file only needs to be world-readable, which is the default, and
# changing ownership requires a uid map the build agent may not be able to create.
COPY app.py /app/app.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "/app/app.py"]
