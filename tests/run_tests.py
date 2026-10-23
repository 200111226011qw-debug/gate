"""离线场景测试: 对比「原版 vpngate.py」与「加固版 vpngate.py」在各种故障下的行为。

运行: python tests/run_tests.py
产物: tests/results.json  + 控制台对比表 (退出码 / nodes.txt 行数 / status.outcome / Worker 请求数 / 耗时)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 节点行格式: 入口[#名字]$sstp://vpn:vpn@主机:端口 —— 只数行数是不够的 (2026-10-07 的教训)
NODE_LINE_RE = re.compile(
    r"^(?P<entry>[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?)#(?P<name>[^$]+)"
    r"\$sstp://vpn:vpn@(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*):(?P<port>\d+)$"
)

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
# TEST_RUNS_DIR: 换一个全新的空目录即可跳过 rmtree (沙箱环境对批量删除有限制时很有用)
RUNS = Path(os.environ["TEST_RUNS_DIR"]) if os.environ.get("TEST_RUNS_DIR") else (TESTS / "runs")


def _first_existing(*paths):
    for p in paths:
        if p.exists():
            return p
    return None


# 被测脚本: 仓库里就是根目录的 vpngate.py; 本工作区在 out/ 下; 对照版本放在 snapshots/ 下 (没有就跳过)
SCRIPT_CANDIDATES = {
    "repo": ROOT / "vpngate.py",
    "out": ROOT / "out" / "vpngate.py",
    "head": ROOT / "snapshots" / "head" / "gate-main" / "vpngate.py",
    "pushed": ROOT / "snapshots" / "pushed" / "vpngate.py",
}
WEB_DIR = _first_existing(ROOT / "web", ROOT / "snapshots" / "head" / "gate-main" / "web")

TARGETS = {name: (path, WEB_DIR) for name, path in SCRIPT_CANDIDATES.items() if path.exists()}
# 「加固版」预期适用于 repo / out / pushed; 「原版」预期只给 head
HARDENED_TARGETS = [n for n in ("repo", "out", "pushed") if n in TARGETS]
if not HARDENED_TARGETS:
    raise SystemExit("找不到被测脚本: 需要仓库根目录的 vpngate.py 或 out/vpngate.py")

BASE_ENV = {
    "CHECK_WORKER": "https://worker.example/check?sstp=vpn:vpn@",
    "CHECK_CONCURRENCY": "4",
    "CHECK_TIMEOUT": "5",
    "CHECK_RETRIES": "2",
    "CHECK_BACKOFF": "0.2",
    "CHECK_MIN_INTERVAL": "0",
    "FETCH_RETRIES": "2",
    "FETCH_BACKOFF": "0.2",
    "MIN_SUCCESS": "5",
    "EDGE_DNS_CHECK": "0",
    "EDGE_HOSTS": "entry-a.example:443,entry-b.example:443,entry-c.example:443",
    # 显式给出锁定名单, 让原版脚本能走完 import (原版还有另一个 import 阶段的致命 bug, 见 no_edge_env 场景)
    "EDGE_HOST_LOCKED": "entry-a.example:443,entry-b.example:443,entry-c.example:443",
    "NODES_URL": "https://last.good/gate/nodes.txt",
    "DATA_URL": "https://last.good/gate/data.json",
    # 关闭内置自动优选: 回归必须全程离线可复现, 且入口池固定才能比对产物
    "OPTIMAL_API": "off",
    # 全局退避暂停在回归里压到 0.5s, 避免测试套件被限流退避拖慢 (生产默认 10s)
    "GLOBAL_RL_PAUSE": "0.5",
    "FAKE_NODES": "12",
    "FAKE_DELAY": "0.001",
    "PYTHONIOENCODING": "utf-8",
}

SCENARIOS = [
    ("ci_real_env", "CI 真实环境 (不设 EDGE_HOSTS/EDGE_HOST_LOCKED) —— 2026-10-06 失败现场",
     {"EDGE_HOSTS": None, "EDGE_HOST_LOCKED": None}),
    ("happy", "一切正常", {}),
    ("worker_429_all", "检测 Worker 全量返回 429 (上游限速)", {"FAKE_WORKER": "all429"}),
    ("worker_503_all", "检测 Worker 全部 503", {"FAKE_WORKER": "http503"}),
    ("sources_down", "两个数据源都不可用", {"FAKE_PRIMARY": "timeout", "FAKE_MIRROR": "timeout"}),
    ("mirror_only", "官方 API 挂, 走 GitHub 镜像", {"FAKE_PRIMARY": "http500"}),
    ("mirror_404", "镜像 404 不重试 (URL 配错快速失败, 与 4xx 不重试口径一致)", {"FAKE_PRIMARY": "http500", "FAKE_MIRROR": "http404"}),
    ("csv_spaced_header", "官方 CSV 表头带空格 + 数据行带 * 前缀 (解析器容错)", {"FAKE_CSV_STYLE": "spaced"}),
    ("partial_429", "部分节点可用、部分撞 429 上游限速", {"FAKE_WORKER": "partial_429", "MIN_SUCCESS": "1"}),
    ("port_cb", "整个端口被目标侧限流 (:443 全灭) —— 端口熔断后额度让给其它端口",
     {"FAKE_WORKER": "all429", "FAKE_SAME_PORT": "443", "FAKE_NODES": "20", "PORT_CB_THRESHOLD": "4"}),
    ("rl_breakdown", "目标侧限流必须单列归因, 不能混进「节点不通」", {"FAKE_WORKER": "all429", "FAKE_NODES": "10"}),
    ("worker_http403", "Worker 路由配错返回 403 —— 归服务异常, 不能算节点不通",
     {"FAKE_WORKER": "http403", "FAKE_NODES": "10"}),
    ("budget_new_first", "额度受限时, 检测名额必须优先给新节点 (PREFER_NEW_NODES)",
     {"FAKE_NODES": "12", "FAKE_MIX_LASTGOOD": "4", "CHECK_BUDGET": "5"}),
    ("budget_truncate", "MAX_CHECK_NODES 截断后, candidates_in_source 必须仍是源规模 (不跟随截断)",
     {"FAKE_WORKER": "all429", "FAKE_SAME_PORT": "443", "FAKE_NODES": "20",
      "MAX_CHECK_NODES": "10", "PORT_CB_THRESHOLD": "4"}),
    ("worker_config_empty", "CHECK_WORKER 未配置 (secrets.DOMAIN 缺失/被清空)", {"CHECK_WORKER": ""}),
    ("low_success", "只有 2 个节点可用 (< MIN_SUCCESS)", {"FAKE_WORKER": "mixed_low"}),
    ("no_lastgood", "数据源全挂 + 没有上一版产物", {"FAKE_PRIMARY": "timeout", "FAKE_MIRROR": "timeout", "FAKE_LASTGOOD": "missing"}),
]

EXPECT = {
    "head": {
        # 原版: 要么发空订阅(退出码 0, 静默), 要么直接判失败(退出码 1)
        "ci_real_env": {"exit": 1, "nodes": None, "max_seconds": 3,
                        "bug": "import 阶段 AttributeError: 'tuple' object has no attribute 'split' -> 秒级退出 1 (2026-10-06 那次失败)"},
        "happy": {"exit": 0, "nodes": ("gt", 0)},
        "worker_429_all": {"exit": 0, "nodes": 0, "bug": "把 429 当成节点不通 -> 发布空 nodes.txt 且运行仍为绿色"},
        "worker_503_all": {"exit": 1, "nodes": None, "bug": "Worker 全 503 -> 整轮判失败"},
        "sources_down": {"exit": 1, "nodes": None, "bug": "数据源抖动 -> 整轮判失败"},
        "mirror_only": {"exit": 0, "nodes": ("gt", 0)},
        "mirror_404": {"exit": 1, "nodes": None, "min_mirror_requests": 2,
                       "bug": "数据源全挂 -> 整轮判失败; 且 404 也吃满重试次数 (2 次), 白等一轮退避"},
        "csv_spaced_header": {"exit": 0, "nodes": ("gt", 0)},
        "partial_429": {"exit": 0, "nodes": 4, "bug": "429 被当成节点不通, 结果里只剩 4 个且无任何告警"},
        "worker_config_empty": {"exit": 1, "nodes": None, "max_seconds": 5,
                                "bug": "Worker 地址为空 -> 每个请求瞬时异常, 秒级退出 1"},
        "low_success": {"exit": 0, "nodes": 2, "bug": "只有 2 个节点也照发, 订阅被改差"},
        "no_lastgood": {"exit": 1, "nodes": None},
        # 以下 2 个场景是 2026-10-21 新增的治理能力, 原版没有, 因此原版预期为「照旧滥发请求」
        "port_cb": {"exit": 0, "nodes": 8, "min_worker_requests": 20,
                    "bug": "不会按端口熔断 -> 20 个节点全打一遍, 全部撞 429"},
        "rl_breakdown": {"exit": 0, "nodes": 8, "min_worker_requests": 10,
                         "bug": "429 被当成节点不通, 归因与「节点真不通」混为一谈"},
        "worker_http403": {"exit": 0, "nodes": 8, "min_worker_requests": 30,
                           "bug": "403 被当成节点不通 -> node_dead 虚高, 掩盖真实的配置问题"},
        "budget_new_first": {"exit": 0, "nodes": ("gt", 0), "min_worker_requests": 10,
                             "bug": "不识别新节点, 额度按源顺序消耗, 老节点挤占检测名额"},
        "budget_truncate": {"exit": 0, "nodes": ("gt", 0),
                            "bug": "无 candidates_in_source 字段, 无法区分「源规模」与「截断后规模」"},
    },
    "fixed": {
        # 加固版: 能用上一版就用上一版 (退出码 0), 只有彻底无内容可发布才退出 1
        "ci_real_env": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        "happy": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        # worker_429_all: 12 个节点端口各不相同, 每个端口只失败 1 次 -> 不会触发端口熔断,
        # 但 429 属于「目标侧限流」, 默认不重试 (TARGET_RL_RETRIES=0), 因此请求数从
        # 旧的 36 次 (12×3) 降到 12 次。改这条预期的原因见 vpngate.py 顶部注释:
        # 旧逻辑把 429 当「检测服务异常」重试 2 次, 实测对持续限流无效且会加剧限流。
        "worker_429_all": {"exit": 0, "nodes": 8, "status": "stale", "max_worker_requests": 10,
                           "status_breakdown": {"target_ratelimit": 10}},
        "worker_503_all": {"exit": 0, "nodes": 8, "status": "stale", "min_worker_requests": 25},
        "sources_down": {"exit": 0, "nodes": 8, "status": "stale"},
        "mirror_only": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        # 404 属于「请求本身有问题」: 必须只发 1 次请求就放弃并切换数据源。
        # 若有人把 4xx 塞回重试链, mirror 请求数会变成 FETCH_RETRIES(2) 次, 在这里变红。
        "mirror_404": {"exit": 0, "nodes": 8, "status": "stale", "max_mirror_requests": 1},
        "csv_spaced_header": {"exit": 0, "nodes": ("gt", 0), "status": "fresh", "source_contains": "vpngate.net"},
        # 10 个 SSTP 节点: 4 成功 + 2 撞 429 + 4 节点不通。
        # 若 429 仍被重试 (旧行为) 请求数会是 4 + 2×3 + 4 = 14; 现在应为 10 —— 这条断言
        # 用来锁死「目标侧限流不重试」, 一旦有人把它塞回重试链就会在这里失败。
        "partial_429": {"exit": 0, "nodes": 4, "status": "fresh", "max_worker_requests": 10,
                        "min_status_worker_errors": 1},
        "worker_config_empty": {"exit": 0, "nodes": 8, "status": "stale", "max_seconds": 10},
        "low_success": {"exit": 0, "nodes": 8, "status": "stale"},
        "no_lastgood": {"exit": 1, "nodes": None},
        # 端口熔断: 20 个节点全在 :443, 连续 4 次 429 后熔断 -> 请求数必须明显少于 20
        # port_cb: 20 个节点全在 :443 (FAKE_SAME_PORT), 其中 i%6==5 的 3 个是 UDP 被过滤,
        # 故去重后源里 :443 恰好 17 个。写死 17 是为了锁住 candidates_in_source 的统计来源
        # —— 它必须来自「去重后未经任何截断」的全集, 而不是被 MAX_CHECK_NODES 截过的池。
        # 注意本场景 MAX_CHECK_NODES=0 (BASE_ENV 未设置 -> 默认 0), 因此 uniq == uniq_all,
        # candidates == candidates_in_source == 17, 两个值相等。这条断言只在「有人把
        # candidates_in_source 的统计源改回 uniq」时才会变红 —— 而要真正触发这个分叉,
        # 变异验证必须额外设 MAX_CHECK_NODES < 17 (例如 10), 否则改回 uniq 也测不出来。
        # 换句话说: 日常回归里这条断言绿, 不代表「分叉路径被覆盖」, 它防的是代码被改回去。
        # 分叉路径本身的自动覆盖见 budget_truncate 场景。
        "port_cb": {"exit": 0, "nodes": 8, "status": "stale", "max_worker_requests": 16,
                    "status_rate_limited_ports": [443], "port_detail_invariant": True,
                    "port_detail_expect": [{"port": 443, "candidates_in_source": 17, "candidates": 17}]},
        # 归因: 9 个 SSTP 节点全撞 429, 必须全部归到 target_ratelimit, 一个都不许算 node_dead
        # 9 个 SSTP 节点端口各不相同、各撞一次 429, 不触发熔断 -> 正好用来守卫
        # 「未熔断但出现 429 的端口也必须进 port_429_detail」(否则判不了阈值是否设太晚)
        "rl_breakdown": {"exit": 0, "nodes": 8, "status": "stale",
                         "status_breakdown": {"target_ratelimit": 9, "node_dead": 0},
                         "port_detail_nonempty": True},
        # 10 个 SSTP 节点全部撞 403: 归 worker_error (非节点侧), 且不重试 -> 请求数 == 10。
        # 若误归 node_dead, status_breakdown.node_dead 会 > 0; 若重试, 请求数会是 30。
        "worker_http403": {"exit": 0, "nodes": 8, "status": "stale", "max_worker_requests": 10,
                           "status_breakdown": {"node_dead": 0}},
        # 12 个节点里前 4 个复用上一版主机名 (已知), 后 6 个是新的; CHECK_BUDGET=5
        # -> 5 个名额必须全给新节点, 一个都不许落到 vpn90000000X 上。
        # 这是 PREFER_NEW_NODES 唯一会被真正执行的场景 (生产 CHECK_BUDGET=0 时它不起作用)。
        "budget_new_first": {"exit": 0, "nodes": 8, "status": "stale", "max_worker_requests": 5,
                             "checked_prefix_absent": "vpn90000000"},
        # 分叉路径的自动守卫: MAX_CHECK_NODES=10 时 uniq (10) != uniq_all (17)。
        # 断言 candidates_in_source 必须仍是 17 (源规模), candidates 是 10 (截断后)。
        # 这样「把统计源改回 uniq」的回归就由日常回归兜住, 不再依赖人工变异验证。
        "budget_truncate": {"exit": 0, "nodes": 8, "status": "stale", "max_worker_requests": 10,
                            "port_detail_invariant": True,
                            "port_detail_expect": [{"port": 443, "candidates_in_source": 17, "candidates": 10}]},
    },
}

# 线上已推送版与 out/ 加固版行为应完全一致, 因此共用同一套预期
EXPECT["_hardened"] = EXPECT["fixed"]
for _name in HARDENED_TARGETS:
    EXPECT[_name] = dict(EXPECT["_hardened"])
TARGET_ORDER = [n for n in (["head"] + HARDENED_TARGETS) if n in TARGETS]


def run_case(target, scenario, extra_env):
    script, web = TARGETS[target]
    run_dir = RUNS / f"{target}-{scenario}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "web").mkdir(parents=True)
    shutil.copy2(script, run_dir / "vpngate.py")
    if web is not None and (web / "index.html").exists():
        shutil.copy2(web / "index.html", run_dir / "web" / "index.html")

    env = dict(os.environ)
    env.update(BASE_ENV)
    for k, v in extra_env.items():
        if v is None:
            env.pop(k, None)          # None = 刻意不设置该变量 (模拟 CI 真实环境)
        else:
            env[k] = v
    env["PUBLIC_DIR"] = str(run_dir / "public")
    env["FAKE_LOG"] = str(run_dir / "fake.log")
    env["PYTHONPATH"] = str(TESTS)

    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(run_dir / "vpngate.py")],
        cwd=str(run_dir), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    seconds = time.time() - started

    nodes_path = run_dir / "public" / "nodes.txt"
    status_path = run_dir / "public" / "status.json"
    nodes_lines = None
    bad_lines = []
    entry_hosts = []
    if nodes_path.exists():
        text = nodes_path.read_text(encoding="utf-8")
        nodes_lines = len([ln for ln in text.splitlines() if ln.strip()])
        for i, ln in enumerate((l for l in text.splitlines() if l.strip()), 1):
            m = NODE_LINE_RE.match(ln)
            if not m:
                bad_lines.append(f"L{i}:{ln[:60]}")
                continue
            entry_hosts.append(m.group("entry").split(":")[0])
    status = None
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))

    checked_hosts = []
    worker_requests = 0
    mirror_requests = 0
    log_path = run_dir / "fake.log"
    if log_path.exists():
        for ln in log_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith("mirror:"):
                mirror_requests += 1
                continue
            if not ln.startswith("worker:"):
                continue
            worker_requests += 1
            target = ln.split(":", 2)[-1]          # 形如 host:port
            checked_hosts.append(target.rsplit(":", 1)[0])

    (run_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")

    return {
        "target": target, "scenario": scenario,
        "exit": proc.returncode, "seconds": round(seconds, 2),
        "nodes_lines": nodes_lines,
        "bad_lines": bad_lines,
        "entry_hosts": sorted(set(entry_hosts)),
        "status_outcome": (status or {}).get("outcome"),
        "status_reason": (status or {}).get("reason"),
        "status_source": (status or {}).get("source"),
        "status_worker_errors": (status or {}).get("worker_errors"),
        "status_breakdown": (status or {}).get("failure_breakdown"),
        "status_rate_limited_ports": (status or {}).get("rate_limited_ports"),
        "status_port_detail": (status or {}).get("port_429_detail"),
        "worker_requests": worker_requests,
        "mirror_requests": mirror_requests,
        "checked_hosts": sorted(set(checked_hosts)),
        "has_warning_annotation": "::warning::" in proc.stdout,
        "stdout_tail": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
    }


def check(expect, result):
    problems = []
    if result["exit"] != expect["exit"]:
        problems.append(f"退出码 {result['exit']} != 期望 {expect['exit']}")
    # 只要产出了节点行, 就必须逐行合法、入口必须是完整域名 (防「按字符拆开」这类事故)
    if result["nodes_lines"]:
        if result["bad_lines"]:
            problems.append(f"有 {len(result['bad_lines'])} 行节点格式非法, 例如 {result['bad_lines'][:2]}")
        bad_entry = [h for h in result["entry_hosts"] if "." not in h]
        if bad_entry:
            problems.append(f"入口不是域名: {bad_entry[:5]}")
    nodes = expect.get("nodes")
    if nodes is None:
        if result["nodes_lines"] is not None:
            problems.append(f"不应生成 nodes.txt, 实际 {result['nodes_lines']} 行")
    elif isinstance(nodes, tuple) and nodes[0] == "gt":
        if not (result["nodes_lines"] or 0) > nodes[1]:
            problems.append(f"nodes.txt 行数 {result['nodes_lines']} 应 > {nodes[1]}")
    else:
        if result["nodes_lines"] != nodes:
            problems.append(f"nodes.txt 行数 {result['nodes_lines']} != 期望 {nodes}")
    if "status" in expect and result["status_outcome"] != expect["status"]:
        problems.append(f"status.outcome={result['status_outcome']} != 期望 {expect['status']}")
    if expect.get("min_worker_requests") and result["worker_requests"] < expect["min_worker_requests"]:
        problems.append(f"Worker 请求数 {result['worker_requests']} < 期望 >= {expect['min_worker_requests']} (重试未生效)")
    if expect.get("max_worker_requests") is not None and result["worker_requests"] > expect["max_worker_requests"]:
        problems.append(f"Worker 请求数 {result['worker_requests']} > 期望 <= {expect['max_worker_requests']} (429 未生效治理, 仍在滥发请求)")
    if expect.get("max_mirror_requests") is not None and result["mirror_requests"] > expect["max_mirror_requests"]:
        problems.append(f"镜像源请求数 {result['mirror_requests']} > 期望 <= {expect['max_mirror_requests']} (4xx 不应重试)")
    if expect.get("min_mirror_requests") and result["mirror_requests"] < expect["min_mirror_requests"]:
        problems.append(f"镜像源请求数 {result['mirror_requests']} < 期望 >= {expect['min_mirror_requests']} (该版本应重试 404 — 若不重试了, 说明行为变化, 请同步更新预期)")
    if expect.get("status_breakdown"):
        got = result["status_breakdown"] or {}
        for k, v in expect["status_breakdown"].items():
            if got.get(k) != v:
                problems.append(f"status.failure_breakdown.{k}={got.get(k)} != 期望 {v}")
    if expect.get("status_rate_limited_ports") is not None:
        got = result["status_rate_limited_ports"] or []
        if list(expect["status_rate_limited_ports"]) != list(got):
            problems.append(f"status.rate_limited_ports={got} != 期望 {expect['status_rate_limited_ports']}")
    if expect.get("min_status_worker_errors") and (result["status_worker_errors"] or 0) < expect["min_status_worker_errors"]:
        problems.append(f"status.worker_errors={result['status_worker_errors']} < 期望 >= {expect['min_status_worker_errors']} (429 未被识别为检测服务异常)")
    if expect.get("port_detail_invariant"):
        # 不变式: 每个熔断端口的 candidates == probed_429 + skipped (端口上的候选要么被
        # 实测、要么被跳过)。不依赖并发数, 因此断言是确定的。
        got = result["status_port_detail"] or []
        if not got:
            problems.append("status.rate_limited_ports_detail 缺失, 无法判断熔断是否过度误杀")
        for d in got:
            if not isinstance(d, dict) or "port" not in d:
                problems.append(f"rate_limited_ports_detail 元素结构异常: {d!r}")
                continue
            if not isinstance(d.get("port"), int):
                problems.append(f"rate_limited_ports_detail.port 应为 int, 实际 {type(d.get('port')).__name__} ({d.get('port')!r}) — JSON key 字符串化陷阱")
            # 不变式 (守恒) 只在「该端口发生了熔断」时成立: 熔断后该端口所有候选要么被实测、
            # 要么被跳过。未熔断的端口可能有成功节点或其它失败, candidates 会 > probed_429 + skipped。
            if d.get("skipped"):
                if d.get("candidates") != (d.get("probed_429") or 0) + (d.get("skipped") or 0):
                    problems.append(f"端口 {d.get('port')} 计数不守恒: candidates={d.get('candidates')} != probed_429={d.get('probed_429')} + skipped={d.get('skipped')}")
            if not d.get("candidates"):
                problems.append(f"端口 {d.get('port')} candidates 为 0, 无法判断是否过度误杀")
            # 截断关闭时 (CHECK_BUDGET=0) 两个口径应相等; 截断开启后 source >= planned
            if d.get("candidates_in_source") is None:
                problems.append(f"端口 {d.get('port')} 缺 candidates_in_source (无法判断是否整段误杀)")
            elif d.get("candidates_in_source") < d.get("candidates"):
                problems.append(f"端口 {d.get('port')} candidates_in_source={d.get('candidates_in_source')} < candidates={d.get('candidates')} (源总数不可能小于计划检测数)")
    if expect.get("port_detail_nonempty"):
        # 扩展语义的守卫: 未触发熔断的端口, 只要出现 429, 也必须出现在 port_429_detail 里。
        # 否则就退回「只在熔断时才记录」的老问题 —— 拿不到 candidates_in_source 就判不了阈值。
        got = result["status_port_detail"] or []
        if not got:
            problems.append("port_429_detail 为空: 有 429 但没熔断的端口也应出现在 detail 里 (扩展语义未生效)")
        for d in got:
            if not isinstance(d.get("port"), int):
                problems.append(f"port_429_detail.port 应为 int, 实际 {type(d.get('port')).__name__}")
            if d.get("skipped") != 0:
                problems.append(f"port_429_detail 出现 skipped={d.get('skipped')} (该场景不应有熔断端口)")
    if expect.get("port_detail_expect"):
        # 写死具体数值: 防止有人「优化」candidates_in_source 的统计来源
        # (比如改回用被 MAX_CHECK_NODES 截断后的 uniq 来算) 而断言仍然绿。
        #
        # 变异验证前提: 下面比较的是「绝对值」, 而 port_cb 场景 MAX_CHECK_NODES=0,
        # 两种统计口径结果相同 (都是 17)。要把这条断言当作「分叉哨兵」验证一次,
        # 需临时加 MAX_CHECK_NODES=10 并故意把 vpngate.py 里的统计源改回 uniq ——
        # 此时应报 candidates_in_source=10 != 期望 17。做完记得还原,
        # 不要把这个设置留在 BASE_ENV (会让其它场景的候选数一起变)。
        got = result["status_port_detail"] or []
        for want in expect["port_detail_expect"]:
            hit = [d for d in got if d.get("port") == want["port"]]
            if not hit:
                problems.append(f"rate_limited_ports_detail 里没有 port={want['port']}: {got}")
                continue
            d = hit[0]
            for k, v in want.items():
                if k == "port" or d.get(k) == v:
                    continue
                problems.append(f"端口 {want['port']} 的 {k}={d.get(k)} != 期望 {v}")
    if expect.get("checked_prefix_absent"):
        hit = [h for h in result["checked_hosts"] if h.startswith(expect["checked_prefix_absent"])]
        if hit:
            problems.append(f"额度被老节点占用: 检测到 {hit[:3]} (新节点优先未生效)")
        if not result["checked_hosts"]:
            problems.append("没有任何节点被检测, 无法验证新节点优先")
    if expect.get("source_contains") and expect["source_contains"] not in (result["status_source"] or ""):
        problems.append(f"status.source={result['status_source']!r} 不含 {expect['source_contains']!r} (没有用上官方源)")
    if expect.get("max_seconds") and result["seconds"] > expect["max_seconds"]:
        problems.append(f"耗时 {result['seconds']}s > 期望 <= {expect['max_seconds']}s")
    return problems


def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    results = []
    print(f"{'场景':<22}{'版本':<7}{'退出码':<7}{'nodes.txt':<11}{'status':<8}{'Worker请求':<11}{'耗时':<7}结论")
    print("-" * 100)
    for scenario, title, extra in SCENARIOS:
        for target in TARGET_ORDER:
            res = run_case(target, scenario, extra)
            problems = check(EXPECT[target][scenario], res)
            res["expectation_met"] = not problems
            res["problems"] = problems
            res["scenario_title"] = title
            results.append(res)
            verdict = "PASS" if not problems else "FAIL: " + "; ".join(problems)
            print(f"{scenario:<22}{target:<7}{res['exit']:<7}{str(res['nodes_lines']):<11}"
                  f"{str(res['status_outcome']):<8}{res['worker_requests']:<11}{res['seconds']:<7}{verdict}")

    (TESTS / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    failures = [r for r in results if not r["expectation_met"]]
    print("-" * 100)
    print(f"合计 {len(results)} 组用例, 未达预期 {len(failures)} 组")
    for f in failures:
        print(f"  - {f['target']}/{f['scenario']}: {'; '.join(f['problems'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
