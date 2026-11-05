#!/usr/bin/env python3
"""Adapts the nginx files of the official mempool website image for AVADO.

Run once at image build time. Every change is an exact text replacement that
must match exactly once, so a new mempool version with different files fails
the build instead of shipping a broken website.

- nginx runs as a normal user: pid, temp and cache files go to /tmp/avado.
- Two worker processes instead of one per CPU.
- No access logs; errors go to the container log.
- mempool's site file is read from /etc/mempool.
- No proxy to mempool.space (its paid services are switched off).
- The AVADO status page is served at /avado/, and /tmp/avado/ready.conf makes
  / open it until the explorer has data.
"""

import sys

MAIN = "/etc/mempool/nginx.conf"
SITE = "/etc/mempool/nginx-mempool.conf"

TEMP_PATHS = (
    "access_log off;\n"
    "\tclient_body_temp_path /tmp/avado/nginx/body;\n"
    "\tproxy_temp_path /tmp/avado/nginx/proxy;\n"
    "\tfastcgi_temp_path /tmp/avado/nginx/fastcgi;\n"
    "\tuwsgi_temp_path /tmp/avado/nginx/uwsgi;\n"
    "\tscgi_temp_path /tmp/avado/nginx/scgi;"
)

AVADO_LOCATION = (
    "\tindex index.html;\n"
    "\n"
    "\t# AVADO status page; / opens it until the explorer has data\n"
    "\tlocation /avado/ {\n"
    "\t\tproxy_pass http://127.0.0.1:8090/;\n"
    "\t\tproxy_set_header Host $host;\n"
    "\t\tproxy_set_header X-Avado $http_x_avado;\n"
    "\t}\n"
    "\tinclude /tmp/avado/ready.conf;"
)

CHANGES = {
    MAIN: [
        ("pid /var/run/nginx.pid;", "pid /tmp/avado/nginx.pid;"),
        ("worker_processes auto;", "worker_processes 2;"),
        ("access_log /var/log/nginx/access.log;", TEMP_PATHS),
        ("error_log /var/log/nginx/error.log;", "error_log stderr warn;"),
        ("proxy_cache_path /var/cache/nginx ", "proxy_cache_path /tmp/avado/nginx/cache "),
        ("include /etc/nginx/conf.d/nginx-mempool.conf;", "include /etc/mempool/nginx-mempool.conf;"),
        ("listen 0.0.0.0:80;", "listen 0.0.0.0:80;"),  # check only: the website is on port 80
    ],
    SITE: [
        ("access_log /var/log/nginx/access_mempool.log;", "access_log off;"),
        ("error_log /var/log/nginx/error_mempool.log;", "error_log stderr warn;"),
        ("\tlocation /api/v1/services {\n\t\tproxy_pass https://mempool.space;\n\t}",
         "\tlocation /api/v1/services {\n\t\treturn 404;\n\t}"),
        ("\tindex index.html;", AVADO_LOCATION),
        ("proxy_pass http://127.0.0.1:8999/api/v1;", "proxy_pass http://127.0.0.1:8999/api/v1;"),  # check only
    ],
}


def main():
    for path, changes in CHANGES.items():
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for old, new in changes:
            count = text.count(old)
            if count != 1:
                sys.exit("%s: expected %r once, found it %d times" % (path, old, count))
            text = text.replace(old, new)
        if "mempool.space" in text:
            sys.exit("%s: still talks to mempool.space" % path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("patched", path)


if __name__ == "__main__":
    main()
