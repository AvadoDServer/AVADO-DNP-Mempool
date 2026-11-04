import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def node(**kw):
    base = {"chain": "main", "blocks": 920000, "headers": 920000, "verificationprogress": 0.999999,
            "initialblockdownload": False, "pruned": False}
    base.update(kw)
    return base


def model(**info):
    base = {"nodeInstalled": True, "free": 10 ** 12, "fulcrum": "missing"}
    base.update(info)
    return main.build_model(base)


RPC = {"host": "node", "port": 8332, "user": "mempool", "password": "rpc-secret"}


class ConfigTest(unittest.TestCase):
    def test_node_only(self):
        c = main.backend_config("main", RPC, False, {"password": "db"}, 11000)
        self.assertEqual(c["MEMPOOL"]["BACKEND"], "none")
        self.assertEqual(c["MEMPOOL"]["NETWORK"], "mainnet")
        self.assertEqual(c["CORE_RPC"]["USERNAME"], "mempool")
        self.assertFalse(c["LIGHTNING"]["ENABLED"])
        self.assertFalse(c["MEMPOOL_SERVICES"]["ACCELERATIONS"])
        self.assertEqual(c["DATABASE"]["PID_DIR"], main.RUN_DIR)
        # Never the OS user "mempool": changing its login would lock the next start out.
        self.assertEqual(c["DATABASE"]["USERNAME"], "explorer")

    def test_with_electrum(self):
        c = main.backend_config("main", RPC, True, {"password": "db"}, 11000)
        self.assertEqual(c["MEMPOOL"]["BACKEND"], "electrum")
        self.assertEqual(c["ELECTRUM"]["HOST"], "DAppNodePackage-fulcrum.avado.dnp.dappnode.eth")

    def test_runs_only_on_the_full_node(self):
        # mempool needs txindex (getrawtransaction for every new block); a pruned node cannot have one.
        self.assertTrue(main.can_run(node()))
        self.assertFalse(main.can_run(node(pruned=True)))
        self.assertFalse(main.can_run({}))


class UseElectrumTest(unittest.TestCase):
    def test_space_saving_node_never_uses_the_electrum_server(self):
        for fulcrum in ("ready", "busy", "missing"):
            self.assertFalse(main.use_electrum(node(pruned=True), fulcrum, True))

    def test_full_node(self):
        self.assertTrue(main.use_electrum(node(), "ready", False))
        self.assertFalse(main.use_electrum(node(), "missing", True))
        self.assertTrue(main.use_electrum(node(), "busy", True))  # restarting: keep it
        self.assertFalse(main.use_electrum(node(), "busy", False))


class FakeBackend:
    def __init__(self):
        self.up = False
        self.starts = 0

    def running(self):
        return self.up

    def stop(self, *_a, **_k):
        self.up = False

    def restart(self):
        self.up = True
        self.starts += 1


class BackendTest(unittest.TestCase):
    """Space-saving node: the explorer stops; full again: it starts by itself."""

    def app(self):
        import tempfile
        app = main.App.__new__(main.App)
        app.bitcoin_host, app.login, app.db_ready, app.db_login = "node", ("mempool", "pw", 8332), True, {"password": "db"}
        app.backend, app.backend_key, app.electrum, app.node = FakeBackend(), None, False, {}
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.old_conf = main.BACKEND_CONF
        main.BACKEND_CONF = os.path.join(self.dir.name, "conf.json")
        self.addCleanup(setattr, main, "BACKEND_CONF", self.old_conf)
        return app

    def test_space_saving_stops_and_full_starts_again(self):
        app = self.app()
        app.node = node()
        app.apply_backend("missing")
        self.assertTrue(app.backend.running())
        app.node = node(pruned=True)
        app.apply_backend("ready")
        self.assertFalse(app.backend.running())
        app.apply_backend("ready")
        self.assertFalse(app.backend.running())
        self.assertEqual(app.backend.starts, 1)
        # back to the full node (it downloads again): starts by itself
        app.node = node(initialblockdownload=True, verificationprogress=0.01)
        app.apply_backend("busy")
        self.assertTrue(app.backend.running())
        self.assertEqual(app.backend.starts, 2)

    def test_never_starts_on_a_space_saving_node(self):
        app = self.app()
        app.node = node(pruned=True)
        app.apply_backend("missing")
        self.assertFalse(app.backend.running())
        self.assertFalse(os.path.exists(main.BACKEND_CONF))

    def test_node_away_keeps_what_runs(self):
        app = self.app()
        app.node = node()
        app.apply_backend("missing")
        app.node = {}  # the node restarts
        app.apply_backend("missing")
        self.assertTrue(app.backend.running())
        self.assertEqual(app.backend.starts, 1)


class ModelTest(unittest.TestCase):
    def test_waiting_for_node_download(self):
        m = model(node=node(verificationprogress=0.5, initialblockdownload=True))
        self.assertIn("still downloading", m["headline"])
        self.assertEqual(m["progress"]["percent"], 50.0)

    def test_ready(self):
        m = model(node=node(), ready=True, height=920000, fees={"fastestFee": 12, "hourFee": 5},
                  mempool={"count": 34567})
        self.assertEqual(m["state"], "ok")
        self.assertEqual(m["sections"][0]["links"][0]["href"], "/")
        self.assertIn({"k": "Fee for the next block", "v": "12 sat/vB"}, m["facts"])

    def test_space_saving_node_shows_one_message_and_the_switch(self):
        for info in ({}, {"ready": True, "fulcrum": "ready"}, {"node": node(pruned=True, initialblockdownload=True,
                                                                        verificationprogress=0.3)}):
            m = model(**dict({"node": node(pruned=True)}, **info))
            self.assertEqual(m["state"], "warn")
            self.assertEqual(m["headline"], "The Explorer needs the full node")
            self.assertNotIn("progress", m)
            self.assertEqual(len(m["sections"]), 1)
            link = m["sections"][0]["links"][0]
            self.assertEqual(link["label"], "Switch to the full node")
            self.assertEqual(link["href"], "http://bitcoincore.my.ava.do/#connect")
            self.assertNotIn("fulcrum.my.ava.do", repr(m))
            self.assertNotIn("Open the Bitcoin Explorer", repr(m))

    def test_full_node_wallet_card_points_to_the_electrum_server(self):
        m = model(node=node(), ready=True, fulcrum="busy")
        connect = [s for s in m["sections"] if s["id"] == "connect"][0]
        self.assertEqual(connect["links"][0]["href"], "http://fulcrum.my.ava.do")

    def test_open_card_only_when_ready(self):
        m = model(node=node(), ready=False)
        self.assertNotIn("open", [s["id"] for s in m["sections"]])
        self.assertNotIn("button appears", repr(m))
        m = model(node=node(verificationprogress=0.5, initialblockdownload=True))
        self.assertNotIn("open", [s["id"] for s in m["sections"]])
        m = model(node=node(), ready=True)
        self.assertEqual(m["sections"][0]["id"], "open")

    def test_electrum_busy(self):
        m = model(node=node(), ready=True, fulcrum="busy")
        look = [s for s in m["sections"] if s["id"] == "lookups"][0]
        self.assertIn("still building", " ".join(look["text"]))

    def test_node_missing(self):
        self.assertEqual(model(nodeInstalled=False)["headline"], "Install the Bitcoin Node first")

    def test_database_error_is_plain(self):
        m = model(node=node(), dbError="the database did not start")
        self.assertEqual(m["state"], "error")
        self.assertIn("database", m["headline"])

    def test_no_secrets(self):
        m = model(node=node(), ready=True)
        self.assertNotIn("password", repr(m).lower())


class ElectrumProbeTest(unittest.TestCase):
    """The explorer must not take a port that only accepts connections for a ready server."""

    def serve_once(self, handler):
        import socket
        import threading
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            with conn:
                handler(conn)
            srv.close()

        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    def test_real_server_answers(self):
        def handler(conn):
            conn.recv(4096)
            conn.sendall(b'{"jsonrpc": "2.0", "id": 0, "result": ["Fulcrum 2.1.4", "1.4"]}\n')
        self.assertTrue(main.electrum_answers("127.0.0.1", self.serve_once(handler)))

    def test_gate_without_server_is_not_ready(self):
        def handler(conn):
            conn.recv(4096)  # the port gate: accepts, then closes because Fulcrum is not up
        self.assertFalse(main.electrum_answers("127.0.0.1", self.serve_once(handler)))

    def test_error_reply_is_not_ready(self):
        def handler(conn):
            conn.recv(4096)
            conn.sendall(b'{"jsonrpc": "2.0", "id": 0, "error": {"code": -32600, "message": "busy"}}\n')
        self.assertFalse(main.electrum_answers("127.0.0.1", self.serve_once(handler)))

    def test_nothing_listening(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(main.electrum_answers("127.0.0.1", port, timeout=1))


class FramingTest(unittest.TestCase):
    """The Admin's Setup tab shows this page in a frame; other sites may not."""

    def test_only_the_admin_may_frame_the_page(self):
        import avadoweb
        csp = avadoweb.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self' http://my.ava.do https://my.ava.do;", csp)
        self.assertNotIn("X-Frame-Options", avadoweb.SECURITY_HEADERS)


if __name__ == "__main__":
    unittest.main()
