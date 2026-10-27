# refkv is installed from git and built from source, which needs a Rust toolchain and git:
# build the environment in a stage of its own, and copy it into a slim image
FROM rust:1.90-slim-trixie AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

# Python with a precompiled standard library, for faster cold starts
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.14 --no-bin --compile-bytecode --no-cache

WORKDIR /app
COPY serve_refkv_minimal.py serve_refkv_minimal.py.lock ./

# Install the locked dependencies (update with: uv lock --script serve_refkv_minimal.py)
RUN uv venv /opt/venv --python 3.14 --managed-python \
    && VIRTUAL_ENV=/opt/venv uv sync --script serve_refkv_minimal.py --active --locked \
        --compile-bytecode --no-cache


FROM debian:trixie-slim

# the same paths as in the build stage: the environment's interpreter links to the Python
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY serve_refkv_minimal.py ./

# Fail the build if the install is broken
RUN python serve_refkv_minimal.py --help > /dev/null

# Defaults; override any REFKV_SERVE_<OPTION> at deploy time
ENV REFKV_SERVE_ROOT=/images \
    REFKV_SERVE_INDEX_DIR=/index \
    REFKV_SERVE_MAX_READS=64 \
    REFKV_SERVE_STATUS=false
RUN mkdir -p /images /index

ENV PYTHONUNBUFFERED=1

# exec so the server receives Cloud Run's SIGTERM
CMD ["sh", "-c", "exec python /app/serve_refkv_minimal.py --port \"${PORT:-8080}\""]
