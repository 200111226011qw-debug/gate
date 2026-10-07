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
RUNS = TESTS / "runs"


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
    ("csv_spaced_header", "官方 CSV 表头带空格 + 数据行带 * 前缀 (解析器容错)", {"FAKE_CSV_STYLE": "spaced"}),
    ("partial_429", "部分节点可用、部分撞 429 上游限速", {"FAKE_WORKER": "partial_429", "MIN_SUCCESS": "1"}),
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
        "csv_spaced_header": {"exit": 0, "nodes": ("gt", 0)},
        "partial_429": {"exit": 0, "nodes": 4, "bug": "429 被当成节点不通, 结果里只剩 4 个且无任何告警"},
        "worker_config_empty": {"exit": 1, "nodes": None, "max_seconds": 5,
                                "bug": "Worker 地址为空 -> 每个请求瞬时异常, 秒级退出 1"},
        "low_success": {"exit": 0, "nodes": 2, "bug": "只有 2 个节点也照发, 订阅被改差"},
        "no_lastgood": {"exit": 1, "nodes": None},
    },
    "fixed": {
        # 加固版: 能用上一版就用上一版 (退出码 0), 只有彻底无内容可发布才退出 1
        "ci_real_env": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        "happy": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        "worker_429_all": {"exit": 0, "nodes": 8, "status": "stale", "min_worker_requests": 25},
        "worker_503_all": {"exit": 0, "nodes": 8, "status": "stale", "min_worker_requests": 25},
        "sources_down": {"exit": 0, "nodes": 8, "status": "stale"},
        "mirror_only": {"exit": 0, "nodes": ("gt", 0), "status": "fresh"},
        "csv_spaced_header": {"exit": 0, "nodes": ("gt", 0), "status": "fresh", "source_contains": "vpngate.net"},
        "partial_429": {"exit": 0, "nodes": 4, "status": "fresh", "min_worker_requests": 12,
                        "min_status_worker_errors": 1},
        "worker_config_empty": {"exit": 0, "nodes": 8, "status": "stale", "max_seconds": 10},
        "low_success": {"exit": 0, "nodes": 8, "status": "stale"},
        "no_lastgood": {"exit": 1, "nodes": None},
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

    worker_requests = 0
    log_path = run_dir / "fake.log"
    if log_path.exists():
        worker_requests = sum(1 for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.startswith("worker:"))

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
        "worker_requests": worker_requests,
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
    if expect.get("min_status_worker_errors") and (result["status_worker_errors"] or 0) < expect["min_status_worker_errors"]:
        problems.append(f"status.worker_errors={result['status_worker_errors']} < 期望 >= {expect['min_status_worker_errors']} (429 未被识别为检测服务异常)")
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
