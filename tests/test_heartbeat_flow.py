"""heartbeat 全流程测试: 打桩网络层, 跑真正的 main()。

为什么要有这一层:
    单元测试只能证明 decide() 算对了要摘谁, 证明不了"摘完以后落盘的东西是对的"。
    真正会伤到线上的错误都在后半段 —— 部署整站替换时漏掉文件、nodes.txt 没写、
    heartbeat.json 没更新导致下一轮证据丢失。这些东西只能跑一遍 main() 才看得见。

运行:
    PYTHONPATH=tests python -m unittest discover -s tests -p "test_heartbeat_flow.py" -v
"""

import contextlib
import io
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
    _FIELDS = ("HEARTBEAT_BUDGET", "HEARTBEAT_CONCURRENCY", "HEARTBEAT_TIMEOUT",
               "HEARTBEAT_CONFIRM", "HEARTBEAT_MIN_ALIVE", "HEARTBEAT_MAX_DEAD_RATIO",
               "HEARTBEAT_BUDGET_AUTO", "HEARTBEAT_ESCALATE_MAX",
               "HEARTBEAT_HISTORY_MAX", "HEARTBEAT_HISTORY_AUTO")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (hb.vg.PUBLIC_DIR, hb.vg.NODES_URL, hb.vg.WORKER_URL_OK,
                       hb.fetch_text, hb.fetch_probe, hb.check_batch) + tuple(getattr(hb, f) for f in self._FIELDS)
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
        hb.HEARTBEAT_BUDGET_AUTO = False
        hb.HEARTBEAT_ESCALATE_MAX = 2
        hb.HEARTBEAT_HISTORY_MAX = 0          # 默认关: 时序记录单独测, 不污染其它用例的产物断言
        # 单测要能直接钉住行数上限, 所以关掉 auto —— auto 的换算由 test_heartbeat 的
        # HistoryMaxTest 单独覆盖, 否则「改 HEARTBEAT_HISTORY_MAX=3」会被 auto 静默忽略
        hb.HEARTBEAT_HISTORY_AUTO = False
        self.probe_state = {}                  # name -> "missing" / "error"
        self.remote = {"nodes.txt": NODES, "data.json": json.dumps(DATA), **OTHER}
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": {}, "cursor": 0})
        hb.fetch_probe = self._probe
        hb.fetch_text = self._fetch
        hb.check_batch = lambda targets, session, breaker: (self._results(targets), _Limiter())

    def tearDown(self):
        (hb.vg.PUBLIC_DIR, hb.vg.NODES_URL, hb.vg.WORKER_URL_OK,
         hb.fetch_text, hb.fetch_probe, hb.check_batch) = self._saved[:6]
        for name, value in zip(self._FIELDS, self._saved[6:]):
            setattr(hb, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 桩 ---
    def _probe(self, session, url, what):
        name = url.split("?")[0].rsplit("/", 1)[-1]
        forced = self.probe_state.get(name)
        if forced:
            return forced, ("HTTP 502" if forced == "error" else None)
        if name not in self.remote:
            return "missing", None
        return "ok", self.remote[name]

    def _fetch(self, session, url, what):
        state, value = self._probe(session, url, what)
        return value if state == "ok" else None

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

    # --- 站点镜像: 「线上没有」与「这次没读到」必须分开 ---
    def test_transient_read_error_aborts_deploy_with_exit_4(self):
        self.probe_state["status.json"] = "error"
        self.assertEqual(hb.main(), 4)
        status = json.loads(self._read("heartbeat.json"))
        self.assertFalse(status["deploy"], "镜像不完整时绝不能部署, 否则 status.json 会从线上消失")
        self.assertEqual(status["staging_errors"], ["status.json: HTTP 502"])

    def test_missing_optional_file_is_tolerated(self):
        self.probe_state["status.json"] = "missing"
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertTrue(status["deploy"])
        self.assertEqual(status["staging_missing"], ["status.json"])
        self.assertEqual(status["staging_errors"], [])

    def test_index_html_missing_falls_back_to_template(self):
        self.probe_state["index.html"] = "missing"
        self.assertEqual(hb.main(), 0)
        self.assertTrue(json.loads(self._read("heartbeat.json"))["deploy"])
        self.assertTrue(self._read("index.html"), "index.html 缺失必须用仓库模板兜底, 不能部署一个没有首页的站点")

    def test_index_html_read_error_still_aborts(self):
        self.probe_state["index.html"] = "error"
        self.assertEqual(hb.main(), 4, "读失败不能走模板兜底: 兜底会把线上页面换成旧模板")
        self.assertFalse(json.loads(self._read("heartbeat.json"))["deploy"])

    # --- 升级重跑的冷却 ---
    def test_escalate_cooldown_after_max_retries(self):
        hb.HEARTBEAT_MAX_DEAD_RATIO = 0.5
        hb.HEARTBEAT_ESCALATE_MAX = 2
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": {}, "cursor": 0, "escalate_streak": 2})
        self.dead_hosts = frozenset({"10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"})
        self.assertEqual(hb.main(), 0, "冷却期不该再返回 3, 否则每小时都跑一遍完整流水线")
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "escalate_cooldown")
        self.assertEqual(status["escalate_streak"], 3)
        self.assertEqual(self._read("nodes.txt"), NODES)

    def test_escalate_streak_resets_when_round_is_healthy(self):
        self.remote["heartbeat.json"] = json.dumps({"dead_streak": {}, "cursor": 0, "escalate_streak": 5})
        self.assertEqual(hb.main(), 0)
        self.assertEqual(json.loads(self._read("heartbeat.json"))["escalate_streak"], 0)

    def test_hold_triggers_rerun_not_just_escalate(self):
        """hold 是「摘了会低于 MIN_ALIVE」, 比 escalate 更严重, 必须同样触发重跑。

        decide() 的 reason 里写着「大面积失效应重跑完整流水线」, 但它先判 hold 再判 escalate ——
        最严重的场景全被 hold 兜住, 永远走不到 escalate。不把 hold 纳入闸门, 那句 reason 就是假的。
        """
        self.dead_hosts = frozenset({"10.0.0.1"})
        hb.HEARTBEAT_CONFIRM = 1
        hb.HEARTBEAT_MIN_ALIVE = 8        # 摘 1 个后剩 7 < 8 -> hold
        hb.HEARTBEAT_MAX_DEAD_RATIO = 1.0  # 别让 ratio 先触发 escalate, 这条测的是 hold
        self.assertEqual(hb.main(), 3, "hold 也要返回 3 让 workflow 重跑")
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "hold")
        self.assertEqual(status["verdict"], "hold")
        self.assertEqual(status["escalate_streak"], 1)
        self.assertEqual(self._read("nodes.txt"), NODES, "hold 不就地砍订阅")

    def test_notest_does_not_reset_escalate_streak(self):
        """一个都没测成: 既证明不了失效, 也证明不了恢复 —— 计数必须原地保留。

        清零的后果是「Worker 抽风一小时 -> 攒够的证据全没了 -> 又要重数两轮」,
        与 decide() 里「未测不该让 dead 计数凭空复活」是同一条原则。
        """
        self.remote["heartbeat.json"] = json.dumps(
            {"dead_streak": {}, "cursor": 0, "escalate_streak": 2})
        # 空结果 = 一个也没测成
        hb.check_batch = lambda targets, session, breaker: ({}, _Limiter())
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "notest")
        self.assertEqual(status["escalate_streak"], 2, "notest 不该清零")

    def test_consecutive_notest_keeps_streak_and_is_not_cooldown(self):
        """连续 notest: 计数保持, 但 outcome 如实写 notest —— 不会伪装成 escalate_cooldown。

        cooldown 只在「本轮真的判失效 (escalate/hold) 且已超限」时出现。检测端全挂时
        什么都没测到, 把它写成 escalate_cooldown 会让读者以为「确认失效了、只是在冷却」。
        """
        self.remote["heartbeat.json"] = json.dumps(
            {"dead_streak": {}, "cursor": 0, "escalate_streak": 2})
        hb.HEARTBEAT_ESCALATE_MAX = 2
        hb.check_batch = lambda targets, session, breaker: ({}, _Limiter())
        for i in range(3):
            self.assertEqual(hb.main(), 0, f"第 {i + 1} 轮 notest 不该重跑")
            status = json.loads(self._read("heartbeat.json"))
            self.assertEqual(status["outcome"], "notest")
            self.assertEqual(status["escalate_streak"], 2, "连续 notest 不清零, 也不累加")
            # 把本轮结果当作下一轮的线上状态 —— 否则每轮都是从同一个初始值开始
            self.remote["heartbeat.json"] = json.dumps(
                {"dead_streak": status["dead_streak"], "cursor": status["cursor"],
                 "escalate_streak": status["escalate_streak"]})

    def test_cooldown_keeps_original_verdict(self):
        """进冷却后 outcome 变成 escalate_cooldown, 但原始判定必须留下来可查。"""
        hb.HEARTBEAT_MAX_DEAD_RATIO = 0.5
        hb.HEARTBEAT_ESCALATE_MAX = 1
        self.remote["heartbeat.json"] = json.dumps(
            {"dead_streak": {}, "cursor": 0, "escalate_streak": 1})
        self.dead_hosts = frozenset({"10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"})
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertEqual(status["outcome"], "escalate_cooldown")
        self.assertEqual(status["verdict"], "escalate", "冷却不该抹掉「为什么进冷却」")

    # --- 预算随节点数伸缩 ---
    def test_auto_budget_scales_with_node_count(self):
        many = "\n".join(f"cf.example.com:443#日本-住宅-{i:03d}$sstp://vpn:vpn@10.1.0.{i % 250}:443"
                         for i in range(1, 121)) + "\n"
        self.remote["nodes.txt"] = many
        hb.HEARTBEAT_BUDGET_AUTO = True
        hb.HEARTBEAT_BUDGET = 12
        hb.HEARTBEAT_CONFIRM = 2
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        # 120 个节点 / confirm 2 / 发布周期 12h -> 每轮至少 ceil(120/6)=20 个
        self.assertEqual(status["budget"], 20)
        self.assertTrue(status["budget_auto"])
        self.assertNotIn("scale_warning", status, "auto 预算已达标的配置不该再报规模告警")

    def test_fixed_budget_reports_scale_warning(self):
        many = "\n".join(f"cf.example.com:443#日本-住宅-{i:03d}$sstp://vpn:vpn@10.1.{i % 250}.{i % 200 + 1}:443"
                         for i in range(1, 201)) + "\n"
        self.remote["nodes.txt"] = many
        hb.HEARTBEAT_BUDGET = 12
        hb.HEARTBEAT_CONFIRM = 2
        self.assertEqual(hb.main(), 0)
        status = json.loads(self._read("heartbeat.json"))
        self.assertIn("scale_warning", status, "N 超过 budget*6 时必须自己喊出来, 否则症状只是「从来不摘」")
        self.assertIn("只测不摘", status["scale_warning"])

    def test_missing_state_with_published_nodes_warns(self):
        """「首轮」和「状态被 check 部署清掉」单看文件分不出来, 但必须在日志里留痕。

        否则最坏的情况是: check 的 preserve 步骤失败 -> streak 每 12 小时归零一次 ->
        心跳永远凑不齐 confirm 轮 -> 每小时都在跑、从来不摘, 而两边日志全绿。
        """
        self.remote.pop("heartbeat.json", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):   # vg.warn 走的是 print() -> stdout
            self.assertEqual(hb.main(), 0)
        out = buf.getvalue()
        self.assertIn("读不到 heartbeat.json", out)
        self.assertIn("死链证据从零重攒", out)

    def test_absent_state_on_empty_site_is_silent(self):
        # 没有已发布节点时不该刷告警 (那才是真正的首轮 / 空站点)
        self.remote.pop("heartbeat.json", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hb.load_prev_heartbeat(None, 0)
        self.assertNotIn("读不到 heartbeat.json", buf.getvalue())

    # --- 时序记录 ---
    def test_history_appended_and_trimmed(self):
        hb.HEARTBEAT_HISTORY_MAX = 3
        self.remote["heartbeat-history.jsonl"] = "\n".join(
            json.dumps({"t": "old", "h": f"9.9.9.{i}", "p": 443}) for i in range(2)) + "\n"
        self.assertEqual(hb.main(), 0)
        lines = [ln for ln in self._read("heartbeat-history.jsonl").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3, "超出上限只保留最后 N 行")
        last = json.loads(lines[-1])
        self.assertEqual(last["act"], "kept")
        self.assertTrue(last["ok"])
        self.assertIn("ms", last)

    def test_history_not_overwritten_when_read_fails(self):
        hb.HEARTBEAT_HISTORY_MAX = 100
        self.remote["heartbeat-history.jsonl"] = '{"t":"old","h":"9.9.9.9","p":443}\n'
        self.probe_state["heartbeat-history.jsonl"] = "error"
        self.assertEqual(hb.main(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "heartbeat-history.jsonl")),
                         "读不到旧历史就不能发布新历史 —— 那等于把攒了几周的数据一次删光")

    def test_history_records_pruned_action(self):
        hb.HEARTBEAT_HISTORY_MAX = 100
        self.dead_hosts = frozenset({"10.0.0.2"})
        self.assertEqual(hb.main(), 0)
        lines = [json.loads(ln) for ln in self._read("heartbeat-history.jsonl").splitlines() if ln.strip()]
        pruned = [x for x in lines if x["act"] == "pruned"]
        self.assertEqual([x["h"] for x in pruned], ["10.0.0.2"])
        self.assertEqual(pruned[0]["kind"], "node_dead")


class StaticShapeTest(unittest.TestCase):
    """常量与使用点不匹配的 bug 单测很难覆盖 —— 用结构断言把它钉住。

    这正是冒烟时抓到的那个错: STAGE_FILES 从字符串改成 (名, 是否必需) 元组后,
    循环里的 `for name in STAGE_FILES` 没同步, `', '.join(missing)` 直接抛 TypeError。
    """

    def test_stage_files_shape(self):
        self.assertIsInstance(hb.STAGE_FILES, (tuple, list))
        names = []
        for item in hb.STAGE_FILES:
            self.assertIsInstance(item, tuple, f"STAGE_FILES 的元素必须是 (名, 必需) 元组: {item!r}")
            self.assertEqual(len(item), 2, f"元组长度必须是 2: {item!r}")
            name, required = item
            self.assertIsInstance(name, str)
            self.assertIsInstance(required, bool)
            names.append(name)
        self.assertEqual(len(names), len(set(names)), "文件名不能重复")
        self.assertIn("index.html", names)
        self.assertIn("data.json", names)

    def test_fetch_and_check_names_are_strings(self):
        self.assertIsInstance(hb.HEARTBEAT_NAME, str)
        self.assertIsInstance(hb.HISTORY_NAME, str)
        self.assertTrue(hb.HEARTBEAT_NAME.endswith(".json"))
        self.assertTrue(hb.HISTORY_NAME.endswith(".jsonl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
