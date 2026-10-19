ARG PYTHON_IMAGE=mirror.gcr.io/python:3.12.8-slim
# fred's libraries: the locked, published ones (pypi), or local: the fred checkout
# passed as the `fred` build context (`FRED_LIBS=local make docker-build`;
# fred-deployment-factory's docs/LOCAL-DEVELOPMENT.md, "Work on unreleased fred").
ARG FRED_LIBS=pypi

FROM scratch AS fred-libs-pypi
FROM scratch AS fred-libs-local
COPY --from=fred libs /libs
FROM fred-libs-${FRED_LIBS} AS fred-libs

# ─── build ────────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

ARG KB=opentext
ARG UV_VERSION=0.7.21

ENV UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Pinned, so the resolver that built an image last month is the one that builds
# it today.
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh \
 && mv /root/.local/bin/uv /usr/local/bin/uv \
 && chmod +x /usr/local/bin/uv

WORKDIR /src

# The manifest alone first: editing the pod's code then costs a rebuild of
# the last layer rather than a re-resolution of every dependency.
COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --no-install-project

COPY . ./

# `--no-editable`, which is the whole difference between an image that runs and
# one that does not: uv installs a project in editable mode by default, and an
# editable install is a link back to the source tree it was built from. That
# tree is /src here and does not exist in the runtime stage, so the pod would
# start and fail on `No module named ...`. Installed for real, the package
# lives in the venv and the source tree stops mattering.
RUN uv sync --locked --no-dev --no-editable

# Empty unless FRED_LIBS=local: then fred's libraries from the checkout replace the
# locked ones, installed for real like the project above.
COPY --from=fred-libs / /fred/
RUN if [ -d /fred/libs ]; then \
      uv pip install --no-sources --python /app/venv/bin/python \
        /fred/libs/fred-pod "/fred/libs/fred-sdk[knowledge-base]"; \
    fi

# ─── runtime ──────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

ARG KB=opentext
ARG KB_PACKAGE=opentext_kb
ARG USER_NAME=fred-user
ARG USER_ID=1000
ARG GROUP_ID=1000

LABEL org.opencontainers.image.source="https://github.com/fred-agent/opentext-knowledge-base" \
      org.opencontainers.image.description="Fred Knowledge Base pod: an OpenText Content Server folder" \
      org.opencontainers.image.licenses="Apache-2.0"

# ca-certificates is what makes the corporate root a cluster injects into the
# system store land somewhere this pod can be pointed at. curl is not installed:
# the same question is answered from inside the pod by the pod's own
# developer tool, which uses the pod's trust store rather than a second one.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g ${GROUP_ID} ${USER_NAME} \
 && useradd -u ${USER_ID} -g ${GROUP_ID} -m ${USER_NAME}

COPY --from=builder --chown=${USER_ID}:${GROUP_ID} /app/venv /app/venv

# The pod's own configuration, and nothing else from the source tree: the
# package itself is in the venv. `config/configuration.yaml` is the path
# $CONFIG_FILE resolves to by default from this working directory, so the image
# runs as shipped and a deployment mounts a ConfigMap over it. The secrets file
# beside it is excluded by .dockerignore and never reaches a registry.
COPY --chown=${USER_ID}:${GROUP_ID} config /app/config

WORKDIR /app

ENV PATH="/app/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/${USER_NAME}

# Python reads no system trust store, so a Content Server signed by a corporate
# authority fails in this pod while curl against the same URL works. Defaulting
# to the image's own bundle makes the pod trust what the container trusts —
# including a root the cluster injected — and the pod ADDS this file to the
# public authorities rather than replacing them. Override it with the path of a
# mounted PEM when the root comes from a ConfigMap instead, or set it empty to
# trust public authorities only.
ENV OPENTEXT_KB_CA_FILE=/etc/ssl/certs/ca-certificates.crt

# `python -m <package>` spelled once, so `docker run <image> publish` and the
# `run` default below are the two commands the SDK actually offers. exec, so
# the pod receives the SIGTERM Kubernetes sends it.
RUN printf '#!/bin/sh\nexec python -m %s "$@"\n' "${KB_PACKAGE}" \
      > /usr/local/bin/knowledge-base \
 && chmod +x /usr/local/bin/knowledge-base

USER ${USER_NAME}

ENTRYPOINT ["knowledge-base"]
CMD ["run"]
