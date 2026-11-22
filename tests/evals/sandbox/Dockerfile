# The image of both sandbox containers (agent and proxy). Built by run.py from an explicit list of files.
FROM node:24.15.0-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/evals
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund && npm cache clean --force
COPY sandbox/ /opt/sandbox/
RUN chmod -R a+rX,go-w /opt/sandbox \
 && mkdir -p /run/proxy /sandbox \
 && chown 10001:10001 /run/proxy
USER 10001:10001
