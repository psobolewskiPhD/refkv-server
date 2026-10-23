FROM debian:trixie-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

# Python with a precompiled standard library, for faster cold starts
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.14 --no-bin --compile-bytecode --no-cache

WORKDIR /app
COPY serve_refkv_minimal.py serve_refkv_minimal.py.lock ./

# Install the locked dependencies (update with: uv lock --script serve_refkv_minimal.py)
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN uv venv /opt/venv --python 3.14 --managed-python \
    && uv sync --script serve_refkv_minimal.py --active --locked --compile-bytecode --no-cache

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
