"""heartbeat 全流程测试: 打桩网络层, 跑真正的 main()。

为什么要有这一层:
    单元测试只能证明 decide() 算对了要摘谁, 证明不了"摘完以后落盘的东西是对的"。
    真正会伤到线上的错误都在后半段 —— 部署整站替换时漏掉文件、nodes.txt 没写、
    heartbeat.json 没更新导致下一轮证据丢失。这些东西只能跑一遍 main() 才看得见。

运行:
    PYTHONPATH=tests python -m unittest discover -s tests -p "test_heartbeat_flow.py" -v
"""

import json
import os
import shutil
import tempfile
import unittest

import heartbeat as hb

NODES = "\n".join(
    f"cf.example.com:443#日本-住宅-{i:02d}$sstp://vpn:vpn@10.0.0.{i}:443" for i in range(1, 9)
) + "\n"

DATA = {
    "generated_at": "2026-10-30 00:00:00 UTC",
    "source": "fake",
    "stats": {"raw_nodes": 100, "sstp_nodes": 40, "checked": 8, "success": 8,
              "failed": 0, "carried_over": 0, "countries": 1,
              "residential_est": 8, "datacenter_est": 0},
    "countries": {"日本": {"code": "JP", "count": 8, "residential": 8, "datacenter": 0,
                          "nodes": [{"host": f"10.0.0.{i}", "port": 443, "residential": "residential"}
                                    for i in range(1, 9)]}},
    "available": [{"host": f"10.0.0.{i}", "port": 443} for i in range(1, 9)],
}

OTHER = {"index.html": "<html>fake</html>", "status.json": "{}",
         "check-worker.js": "//fake", "endpoints.json": "[]"}


class _Limiter:
    def snapshot(self):
        return {"interval_s": 0.0, "pauses": 0}


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (hb.vg.PUBLIC_DIR, hb.vg.NODES_URL, hb.vg.WORKER_URL_OK, hb.fetch_text, hb.check_batch,
                       hb.HEARTBEAT_BUDGET, hb.HEARTBEAT_CONCURRENCY, hb.HEARTBEAT_TIMEOUT,
                       hb.HEARTBEAT_CONFIRM, hb.HEARTBEAT_MIN_ALIVE, hb.HEARTBEAT_MAX_DEAD_RATIO)
        hb.vg.PUBLIC_DIR = self.tmp
        hb.vg.NODES_URL = "https://fake.example/gate/nodes.txt"
        # main() 在 Worker 未配置时会直接跳过整轮 (返回 0, 不写任何东西) —— 测试里必须放开这一闸
        hb.vg.WORKER_URL_OK = True
        hb.HEARTBEAT_BUDGET = 12
        hb.HEARTBEAT_CONCURRENCY = 1
        hb.HEARTBEAT_TIMEOUT = 5
        hb.HEARTBEAT_CONFIRM = 1
        hb.HEARTBEAT_MIN_ALIVE = 1
        hb.HEARTBEAT_MAX_DEAD_RATIO = 1.0
        self.remote = {"nodes.txt": NODES, "data.json": json.dumps(DATA), **OTHER}
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": {}, "cursor": 0})
        hb.fetch_text = self._fetch
        hb.check_batch = lambda targets, session, breaker: (self._results(targets), _Limiter())

    def tearDown(self):
        (hb.vg.PUBLIC_DIR, hb.vg.NODES_URL, hb.vg.WORKER_URL_OK, hb.fetch_text, hb.check_batch,
         hb.HEARTBEAT_BUDGET, hb.HEARTBEAT_CONCURRENCY, hb.HEARTBEAT_TIMEOUT,
         hb.HEARTBEAT_CONFIRM, hb.HEARTBEAT_MIN_ALIVE, hb.HEARTBEAT_MAX_DEAD_RATIO) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 桩 ---
    def _fetch(self, session, url, what):
        name = url.split("?")[0].rsplit("/", 1)[-1]
        return self.remote.get(name)

    dead_hosts = frozenset()

    def _results(self, targets):
        out = {}
        for t in targets:
            k = (t["host"], t["port"])
            if t["host"] in self.dead_hosts:
                out[k] = {"host": t["host"], "port": t["port"], "success": False,
                          "failure_kind": "node_dead", "worker_error": False, "skipped_by_breaker": False}
            else:
                out[k] = {"host": t["host"], "port": t["port"], "success": True, "failure_kind": None}
        return out

    def _read(self, name):
        path = os.path.join(self.tmp, name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # --- 用例 ---
    def test_no_change_still_writes_heartbeat_and_stages_site(self):
        self.dead_hosts = frozenset()
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "ok")
        self.assertFalse(status["changed"])
        self.assertEqual(self._read("nodes.txt"), NODES, "没改动就必须原样输出, 一个字节都不能变")
        for name in ("index.html", "status.json", "check-worker.js", "endpoints.json", "data.json"):
            self.assertIsNotNone(self._read(name), f"{name} 必须被镜像到 public/, 否则部署会把它从线上删掉")

    def test_prune_rewrites_nodes_and_data(self):
        self.dead_hosts = frozenset({"10.0.0.2", "10.0.0.5"})
        self.assertEqual(hb.main(), 0)
        nodes = self._read("nodes.txt")
        self.assertNotIn("10.0.0.2:", nodes)
        self.assertNotIn("10.0.0.5:", nodes)
        self.assertEqual(len([ln for ln in nodes.splitlines() if ln.strip()]), 6)

        data = json.loads(self._read("data.json"))
        self.assertEqual(data["stats"]["success"], 6)
        self.assertEqual(data["countries"]["日本"]["count"], 6)
        self.assertEqual(data["heartbeat"]["removed"], 2)
        self.assertNotIn("10.0.0.2", json.dumps(data["available"]))

        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual((status["outcome"], status["changed"], status["pruned_count"]), ("pruned", True, 2))
        self.assertEqual(len(status["dead_streak"]), 2)

    def test_untested_nodes_are_kept_end_to_end(self):
        def flaky(targets, session, breaker):
            out = {}
            for t in targets:
                out[(t["host"], t["port"])] = {
                    "host": t["host"], "port": t["port"], "success": False,
                    "failure_kind": "worker_error", "worker_error": True, "skipped_by_breaker": False}
            return out, _Limiter()
        hb.check_batch = flaky
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": {f"10.0.0.{i}:443": 9 for i in range(1, 9)}})
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "ok")
        self.assertFalse(status["changed"])
        self.assertEqual(self._read("nodes.txt"), NODES, "检测服务异常时不许动节点表")

    def test_missing_nodes_txt_returns_1_and_writes_nothing(self):
        del self.remote["nodes.txt"]
        self.assertEqual(hb.main(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nodes.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "heartbeat.json")))

    def test_escalate_returns_3_without_pruning(self):
        hb.HEARTBEAT_MAX_DEAD_RATIO = 0.5
        self.dead_hosts = frozenset({"10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"})
        self.assertEqual(hb.main(), 3)
        self.assertEqual(self._read("nodes.txt"), NODES, "升级重跑时不就地砍订阅")
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "escalate")
        self.assertFalse(status["changed"])

    def test_dead_streak_accumulates_across_runs(self):
        """第二轮才摘: 验证 heartbeat.json 里的证据真的被下一轮读回来了。"""
        self.dead_hosts = frozenset({"10.0.0.3"})
        hb.HEARTBEAT_CONFIRM = 2
        self.assertEqual(hb.main(), 0)
        first = json.loads(self._read("heartbeat.json"))
        self.assertEqual((first["outcome"], first["changed"]), ("ok", False))
        self.assertEqual(first["dead_streak"]["10.0.0.3:443"], 1)

        # 把第一轮的结果当作线上 heartbeat.json, 再跑一轮
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": first["dead_streak"],
                                                    "cursor": first["cursor"]})
        self.assertEqual(hb.main(), 0)
        second = json.loads(self._read("heartbeat.json"))
        self.assertEqual(second["outcome"], "pruned")
        self.assertNotIn("10.0.0.3:", self._read("nodes.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
