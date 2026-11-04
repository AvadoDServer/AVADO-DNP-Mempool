#!/usr/bin/env python3
"""AVADO Bitcoin Explorer: the mempool explorer on the AVADO Bitcoin Node.

Runs, in one container: MariaDB (the explorer's database, reachable only
inside this container), the mempool backend, nginx (the explorer website on
port 80) and this supervisor, whose plain-language page is at /avado/.

- Gets its own RPC login from the Bitcoin Node app (handed out only to this
  app's container). The login is written into the backend's config file,
  readable only by this app, and never shown.
- Uses the Electrum Server app for address and transaction look-ups when it is
  installed and ready; otherwise the backend talks to the node only.
- On a space-saving (pruned) node it limits the block statistics to the blocks
  the node still has.
- Until the explorer has data, http://mempool.my.ava.do/ opens the status page
  instead of an empty explorer.
"""

import collections
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import avadoweb
from avadoweb import log
from bitcoinrpc import RPC_IN_WARMUP, BitcoinRpc, RpcError, fetch_login

_HERE = os.path.dirname(os.path.abspath(__file__))
_VERSION_FILE = os.path.join(_HERE, "package-version")
VERSION = open(_VERSION_FILE).read().strip() if os.path.exists(_VERSION_FILE) else "dev"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_DIR = os.path.join(DATA_DIR, "mysql")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
STATE_DIR = os.path.join(DATA_DIR, "avado")
DB_LOGIN_FILE = os.path.join(STATE_DIR, "database.json")
BACKEND_CONF = os.path.join(STATE_DIR, "mempool-config.json")
RUN_DIR = os.environ.get("RUN_DIR", "/tmp/avado")
DB_SOCKET = os.path.join(RUN_DIR, "mysqld.sock")
READY_CONF = os.path.join(RUN_DIR, "ready.conf")
NGINX_CONF = os.environ.get("NGINX_CONF", "/etc/mempool/nginx.conf")
PUBLIC_DIR = os.path.join(_HERE, "public")
BACKEND_PORT = 8999
PAGE_PORT = 8090
DB_USER = "explorer"  # the explorer's database login; not the OS user
FULCRUM_HOST = "DAppNodePackage-fulcrum.avado.dnp.dappnode.eth"
CHAIN_TO_NETWORK = {"main": "mainnet", "test": "testnet", "testnet4": "testnet4", "signet": "signet", "regtest": "regtest"}
GB = 1000 ** 3


class Service:
    """One child process, restarted with a growing pause when it stops."""

    def __init__(self, name, args_fn, env=None, quiet=False):
        self.name = name
        self.args_fn = args_fn
        self.env = env
        self.quiet = quiet
        self.lock = threading.RLock()
        self.proc = None
        self.wanted = False
        self.lines = collections.deque(maxlen=60)
        self.started_at = None
        self.restarts = 0
        self.next_start = 0
        self.last_exit = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _reader(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.lines.append(line)
            if not self.quiet:
                print("[%s] %s" % (self.name, line), flush=True)

    def start(self):
        with self.lock:
            self.wanted = True
            if self.running():
                return
            log("starting", self.name)
            self.proc = subprocess.Popen(self.args_fn(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=self.env)
            self.started_at = time.time()
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def stop(self, timeout=30, sig=signal.SIGTERM):
        with self.lock:
            self.wanted = False
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.proc = None
                return
            log("stopping", self.name)
            proc.send_signal(sig)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None

    def restart(self):
        self.stop()
        self.next_start = 0
        self.start()

    def tick(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is not None:
                uptime = time.time() - (self.started_at or time.time())
                self.restarts = 0 if uptime > 600 else self.restarts + 1
                pause = min(300, 5 * (2 ** min(self.restarts, 6)))
                self.last_exit = {"code": self.proc.returncode, "at": time.time(), "pause": pause}
                log("%s stopped (exit %s); next try in %d s" % (self.name, self.proc.returncode, pause))
                self.proc = None
                self.next_start = time.time() + pause
            if self.wanted and self.proc is None and time.time() >= self.next_start:
                try:
                    self.start()
                except OSError as err:
                    log("cannot start %s: %s" % (self.name, err))
                    self.next_start = time.time() + 60


def backend_config(chain, rpc, electrum, db, indexing_blocks):
    """The mempool backend's settings; everything not given uses its defaults."""
    network = CHAIN_TO_NETWORK.get(chain, "mainnet")
    return {
        "MEMPOOL": {
            "NETWORK": network,
            "BACKEND": "electrum" if electrum else "none",
            "HTTP_PORT": BACKEND_PORT,
            "CACHE_DIR": CACHE_DIR,
            "INDEXING_BLOCKS_AMOUNT": indexing_blocks,
            "STDOUT_LOG_MIN_PRIORITY": "info",
            "AUTOMATIC_POOLS_UPDATE": True,
            "POOLS_JSON_URL": "https://raw.githubusercontent.com/mempool/mining-pools/master/pools-v2.json",
            "POOLS_JSON_TREE_URL": "https://api.github.com/repos/mempool/mining-pools/git/trees/master",
            "AUDIT": False,
            "RUST_GBT": True,
            "CPFP_INDEXING": False,
            "MAX_TRACKED_ADDRESSES": 1,
        },
        "CORE_RPC": {"HOST": rpc["host"], "PORT": rpc["port"], "USERNAME": rpc["user"],
                     "PASSWORD": rpc["password"], "TIMEOUT": 60000},
        "ELECTRUM": {"HOST": FULCRUM_HOST, "PORT": 50001, "TLS_ENABLED": False},
        "DATABASE": {"ENABLED": True, "HOST": "127.0.0.1", "PORT": 3306, "SOCKET": DB_SOCKET,
                     "DATABASE": "mempool", "USERNAME": DB_USER, "PASSWORD": db["password"],
                     # The backend's lock file; its own folder is read-only for this app.
                     "PID_DIR": RUN_DIR},
        "SYSLOG": {"ENABLED": False},
        "STATISTICS": {"ENABLED": True, "TX_PER_SECOND_SAMPLE_PERIOD": 150},
        "MAXMIND": {"ENABLED": False},
        "LIGHTNING": {"ENABLED": False},
        "REDIS": {"ENABLED": False},
        "MEMPOOL_SERVICES": {"ACCELERATIONS": False},
        "FIAT_PRICE": {"ENABLED": True, "PAID": False, "API_KEY": ""},
        "STRATUM": {"ENABLED": False},
    }


def indexing_amount(node):
    """How many recent blocks to index for the mining statistics. A pruned node
    has only its newest blocks; asking for older ones fails again and again."""
    if not node.get("pruned"):
        return 11000
    kept = int(node.get("blocks") or 0) - int(node.get("pruneheight") or 0)
    return max(0, min(11000, kept - 300))


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (local URL only)
        return json.loads(resp.read().decode("utf-8"))


ELECTRUM_HELLO = b'{"jsonrpc": "2.0", "id": 0, "method": "server.version", "params": ["avado-explorer", "1.4"]}\n'


def electrum_answers(host, port, timeout=3):
    """True when an Electrum server answers server.version on host:port.

    A bare TCP connect is not enough: the Electrum Server app's port gate accepts
    connections while Fulcrum itself is still waiting for the node, and then
    closes them, so the explorer would switch to it too early and log a
    connection error every second."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(ELECTRUM_HELLO)
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
        return isinstance(reply, dict) and reply.get("id") == 0 and "result" in reply
    except (OSError, ValueError):
        return False


class App:
    def __init__(self, env=os.environ):
        self.bitcoin_host = env.get("BITCOIN_HOST") or "DAppNodePackage-bitcoincore.avado.dnp.dappnode.eth"
        for d in (DB_DIR, CACHE_DIR, STATE_DIR, RUN_DIR, os.path.join(RUN_DIR, "nginx")):
            os.makedirs(d, exist_ok=True)
        self.db_login = avadoweb.read_json(DB_LOGIN_FILE)
        if not self.db_login or not self.db_login.get("password"):
            self.db_login = {"password": secrets.token_urlsafe(24)}
            avadoweb.write_json(DB_LOGIN_FILE, self.db_login)
        backend_env = dict(os.environ, MEMPOOL_CONFIG_FILE=BACKEND_CONF)
        self.mariadb = Service("database", self.mariadb_args, quiet=True)
        self.backend = Service("explorer", lambda: ["node", "/backend/package/index.js"], env=backend_env)
        self.nginx = Service("web", lambda: ["nginx", "-e", "stderr", "-c", NGINX_CONF, "-g", "daemon off;"])
        self.login = None
        self.login_at = 0
        self.login_error = None
        self.node = {}
        self.node_error = None
        self.electrum = False      # what the backend runs with now
        self.backend_key = None    # settings the backend was started with
        self.ready = None
        self.db_ready = False
        self.db_error = None
        self.cache = {"at": 0, "model": None}
        self.cache_lock = threading.Lock()

    # -- database
    def mariadb_args(self):
        return ["mariadbd", "--no-defaults", "--datadir=%s" % DB_DIR, "--socket=%s" % DB_SOCKET,
                "--pid-file=%s" % os.path.join(RUN_DIR, "mysqld.pid"), "--bind-address=127.0.0.1", "--port=3306",
                "--skip-name-resolve", "--innodb-buffer-pool-size=256M", "--max-connections=60",
                "--performance-schema=OFF", "--character-set-server=utf8mb4", "--log-error="]

    def init_database(self):
        if not os.path.exists(os.path.join(DB_DIR, "mysql")):
            log("creating the explorer database")
            subprocess.run(["mariadb-install-db", "--no-defaults", "--datadir=%s" % DB_DIR, "--skip-test-db",
                            "--auth-root-authentication-method=socket", "--user=%s" % os.environ.get("USER", "mempool")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.mariadb.start()
        for _ in range(120):
            if subprocess.run(["mariadb-admin", "--no-defaults", "--socket=%s" % DB_SOCKET, "ping"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("the database did not start")
        # The explorer logs in as "explorer" with a password. The OS user "mempool" keeps its
        # password-less socket login as the database owner (no admin password exists at all);
        # it must not be changed, or the next start could not log in.
        password = self.db_login["password"].replace("'", "")
        sql = ("CREATE DATABASE IF NOT EXISTS mempool; "
               "CREATE USER IF NOT EXISTS '{u}'@'localhost' IDENTIFIED BY '{p}'; "
               "CREATE USER IF NOT EXISTS '{u}'@'127.0.0.1' IDENTIFIED BY '{p}'; "
               "ALTER USER '{u}'@'localhost' IDENTIFIED BY '{p}'; "
               "ALTER USER '{u}'@'127.0.0.1' IDENTIFIED BY '{p}'; "
               "GRANT ALL PRIVILEGES ON mempool.* TO '{u}'@'localhost'; "
               "GRANT ALL PRIVILEGES ON mempool.* TO '{u}'@'127.0.0.1'; FLUSH PRIVILEGES;\n").format(u=DB_USER, p=password)
        # Through stdin, so the password is never in a process list or an error message.
        done = subprocess.run(["mariadb", "--no-defaults", "--socket=%s" % DB_SOCKET], input=sql.encode("utf-8"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if done.returncode != 0:
            raise RuntimeError("the database refused the explorer login: %s"
                               % done.stderr.decode("utf-8", "replace").replace(password, "***").strip())
        self.db_ready = True
        log("explorer database ready")

    def start_database_then_loop(self):
        """The page stays up while the database starts; a failure is retried, never fatal."""
        pause = 10
        while not self.db_ready:
            try:
                self.init_database()
            except (OSError, RuntimeError, subprocess.CalledProcessError) as err:
                self.db_error = str(err)
                log("explorer database not ready: %s; trying again in %d s" % (err, pause))
                time.sleep(pause)
                pause = min(300, pause * 2)
        self.db_error = None
        self.loop()

    # -- Bitcoin node
    def refresh_login(self, force=False):
        if self.login and not force and time.time() - self.login_at < 300:
            return self.login
        try:
            login = fetch_login(self.bitcoin_host, "mempool")
            changed = self.login is not None and self.login != login
            self.login, self.login_at, self.login_error = login, time.time(), None
            if changed:
                log("the Bitcoin Node app gave a new login")
                self.backend_key = None  # makes the loop restart the backend
        except RpcError as err:
            self.login_error = {"code": err.code, "text": str(err)}
        return self.login

    def check_node(self):
        login = self.refresh_login()
        if not login:
            self.node, self.node_error = {}, "unreachable"
            return
        try:
            self.node = BitcoinRpc(self.bitcoin_host, login[2], login[0], login[1], timeout=8).call("getblockchaininfo")
            self.node_error = None
        except RpcError as err:
            self.node = {}
            self.node_error = "warmup" if err.code == RPC_IN_WARMUP else ("login" if err.code == 401 else "unreachable")
            if err.code == 401:
                self.refresh_login(force=True)

    def fulcrum_state(self):
        """'missing' (not installed), 'ready' (answers wallets) or 'busy'."""
        if not avadoweb.resolve_ips(FULCRUM_HOST):
            return "missing"
        return "ready" if electrum_answers(FULCRUM_HOST, 50001) else "busy"

    # -- the loop
    def loop(self):
        while True:
            try:
                for svc in (self.mariadb, self.nginx, self.backend):
                    svc.tick()
                self.check_node()
                fulcrum = self.fulcrum_state()
                if fulcrum == "ready":
                    want_electrum = True
                elif fulcrum == "missing":
                    want_electrum = False
                else:
                    want_electrum = self.electrum  # keep what runs while the Electrum Server restarts
                if self.node and self.login and self.db_ready:
                    key = (want_electrum, self.login, self.node.get("chain"), indexing_amount(self.node) // 1000)
                    if key != self.backend_key:
                        conf = backend_config(self.node.get("chain"), {
                            "host": self.bitcoin_host, "port": self.login[2], "user": self.login[0],
                            "password": self.login[1]}, want_electrum, self.db_login, indexing_amount(self.node))
                        avadoweb.write_text(BACKEND_CONF, json.dumps(conf, indent=2), mode=0o600)
                        log("explorer settings: %s, %d blocks of statistics" % (
                            "with the Electrum Server" if want_electrum else "node only", indexing_amount(self.node)))
                        self.electrum = want_electrum
                        self.backend_key = key
                        self.backend.restart()
                self.update_ready()
            except Exception as err:  # keep the loop alive
                log("check failed:", repr(err))
            time.sleep(10)

    def explorer_height(self):
        if not self.backend.running():
            return None
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/v1/blocks/tip/height" % BACKEND_PORT, timeout=4) as r:
                return int(r.read().decode().strip())
        except (OSError, ValueError):
            return None

    def update_ready(self):
        node_synced = bool(self.node) and not self.node.get("initialblockdownload") and \
            float(self.node.get("verificationprogress") or 0) >= 0.9999
        height = self.explorer_height()
        ready = node_synced and height is not None and height >= int(self.node.get("blocks") or 0) - 2
        if ready != self.ready:
            # Until the explorer has data, the address opens the AVADO status page.
            avadoweb.write_text(READY_CONF, "" if ready else "location = / { return 302 /avado/; }\n", mode=0o644)
            if self.nginx.running():
                subprocess.run(["nginx", "-e", "stderr", "-c", NGINX_CONF, "-s", "reload"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            self.ready = ready
            log("explorer ready" if ready else "explorer not ready yet: / opens the status page")

    # -- page
    def gather(self):
        info = {"node": self.node, "nodeError": self.node_error, "loginError": self.login_error,
                "nodeInstalled": bool(avadoweb.resolve_ips(self.bitcoin_host)), "electrum": self.electrum,
                "fulcrum": self.fulcrum_state(), "backendRunning": self.backend.running(), "ready": bool(self.ready),
                "dbReady": self.db_ready, "dbError": self.db_error}
        info["free"], _ = avadoweb.disk_free(DATA_DIR)
        info["height"] = self.explorer_height()
        if info["height"] is not None:
            try:
                info["fees"] = http_json("http://127.0.0.1:%d/api/v1/fees/recommended" % BACKEND_PORT)
                info["mempool"] = http_json("http://127.0.0.1:%d/api/v1/mempool" % BACKEND_PORT)
            except (OSError, ValueError):
                pass
        return info

    def status(self):
        with self.cache_lock:
            if self.cache["model"] is not None and time.time() - self.cache["at"] < 4:
                return self.cache["model"]
        model = build_model(self.gather())
        with self.cache_lock:
            self.cache = {"at": time.time(), "model": model}
        return model

    def run(self):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        avadoweb.write_text(READY_CONF, "location = / { return 302 /avado/; }\n", mode=0o644)
        avadoweb.WebApp(PUBLIC_DIR, self.status, version=VERSION, title="Bitcoin Explorer").serve(
            port=PAGE_PORT, host="127.0.0.1")
        self.nginx.start()
        threading.Thread(target=self.start_database_then_loop, daemon=True).start()
        while True:
            time.sleep(3600)

    def shutdown(self, *_args):
        log("shutting down")
        self.backend.stop(timeout=5)
        self.nginx.stop(timeout=3, sig=signal.SIGQUIT)
        self.mariadb.stop(timeout=20)
        sys.exit(0)


# ---------------------------------------------------------------- page model

def fmt_int(n):
    return "{:,}".format(int(n))


def build_model(info):
    node = info.get("node") or {}
    model = {"title": "Bitcoin Explorer", "facts": [], "sections": [], "refresh": 10}
    progress = float(node.get("verificationprogress") or 0)
    ibd = bool(node) and (bool(node.get("initialblockdownload")) or progress < 0.9999)
    pruned = bool(node.get("pruned"))

    if not info.get("nodeInstalled"):
        model["state"] = "error"
        model["headline"] = "Install the Bitcoin Node first"
        model["text"] = ["The Bitcoin Explorer shows what your own Bitcoin node sees. Install \"Bitcoin Node\" from "
                         "the AVADO store; this page then updates by itself."]
    elif (info.get("loginError") or {}).get("code") == 403:
        model["state"] = "error"
        model["headline"] = "The Bitcoin Node does not let the explorer in"
        model["text"] = ["Restart the Bitcoin Node app and then this app in the AVADO Admin. If this stays, contact "
                         "AVADO support."]
    elif info.get("dbError"):
        model["state"] = "error"
        model["headline"] = "The explorer's database does not start"
        model["text"] = ["It tries again by itself. If this message stays for more than an hour, restart this app in "
                         "the AVADO Admin, or contact AVADO support."]
    elif not node:
        model["state"] = "working"
        model["headline"] = "Waiting for your Bitcoin node"
        model["text"] = ["Your Bitcoin node is starting. The explorer begins as soon as the node answers."]
    elif ibd:
        model["state"] = "working"
        model["headline"] = "Your Bitcoin node is still downloading the history"
        model["progress"] = {"percent": round(progress * 100, 2), "left": "Bitcoin node: %.1f%% done" % (progress * 100),
                             "right": "The explorer opens when the node is done"}
        model["text"] = ["The explorer shows blocks and fees once your node has the whole picture. You can close this "
                         "page; it opens the explorer by itself when it is ready."]
    elif not info.get("ready"):
        model["state"] = "working"
        model["headline"] = "The explorer is getting ready"
        model["text"] = ["It is reading the newest blocks and the waiting transactions from your node. This takes a few "
                         "minutes; the block statistics fill in over the next hours."]
    else:
        model["state"] = "ok"
        model["headline"] = "Your Bitcoin Explorer is ready"
        model["text"] = ["It shows the current fees, the transactions waiting for a block and the newest blocks, all "
                         "looked up on your own node. No website learns what you look at."]

    fees = info.get("fees") or {}
    if fees.get("fastestFee") is not None:
        model["facts"].append({"k": "Fee for the next block", "v": "%s sat/vB" % fees.get("fastestFee")})
        model["facts"].append({"k": "Fee within an hour", "v": "%s sat/vB" % fees.get("hourFee")})
    mempool = info.get("mempool") or {}
    if mempool.get("count") is not None:
        model["facts"].append({"k": "Transactions waiting", "v": fmt_int(mempool["count"])})
    if node.get("blocks"):
        model["facts"].append({"k": "Newest block", "v": fmt_int(node["blocks"])})

    open_s = {"id": "open", "title": "Open the explorer"}
    if info.get("ready"):
        open_s["links"] = [{"label": "Open the Bitcoin Explorer", "href": "/", "style": "primary"}]
        open_s["text"] = ["Bookmark http://mempool.my.ava.do on your computer or phone (on the AVADO Wi-Fi or with "
                          "Remote Access)."]
    else:
        open_s["text"] = ["The button appears here when the explorer is ready."]
    model["sections"].append(open_s)

    look = {"id": "lookups", "title": "Look up your own addresses and transactions"}
    if pruned:
        look["tone"] = "info"
        look["text"] = ["Your Bitcoin Node is in space-saving mode, so the explorer shows fees, waiting transactions and "
                        "the newest blocks. It cannot look up older transactions or addresses: that needs the full node "
                        "and the Electrum Server."]
    elif info.get("electrum"):
        look["text"] = ["Address look-ups go through your own Electrum Server, so you can check your addresses and "
                        "transactions here without telling any website about them."]
    elif info.get("fulcrum") == "busy":
        look["text"] = ["Your Electrum Server is still building its index. When it is ready, the explorer can look up "
                        "addresses too. It switches over by itself."]
    else:
        look["text"] = ["To look up addresses, install the Electrum Server from the AVADO store. The explorer then uses it "
                        "by itself. Until then you can look up transactions and blocks."]
    model["sections"].append(look)

    model["sections"].append({
        "id": "connect", "title": "Connect your hardware wallet to your own node",
        "text": ["Wallets such as Sparrow and Electrum connect through the Electrum Server app. Its page shows the "
                 "exact address to type into your wallet."],
        "links": [{"label": "Open the Electrum Server page", "href": "http://fulcrum.my.ava.do", "style": None}]
        if info.get("fulcrum") != "missing" else
        [{"label": "Open the Bitcoin Node page", "href": "http://bitcoincore.my.ava.do", "style": None}],
    })
    model["sections"].append({
        "id": "privacy", "title": "What leaves your AVADO",
        "text": ["Your look-ups stay on your AVADO. The explorer only fetches Bitcoin prices from public exchanges and the "
                 "list of mining pool names from GitHub; neither learns what you look at."],
        "advanced": [
            {"k": "Address look-ups", "v": "Through your Electrum Server" if info.get("electrum") else "Not available yet"},
            {"k": "Bitcoin node mode", "v": "Space-saving (pruned)" if pruned else ("Full node" if node else "Unknown")},
            {"k": "Explorer has block", "v": fmt_int(info["height"]) if info.get("height") is not None else "Not yet"},
            {"k": "Free on your AVADO", "v": avadoweb.human_bytes(info.get("free"))},
        ],
    })
    return model


if __name__ == "__main__":
    App().run()
