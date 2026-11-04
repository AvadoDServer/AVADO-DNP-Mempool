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

    def test_indexing_full(self):
        self.assertEqual(main.indexing_amount(node()), 11000)

    def test_indexing_pruned_stays_within_kept_blocks(self):
        self.assertEqual(main.indexing_amount(node(pruned=True, pruneheight=914000)), 5700)
        self.assertEqual(main.indexing_amount(node(pruned=True, pruneheight=919900)), 0)


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

    def test_pruned_explains_limits(self):
        m = model(node=node(pruned=True), ready=True)
        look = [s for s in m["sections"] if s["id"] == "lookups"][0]
        self.assertIn("space-saving", " ".join(look["text"]))

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


if __name__ == "__main__":
    unittest.main()
