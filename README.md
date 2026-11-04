# AVADO Bitcoin Explorer

An AVADO package that runs the [mempool](https://github.com/mempool/mempool)
explorer on the AVADO Bitcoin Node: current fees, waiting transactions and
new blocks, looked up on the owner's own node. The explorer is at
`http://mempool.my.ava.do`, the plain-language status page at
`http://mempool.my.ava.do/avado/` (and `/` opens the status page until the
explorer has data).

Needs the Bitcoin Node package (`bitcoincore.avado.dnp.dappnode.eth`, repo
AVADO-DNP-BitcoinCore) with the full history. Uses the Electrum Server package
for address look-ups when it is installed and ready.

## What it does

One container runs MariaDB (reachable only inside the container), the mempool
backend, nginx and a small Python supervisor:

- **Gets its own login from the Bitcoin Node** (`/internal/rpc-login/mempool`,
  answered only to this app's container), writes it into the backend's config
  (readable only by this app) and never shows it.
- **Switches to the Electrum Server by itself** when it is installed and
  answering, and back to node-only when it is removed.
- **Needs the full node.** mempool reads the transactions of every new block
  with `getrawtransaction`, which needs the node's transaction index
  (`txindex`); a pruned (space-saving) node cannot keep one. On a pruned node
  the explorer does not start: the page says "The Explorer needs the full node"
  and links to "Switch to the full node" on the Bitcoin Node page. When the node
  is full again, the explorer starts by itself.
- **No paid or external mempool.space services**: accelerator, Lightning and
  the services proxy are off (the services route answers 404). Only Bitcoin
  prices from public exchanges and the mining-pool list from GitHub are
  fetched.
- The database user is made on the box with a random password, passed to
  MariaDB through stdin; there is no database admin password.

No ports are published on the box.

## Image

The explorer is taken unchanged from the official `mempool/backend` and
`mempool/frontend` images (pinned by digest); only their settings and nginx
files are adapted at build time (`build/patch_nginx.py`: every change must
match exactly once, so a new mempool version with different files fails the
build). Runtime base is Debian bookworm with nginx and MariaDB from Debian.
Runs as an unprivileged user.

## Licence and reuse

GPL-3.0 (see LICENSE) for the files in this repository. mempool is AGPL-3.0,
Copyright The Mempool Open Source Project and contributors; it is used
unchanged and its source is at github.com/mempool/mempool.

## Development

```
cd build/app && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t mempool-test -f build/Dockerfile .
```

Release after the Bitcoin Node is published in the store (store dependency).
Merge with a merge commit, not squash.
