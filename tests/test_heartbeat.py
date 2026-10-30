"""heartbeat 的离线测试: 只测判定逻辑, 不碰网络。

判定决定「摘哪些行」, 是这套机制里唯一具备破坏性的地方 ——
测不到的地方就会变成线上事故, 所以这里覆盖的是各种"该摘 / 不该摘"的边界,
而不是 happy path。

运行 (仓库约定: 用 tests/requests.py 顶替真实 requests):
    PYTHONPATH=tests python -m unittest discover -s tests -p "test_heartbeat.py" -v
"""

import copy
import unittest

import heartbeat as hb


def _line(host="1.1.1.1", port=443, entry="cf.example.com:443", tag="日本-住宅-01"):
    return f"{entry}#{tag}$sstp://vpn:vpn@{host}:{port}"


def _items(n=6, port=443):
    """构造 n 个可解析节点; 全部用同一端口, 便于控制 count/ratio。"""
    lines = [_line(host=f"10.0.0.{i}", port=port, tag=f"日本-住宅-{i:02d}") for i in range(1, n + 1)]
    items, skipped = hb.parse_nodes_text("\n".join(lines))
    assert not skipped, skipped
    return items


def _res(items, dead=(), alive=(), untested=(), kind="node_dead"):
    out = {}
    for it in items:
        k = (it["host"], it["port"])
        if it in dead or k in dead:
            out[k] = {"host": it["host"], "port": it["port"], "success": False,
                      "failure_kind": kind, "worker_error": False, "skipped_by_breaker": False}
        elif it in alive or k in alive:
            out[k] = {"host": it["host"], "port": it["port"], "success": True, "failure_kind": None}
        elif it in untested or k in untested:
            out[k] = {"host": it["host"], "port": it["port"], "success": False,
                      "failure_kind": "worker_error", "worker_error": True, "skipped_by_breaker": False}
    return out


class TestParse(unittest.TestCase):
    def test_parse_full_line(self):
        items, skipped = hb.parse_nodes_text(_line())
        self.assertEqual(skipped, [])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["host"], it["port"]), ("1.1.1.1", 443))
        self.assertEqual(it["entry"], "cf.example.com:443")
        self.assertEqual(it["tag"], "日本-住宅-01")

    def test_host_lowercased_for_stable_keys(self):
        items, _ = hb.parse_nodes_text(_line(host="CF.Example.COM"))
        self.assertEqual(items[0]["host"], "cf.example.com")
        self.assertEqual(hb.node_key(items[0]), "cf.example.com:443")

    def test_unparsable_lines_are_reported_not_dropped(self):
        text = _line() + "\n" + "not-a-node-at-all" + "\n" + "\n"
        items, skipped = hb.parse_nodes_text(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(skipped), 1, "不合法的行必须报告, 由调用方决定怎么处理")

    def test_empty(self):
        items, skipped = hb.parse_nodes_text("")
        self.assertEqual((items, skipped), ([], []))

    def test_roundtrip_prune_keeps_original_order(self):
        items = _items(4)
        kept = hb.prune_nodes_text(items, {hb.node_key(items[1])})
        lines = [ln for ln in kept.splitlines() if ln]
        self.assertEqual(lines, [items[0]["line"], items[2]["line"], items[3]["line"]])


class TestSelect(unittest.TestCase):
    def test_small_list_takes_all_without_moving_cursor(self):
        items = _items(3)
        picked, cursor = hb.select_targets(items, budget=12, cursor=5)
        self.assertEqual(len(picked), 3)
        self.assertEqual(cursor, 5)

    def test_budget_respected(self):
        items = _items(40)
        picked, _ = hb.select_targets(items, budget=8)
        self.assertEqual(len(picked), 8)

    def test_cursor_rotates_coverage(self):
        """多轮累计必须能覆盖全表 —— 否则固定取前 N 个, 末尾节点永远不被复检。"""
        items = _items(40)
        seen, cursor = set(), 0
        for _ in range(10):
            picked, cursor = hb.select_targets(items, budget=4, cursor=cursor)
            seen.update(hb.node_key(it) for it in picked)
        self.assertGreaterEqual(len(seen), 20, f"10 轮 × 4 个只覆盖了 {len(seen)}/40 个")

    def test_hot_nodes_take_at_most_half_the_budget(self):
        items = _items(20)
        streak = {hb.node_key(it): 1 for it in items[:10]}
        picked, _ = hb.select_targets(items, budget=8, cursor=0, streak=streak)
        hot = [it for it in picked if hb.node_key(it) in streak]
        self.assertLessEqual(len(hot), 4, "僵死节点群不能把额度吃干净")
        self.assertEqual(len(picked), 8)


class TestDecide(unittest.TestCase):
    def test_first_round_never_prunes(self):
        items = _items(6)
        v = hb.decide(items, _res(items, dead=items[:3]), {}, confirm=2, min_alive=1)
        self.assertEqual(v["prune"], set(), "单轮结论不足以摘除")
        self.assertEqual(v["outcome"], "ok")
        self.assertEqual(len(v["streak"]), 3)
        self.assertTrue(all(n == 1 for n in v["streak"].values()))

    def test_second_round_prunes(self):
        items = _items(6)
        first = hb.decide(items, _res(items, dead=items[:2]), {}, confirm=2, min_alive=1)
        # max_dead_ratio=1.0: 本轮只抽了 2 个且全死, 死亡占比 100% 会触发 escalate,
        # 这条用例要测的是「两轮确认后摘除」, 不是 escalate, 所以先把那个闸门放开。
        second = hb.decide(items, _res(items, dead=items[:2]), first["streak"],
                           confirm=2, min_alive=1, max_dead_ratio=1.0)
        self.assertEqual(second["prune"], {hb.node_key(it) for it in items[:2]})
        self.assertEqual(second["outcome"], "pruned")
        self.assertEqual(second["dead"], 2)

    def test_worker_error_never_prunes(self):
        items = _items(6)
        streak = {hb.node_key(it): 5 for it in items[:3]}
        v = hb.decide(items, _res(items, untested=items[:3]), streak, confirm=1, min_alive=1)
        self.assertEqual(v["prune"], set(), "「没测成」不能当成「不通」")
        self.assertEqual(v["untested"], 3)
        self.assertEqual(v["streak"][hb.node_key(items[0])], 5, "未测不得重置已累积的证据")

    def test_target_ratelimit_never_prunes(self):
        items = _items(4)
        streak = {hb.node_key(items[0]): 9}
        res = {(items[0]["host"], 443): {"host": items[0]["host"], "port": 443, "success": False,
                                        "failure_kind": "target_ratelimit", "worker_error": True,
                                        "skipped_by_breaker": False}}
        v = hb.decide(items, res, streak, confirm=1, min_alive=1)
        self.assertEqual(v["prune"], set(), "目标侧 429 是「未测」, 摘掉等于帮限流方干活")
        self.assertEqual(v["untested"], 1)

    def test_breaker_skip_never_prunes(self):
        items = _items(4)
        streak = {hb.node_key(items[0]): 9}
        res = {(items[0]["host"], 443): {"host": items[0]["host"], "port": 443, "success": False,
                                        "failure_kind": "node_dead", "worker_error": True,
                                        "skipped_by_breaker": True}}
        v = hb.decide(items, res, streak, confirm=1, min_alive=1)
        self.assertEqual(v["prune"], set(), "被熔断跳过 = 没测, 不能算确认不通")

    def test_alive_resets_streak(self):
        items = _items(4)
        streak = {hb.node_key(items[0]): 1}
        v = hb.decide(items, _res(items, alive=items[:1]), streak, confirm=2, min_alive=1)
        self.assertNotIn(hb.node_key(items[0]), v["streak"], "恢复的节点必须清零, 否则旧证据会叠加")

    def test_hold_when_too_few_would_remain(self):
        items = _items(6)
        v = hb.decide(items, _res(items, dead=items[:4]), {}, confirm=1, min_alive=5)
        self.assertEqual(v["outcome"], "hold")
        self.assertEqual(v["prune"], set())
        self.assertIn("重跑", v["reason"])

    def test_escalate_on_mass_death(self):
        items = _items(10)
        v = hb.decide(items, _res(items, dead=items[:6]), {}, confirm=1, min_alive=1, max_dead_ratio=0.5)
        self.assertEqual(v["outcome"], "escalate")
        self.assertEqual(v["prune"], set(), "大面积失效要重跑, 不能就地砍掉一半订阅")
        self.assertGreater(v["ratio"], 0.5)

    def test_notest_when_nothing_checked(self):
        items = _items(4)
        v = hb.decide(items, {}, {}, confirm=1, min_alive=1)
        self.assertEqual(v["outcome"], "notest")
        self.assertEqual(v["prune"], set())

    def test_streak_pruned_of_stale_keys(self):
        items = _items(4)
        streak = {"9.9.9.9:443": 7, hb.node_key(items[0]): 1}
        v = hb.decide(items, _res(items, alive=items), streak, confirm=2, min_alive=1)
        self.assertEqual(v["streak"], {}, "已不在产物里的历史记录必须清掉, 否则 heartbeat.json 无限膨胀")


class TestPruneDataJson(unittest.TestCase):
    DATA = {
        "generated_at": "2026-10-30 00:00:00 UTC",
        "stats": {"raw_nodes": 300, "sstp_nodes": 120, "checked": 120, "success": 3,
                  "failed": 117, "carried_over": 0, "countries": 2,
                  "residential_est": 2, "datacenter_est": 1},
        "countries": {
            "日本": {"code": "JP", "count": 2, "residential": 1, "datacenter": 1,
                     "nodes": [{"host": "1.1.1.1", "port": 443, "residential": "residential"},
                               {"host": "1.1.1.2", "port": 443, "residential": "datacenter"}]},
            "新加坡": {"code": "SG", "count": 1, "residential": 1, "datacenter": 0,
                       "nodes": [{"host": "2.2.2.2", "port": 995, "residential": "residential"}]},
        },
        "available": [{"host": "1.1.1.1", "port": 443}, {"host": "1.1.1.2", "port": 443},
                      {"host": "2.2.2.2", "port": 995}],
    }

    def test_prune_removes_nodes_and_recomputes_counts(self):
        data = hb.prune_data_json(copy.deepcopy(self.DATA), {"1.1.1.1:443"})
        self.assertEqual(data["stats"]["success"], 2)
        self.assertEqual(data["stats"]["countries"], 2)
        self.assertEqual(data["countries"]["日本"]["count"], 1)
        self.assertEqual(data["countries"]["日本"]["datacenter"], 1)
        self.assertEqual(data["countries"]["日本"]["residential"], 0)
        self.assertEqual(len([n for n in data["available"] if n["host"] == "1.1.1.1"]), 0)

    def test_empty_country_dropped(self):
        data = hb.prune_data_json(copy.deepcopy(self.DATA), {"2.2.2.2:995"})
        self.assertNotIn("新加坡", data["countries"])
        self.assertEqual(data["stats"]["countries"], 1)

    def test_history_fields_not_faked(self):
        before = dict(self.DATA["stats"])
        data = hb.prune_data_json(copy.deepcopy(self.DATA), {"1.1.1.1:443"})
        self.assertEqual(data["stats"]["raw_nodes"], before["raw_nodes"], "raw_nodes 描述那一轮抓取, 不能重写")
        self.assertEqual(data["stats"]["checked"], before["checked"], "checked 描述那一轮检测, 不能重写")

    def test_bad_input_is_passed_through(self):
        self.assertEqual(hb.prune_data_json(None, {"a:1"}), None)
        self.assertEqual(hb.prune_data_json([], set()), [])

    def test_input_not_mutated(self):
        original = copy.deepcopy(self.DATA)
        hb.prune_data_json(self.DATA, {"1.1.1.1:443"})
        self.assertEqual(self.DATA, original, "prune_data_json 不应改动调用方传入的对象")


class BudgetTest(unittest.TestCase):
    """预算与规模: 这两个纯函数决定了「心跳是不是在空转」。

    N 超过 budget*6 时, 连续 2 轮确认要跑超过 12 小时, 而 check.yml 每 12 小时整站重发,
    证据会被清掉 —— 症状是「每小时都在跑、从来不摘」, 没有告警就永远查不出来。
    """

    _FIELDS = ("HEARTBEAT_BUDGET_AUTO", "HEARTBEAT_BUDGET", "HEARTBEAT_CONFIRM",
               "HEARTBEAT_BUDGET_FLOOR", "HEARTBEAT_BUDGET_CEIL")

    def _with(self, **kw):
        self._saved = tuple(getattr(hb, f) for f in self._FIELDS)
        for k, v in kw.items():
            setattr(hb, k, v)

    def tearDown(self):
        if hasattr(self, "_saved"):
            for name, value in zip(self._FIELDS, self._saved):
                setattr(hb, name, value)

    def test_auto_keeps_floor_for_small_site(self):
        self._with(HEARTBEAT_BUDGET_AUTO=True, HEARTBEAT_CONFIRM=2,
                   HEARTBEAT_BUDGET_FLOOR=12, HEARTBEAT_BUDGET_CEIL=48)
        self.assertEqual(hb.resolve_budget(17), 12, "小站点不该被自动预算削到 12 以下")

    def test_auto_scales_with_node_count(self):
        self._with(HEARTBEAT_BUDGET_AUTO=True, HEARTBEAT_CONFIRM=2,
                   HEARTBEAT_BUDGET_FLOOR=12, HEARTBEAT_BUDGET_CEIL=48)
        self.assertEqual(hb.resolve_budget(120), 20)   # ceil(120 / (12//2))
        self.assertEqual(hb.resolve_budget(200), 34)

    def test_auto_respects_ceiling(self):
        self._with(HEARTBEAT_BUDGET_AUTO=True, HEARTBEAT_CONFIRM=2,
                   HEARTBEAT_BUDGET_FLOOR=12, HEARTBEAT_BUDGET_CEIL=48)
        self.assertEqual(hb.resolve_budget(4000), 48, "上限用于兜住请求量, 不能随 N 无限增长")

    def test_fixed_budget_is_not_scaled(self):
        self._with(HEARTBEAT_BUDGET_AUTO=False, HEARTBEAT_BUDGET=12,
                   HEARTBEAT_CONFIRM=2, HEARTBEAT_BUDGET_FLOOR=12, HEARTBEAT_BUDGET_CEIL=48)
        self.assertEqual(hb.resolve_budget(999), 12)

    def test_scale_warning_is_silent_when_window_fits(self):
        self.assertIsNone(hb.scale_warning(17, 12))
        self.assertIsNone(hb.scale_warning(72, 12), "72/12=6h, 2 轮正好 12h, 仍在发布周期内")

    def test_scale_warning_names_the_failure_mode(self):
        msg = hb.scale_warning(200, 12)
        self.assertIsNotNone(msg)
        self.assertIn("只测不摘", msg)
        self.assertIn("HEARTBEAT_BUDGET=auto", msg)


class FetchProbeTest(unittest.TestCase):
    """404 与 5xx 必须分开: 前者允许继续, 后者必须放弃本轮部署。"""

    def _probe(self, status, text="", boom=None):
        class _R:
            status_code = status

        _R.text = text

        def fake(session, url):
            if boom:
                raise boom
            return _R()

        saved = hb.vg._http_get
        hb.vg._http_get = fake
        try:
            return hb.fetch_probe(None, "https://x.example/y", "y")
        finally:
            hb.vg._http_get = saved

    def test_200_with_body_is_ok(self):
        self.assertEqual(self._probe(200, "content"), ("ok", "content"))

    def test_404_and_410_are_missing(self):
        self.assertEqual(self._probe(404)[0], "missing")
        self.assertEqual(self._probe(410)[0], "missing")

    def test_5xx_is_error(self):
        state, detail = self._probe(503)
        self.assertEqual(state, "error")
        self.assertIn("503", detail)

    def test_empty_body_on_200_is_error(self):
        # Pages 在部署切换的一瞬间会返回 200 + 空 body: 既不是"有文件", 也不是"没文件"
        self.assertEqual(self._probe(200, "  ")[0], "error")

    def test_exception_is_error(self):
        state, detail = self._probe(200, boom=TimeoutError("timed out"))
        self.assertEqual(state, "error")
        self.assertIn("TimeoutError", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
