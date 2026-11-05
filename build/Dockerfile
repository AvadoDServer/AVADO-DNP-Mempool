# AVADO Bitcoin Explorer image: the mempool explorer (backend + website), its
# database (MariaDB) and the AVADO status page, in one container.
#
# The explorer itself is taken unchanged from the official mempool images
# (AGPL-3.0, source: github.com/mempool/mempool); only their settings and the
# nginx configuration are adapted here, at build time.
#
# Build context is the repository root (docker-compose.yml: context ".",
# dockerfile "build/Dockerfile"). See /.dockerignore for what is sent.

# Images pinned by digest (multi-arch index digests). Update tag and digest
# together: `docker buildx imagetools inspect <image>:<tag>` ("Digest:" line).
# Backend and frontend must be the same mempool version.
ARG MEMPOOL_BACKEND=mempool/backend:v3.3.2@sha256:c358a20591285f77fb11e6f2502441e6b15bc7dd8282433e09bfd948b8ab0aa8
ARG MEMPOOL_FRONTEND=mempool/frontend:v3.3.2@sha256:83c5d1e20cd4fad358584f5921ca547d69ab80162d0e2f3138c2d142a91dbadd
ARG DEBIAN_IMAGE=debian:bookworm-20261027-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

FROM --platform=linux/amd64 ${MEMPOOL_BACKEND} AS backend

########## Website: let mempool's own start script write its settings once
FROM --platform=linux/amd64 ${MEMPOOL_FRONTEND} AS frontend
# Bitcoin mainnet only; no paid mempool.space services (accelerator), no
# Lightning. The website talks to the backend on 127.0.0.1:8999.
RUN MAINNET_ENABLED=true TESTNET_ENABLED=false TESTNET4_ENABLED=false SIGNET_ENABLED=false \
      REGTEST_ENABLED=false LIQUID_ENABLED=false LIQUID_TESTNET_ENABLED=false LIGHTNING=false \
      ACCELERATOR=false ACCELERATOR_BUTTON=false PUBLIC_ACCELERATIONS=false SERVICES_API="" \
      STRATUM_ENABLED=false AUDIT=false MINING_DASHBOARD=true HISTORICAL_PRICE=true \
      FRONTEND_HTTP_PORT=80 BACKEND_MAINNET_HTTP_HOST=127.0.0.1 BACKEND_MAINNET_HTTP_PORT=8999 \
      /patch/entrypoint.sh true \
    && grep -q "MAINNET_ENABLED = true" /var/www/mempool/browser/resources/config.js \
    && grep -q "ACCELERATOR = false" /var/www/mempool/browser/resources/config.js \
    && grep -q "listen 0.0.0.0:80;" /etc/nginx/nginx.conf \
    && grep -q "proxy_pass http://127.0.0.1:8999/api/v1;" /etc/nginx/conf.d/nginx-mempool.conf

########## Runtime
FROM --platform=linux/amd64 ${DEBIAN_IMAGE}
# nginx: the website. mariadb-server: the explorer's database. python3: the
# AVADO page and supervisor. libatomic1/libstdc++6: needed by mempool's node.
# setcap (libcap2-bin) lets nginx open port 80 without root. libcap2-bin stays:
# removing it would remove nginx too (nginx-common needs it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx-light mariadb-server mariadb-client python3 tini \
         libatomic1 libstdc++6 ca-certificates libcap2-bin \
    && setcap cap_net_bind_service=+ep /usr/sbin/nginx \
    && getcap /usr/sbin/nginx | grep -q cap_net_bind_service \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled /var/lib/mysql/*

COPY --from=backend /usr/bin/node /usr/bin/node
COPY --from=backend /backend/package /backend/package
COPY --from=frontend /var/www/mempool /var/www/mempool
COPY --from=frontend /etc/nginx/nginx.conf /etc/mempool/nginx.conf
COPY --from=frontend /etc/nginx/conf.d/nginx-mempool.conf /etc/mempool/nginx-mempool.conf

# nginx as a normal user, the AVADO page at /avado/ and no proxy to
# mempool.space: see build/patch_nginx.py. Each change must match exactly, so a
# new mempool version with different files fails the build instead of
# shipping a broken website.
COPY build/patch_nginx.py /tmp/patch_nginx.py
RUN python3 /tmp/patch_nginx.py && rm /tmp/patch_nginx.py \
    && mkdir -p /tmp/avado/nginx && echo > /tmp/avado/ready.conf \
    && nginx -t -c /etc/mempool/nginx.conf && rm -rf /tmp/avado

COPY build/app /app
COPY dappnode_package.json /tmp/dappnode_package.json
RUN python3 -c 'import json,re; v=json.load(open("/tmp/dappnode_package.json"))["version"]; \
assert re.fullmatch(r"[0-9A-Za-z.+-]+", v), "bad package version"; open("/app/package-version","w").write(v+"\n")' \
    && rm /tmp/dappnode_package.json \
    && cd /app && python3 -m unittest discover -s tests -q \
    && rm -rf /app/tests /app/__pycache__ \
    && node -e 'process.exit(0)' && test -f /backend/package/index.js \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin mempool \
    && mkdir -p /data && chown mempool:mempool /data

USER mempool
ENV DATA_DIR=/data USER=mempool PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NODE_ENV=production
WORKDIR /backend/package
VOLUME /data
EXPOSE 80
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/main.py"]
