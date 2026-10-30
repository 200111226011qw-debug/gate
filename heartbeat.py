#!/usr/bin/env python3
"""
发布后心跳 (post-publish heartbeat)
====================================
解决什么问题
------------
现有流程的问题是**时间窗**: 每 12 小时才跑一轮, 而 VPN Gate 的 SSTP 节点
经常在几十分钟内成批失效。`vpngate.py` 的 stale 保护发生在「发布层」——
它保证"这一轮产不出东西时别把订阅改差", 但管不了"发布成功之后, 12 小时内
节点死掉"。这段时间里订阅照发, 客户端拿到的是死节点, 而 gate 一无所知,
要等下一轮才知道。

本脚本补的就是这段空窗: 定期把**线上已发布**的 nodes.txt 拿回来复检,
把确认死掉的节点摘掉并重新发布, 不等 12 小时。

三条不可退让的约束
------------------
1. **只摘「确认节点侧不通」的行。**
   `target_ratelimit`(目标侧限流未测) 和 `worker_error`(检测服务异常) 一律保留。
   它们代表"没测成", 不是"测出来不通"。把未测当不通, 会在 Worker 抽风的
   那半小时里把整个订阅洗掉 —— 这是这套系统最贵的一种事故。

2. **同一节点连续 N 轮确认才摘 (HEARTBEAT_CONFIRM, 默认 2)。**
   单轮结论可能是误判 (TLS/证书/路由抖动)。摘除是破坏性操作, 需要重复证据。

3. **摘除后剩余不足时不动手 (HEARTBEAT_MIN_ALIVE)。**
   真出现大面积死亡, 正确的动作是重跑完整流水线 (重新抓源 → 重新检测),
   而不是把现存节点表砍到只剩几个。本脚本用退出码 3 把这个信号传回 workflow。

为什么复用 vpngate.py 而不是重写
-------------------------------
检测逻辑 (Worker 调用、三级归因、端口熔断、自适应限速) 已经在 vpngate.py 里
被反复修过了。心跳如果自带一套, 两边迟早会不一致 —— 比如某天 Worker 换了
错误文案, 只改了一处, 另一处开始把限流当节点不通。所以这里直接 import 复用,
只新增"采样 / 累积证据 / 摘除"这三件 vpngate.py 不需要做的事。

退出码
------
  0 = 正常 (可能没有任何改动, 见 heartbeat.json.changed)
  1 = 拿不到线上 nodes.txt, 什么也没做 (调用方不应部署)
  3 = 建议升级为完整重跑 (样本死亡比例过高, 疑似大面积失效或检测端异常)
"""

import os
import sys
import time
import json
import random
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except Exception as _exc:
    print(f"[FATAL] 无法导入 requests: {type(_exc).__name__}: {_exc}")
    print("        修复: 用同一个解释器安装依赖 -> python -m pip install -r requirements.txt")
    sys.exit(1)

import vpngate as vg

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _env_int(name, default, minimum=0):
    """读整数环境变量, 非法值回退默认值而不是让整轮崩掉 (workflow 里手抖写个空串很常见)。"""
    raw = os.environ.get(name)
    try:
        return max(minimum, int(str(raw).strip())) if raw not in (None, "") else default
    except (TypeError, ValueError):
        print(f"::warning::{name}={raw!r} 不是整数, 回退默认值 {default}", flush=True)
        return default


HEARTBEAT_BUDGET = _env_int("HEARTBEAT_BUDGET", 12, 1)          # 单轮最多复检多少个节点
HEARTBEAT_CONCURRENCY = _env_int("HEARTBEAT_CONCURRENCY", 2, 1)  # 并发必须低于主流程: 这是在已发布产物上做增量探测
HEARTBEAT_TIMEOUT = float(os.environ.get("HEARTBEAT_TIMEOUT", "30") or 30)
HEARTBEAT_CONFIRM = _env_int("HEARTBEAT_CONFIRM", 2, 1)         # 连续 N 轮判死才摘
HEARTBEAT_MIN_ALIVE = _env_int("HEARTBEAT_MIN_ALIVE", vg.MIN_SUCCESS, 1)
HEARTBEAT_MAX_DEAD_RATIO = float(os.environ.get("HEARTBEAT_MAX_DEAD_RATIO", "0.5") or 0.5)

NODES_FILENAME = "nodes.txt"
DATA_FILENAME = "data.json"
HEARTBEAT_NAME = "heartbeat.json"

# 心跳把超时压低: 它跑在定时任务里, 一轮拖过 10 分钟就有可能和下一轮重叠。
# 直接改模块级变量是因为 check_one 内部读的就是这个全局值 (见 check_one 的 session.get)。
vg.CHECK_TIMEOUT = HEARTBEAT_TIMEOUT


def _set_output(name, value):
    """写 GitHub Actions step output; 非 Actions 环境静默跳过 (本地跑不会报错)。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 解析: 把线上 nodes.txt 还原成节点表
# ---------------------------------------------------------------------------
def parse_nodes_text(text):
    """解析 "入口#名字$sstp://vpn:vpn@主机:端口" (允许入口带 :端口)。

    逐行过 vpngate.NODE_LINE_RE, 不合法的行 **原样保留**而不是丢弃 ——
    心跳不是产物清洗器。一行不合法说明产物本身有问题, 那是主流程该负责的事;
    这里静默认删掉它, 等于用一个 bug 掩盖另一个 bug。
    """
    items, skipped = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not vg.NODE_LINE_RE.match(line):
            skipped.append(line[:100])
            continue
        head, _, link = line.partition("$")
        entry, _, tag = head.partition("#")
        host, _, port = link.rsplit("@", 1)[-1].rpartition(":")
        if not host or not port.isdigit():
            skipped.append(line[:100])
            continue
        items.append({
            "line": line,
            "entry": entry,
            "tag": tag,
            "host": host.lower(),
            "port": int(port),
        })
    return items, skipped


def node_key(item):
    return f"{item['host']}:{item['port']}"


# ---------------------------------------------------------------------------
# 采样: 多轮覆盖全表, 而不是每轮只测文件开头
# ---------------------------------------------------------------------------
def select_targets(items, budget, cursor=0, streak=None):
    """挑本轮要复检的节点。

    两个优先级, 依次占用额度:
      1) 已有死链记录的节点 —— 它们只差一次确认就会出局, 先测它们性价比最高;
         但最多占一半额度, 否则一个僵死的节点群会把额度吃干净, 其余节点永远轮不到。
      2) 其余按固定步长轮转 (cursor 跨轮推进) —— nodes.txt 是按国家分组排的,
         简单取前 N 个会导致"只有日本节点被复检", 步长采样能保证 N 轮内覆盖全表。
    """
    streak = streak or {}
    if len(items) <= budget:
        return list(items), cursor

    hot = [it for it in items if streak.get(node_key(it), 0) > 0]
    rest = [it for it in items if streak.get(node_key(it), 0) == 0]

    picked, ids = [], set()

    hot_quota = min(len(hot), max(1, budget // 2))
    for it in hot[:hot_quota]:
        picked.append(it)
        ids.add(id(it))

    remain = budget - len(picked)
    if remain > 0 and rest:
        n = len(rest)
        step = max(1.0, n / float(remain))
        pos = float(cursor % n)
        for _ in range(remain):
            it = rest[int(pos) % n]
            if id(it) not in ids:
                picked.append(it)
                ids.add(id(it))
            pos += step
        # 游标必须按 budget 推进, 而不是按 pos 的最终值 —— 后者一圈下来正好回到原点
        # (pos 前进 remain*step ≈ n), 于是每一轮都采同一批节点, 其余节点永远不被复检。
        # 这是"看起来在轮转, 实际上原地踏步"的典型写法。
        cursor = (cursor + len(picked)) % n

    # 兜底: 步长采样可能因取整重复, 用剩余节点补满 (只影响小列表, 不改变覆盖性)
    if len(picked) < min(budget, len(items)):
        for it in items:
            if len(picked) >= budget:
                break
            if id(it) not in ids:
                picked.append(it)
                ids.add(id(it))

    return picked, cursor


# ---------------------------------------------------------------------------
# 判定: 纯函数, 便于离线测试 (不含任何网络调用)
# ---------------------------------------------------------------------------
def decide(items, results, prev_streak=None, confirm=2, min_alive=5, max_dead_ratio=0.5):
    """根据本轮检测结果与历史死链记录, 决定摘除哪些节点。

    results: {(host, port): check_one 的返回}
    只认 failure_kind == "node_dead" 且在结果里明确 success=False 的条目;
    worker_error / target_ratelimit / skipped_by_breaker 全部视为「未测」并保留。

    返回 dict:
      prune      : 本轮要摘掉的 key 集合
      streak     : 更新后的死链计数 (写回 heartbeat.json, 给下一轮累积证据)
      outcome    : ok | pruned | hold | escalate | notest
      reason     : 人读的原因
      alive/dead/untested : 本轮口径统计
    """
    prev_streak = dict(prev_streak or {})
    keys = {node_key(it) for it in items}

    # 清掉已经不在产物里的历史记录, 避免 heartbeat.json 无限膨胀
    streak = {k: v for k, v in prev_streak.items() if k in keys}

    dead, alive, untested = set(), set(), set()
    for item in items:
        key = node_key(item)
        r = results.get((item["host"], item["port"]))
        if r is None:
            continue
        if r.get("success"):
            alive.add(key)
            streak.pop(key, None)
            continue
        kind = r.get("failure_kind")
        if kind == "node_dead" and not r.get("skipped_by_breaker"):
            dead.add(key)
            streak[key] = streak.get(key, 0) + 1
        else:
            # 「没测成」: 计数原样保留, 既不加也不减 ——
            # 减掉会让一个已经确认过一次的节点凭空复活, 等于绕过确认机制。
            untested.add(key)

    checked = len(alive) + len(dead) + len(untested)
    if checked == 0:
        return {"prune": set(), "streak": streak, "outcome": "notest",
                "reason": "本轮一个节点都没测成 (检测端不可用或全部被熔断)",
                "alive": 0, "dead": 0, "untested": 0, "ratio": 0.0}

    # 关键: 摘除条件必须同时满足「本轮确认不通」和「历史连续确认达阈值」。
    # 只按 streak 判断会犯一个致命错误: 一个连续两轮确认死亡的节点, 若第三轮因为
    # Worker 抽风变成「未测」, streak 仍是 2, 于是被判定为可摘除 —— 用手上的旧证据
    # 去执行一个需要新证据支持的动作。dead 是本轮的实测结果, 不能省。
    prune = {k for k in dead if streak.get(k, 0) >= confirm and k in keys}
    ratio = len(dead) / float(checked)
    alive_after = len(keys) - len(prune)

    if not prune:
        reason = (f"复检 {checked} 个: 确认不通 {len(dead)} 个, 存活 {len(alive)} 个, 未测 {len(untested)} 个; "
                  f"暂无达到连续 {confirm} 轮确认的节点, 不摘除")
        return {"prune": set(), "streak": streak, "outcome": "ok", "reason": reason,
                "alive": len(alive), "dead": len(dead), "untested": len(untested), "ratio": ratio}

    if alive_after < min_alive:
        reason = (f"本应摘掉 {len(prune)} 个, 但摘后只剩 {alive_after} 个 (< HEARTBEAT_MIN_ALIVE={min_alive}); "
                  f"大面积失效应重跑完整流水线, 本轮不摘除")
        return {"prune": set(), "streak": streak, "outcome": "hold", "reason": reason,
                "alive": len(alive), "dead": len(dead), "untested": len(untested), "ratio": ratio}

    if ratio > max_dead_ratio:
        reason = (f"本轮确认不通占比 {ratio:.0%} 超过 HEARTBEAT_MAX_DEAD_RATIO={max_dead_ratio:.0%}; "
                  f"疑似大面积失效或检测端异常, 不摘除, 建议重跑完整流水线")
        return {"prune": set(), "streak": streak, "outcome": "escalate", "reason": reason,
                "alive": len(alive), "dead": len(dead), "untested": len(untested), "ratio": ratio}

    reason = (f"复检 {checked} 个: 连续 {confirm} 轮确认不通 {len(prune)} 个, 摘除后剩 {alive_after} 个")
    return {"prune": prune, "streak": streak, "outcome": "pruned", "reason": reason,
            "alive": len(alive), "dead": len(dead), "untested": len(untested), "ratio": ratio}


# ---------------------------------------------------------------------------
# 产物改写
# ---------------------------------------------------------------------------
def prune_nodes_text(items, prune):
    """按原顺序保留未被摘除的行; 原文里解析不了的行原样保留 (原因见 parse_nodes_text)。"""
    kept = []
    for raw in items:
        if node_key(raw) in prune:
            continue
        kept.append(raw["line"])
    return "\n".join(kept) + "\n" if kept else ""


def prune_data_json(data, prune):
    """同步裁剪 data.json, 避免页面/面板显示已经摘掉的节点。

    只动 "countries[*].nodes" 与 "available", stats 里与"节点数"相关的字段重算;
    raw_nodes / sstp_nodes / checked 描述的是**那一轮抓取与检测**, 不是当前存活数,
    重写它们会伪造历史, 因此保持原值, 另在 data["heartbeat"] 里记录本次裁剪。
    """
    if not isinstance(data, dict):
        return data
    # 不改调用方传进来的对象: 心跳里 data 是从磁盘读的、只用一次, 但测试和未来的
    # 复用场景 (比如先算预览再决定要不要写) 会依赖"原对象不变"。
    data = dict(data)
    dead = set(prune)

    def keep(n):
        try:
            return f"{str(n.get('host') or '').lower()}:{int(n.get('port'))}" not in dead
        except (TypeError, ValueError):
            return True

    countries = {}
    for name, grp in (data.get("countries") or {}).items():
        nodes = [n for n in (grp.get("nodes") or []) if keep(n)]
        if not nodes:
            continue
        grp = dict(grp)
        grp["nodes"] = nodes
        grp["count"] = len(nodes)
        grp["residential"] = sum(1 for n in nodes if n.get("residential") == "residential")
        grp["datacenter"] = sum(1 for n in nodes if n.get("residential") == "datacenter")
        countries[name] = grp
    data["countries"] = countries

    if isinstance(data.get("available"), list):
        data["available"] = [n for n in data["available"] if keep(n)]

    stats = data.get("stats")
    if isinstance(stats, dict):
        stats = dict(stats)
        total = sum(g["count"] for g in countries.values())
        stats["countries"] = len(countries)
        stats["success"] = total
        stats["residential_est"] = sum(g["residential"] for g in countries.values())
        stats["datacenter_est"] = sum(g["datacenter"] for g in countries.values())
        stats["failed"] = max(0, int(stats.get("checked") or 0) - total)
        data["stats"] = stats
    return data


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
def fetch_text(session, url, what):
    try:
        r = vg._http_get(session, f"{url}{'&' if '?' in url else '?'}t={int(time.time())}")
        if r.status_code != 200:
            vg.log("HEARTBEAT", f"{what} 不可用 (HTTP {r.status_code})")
            return None
        if not (r.text or "").strip():
            # HTTP 200 但空 body 与 404 在语义上等价 (站点刚部署完、Pages 缓存穿透等),
            # 所以归为"不可用"。分开报是为了排查时不会把 200 当成"拿到了"。
            vg.log("HEARTBEAT", f"{what} 内容为空 (HTTP 200), 视为不可用")
            return None
        return r.text
    except Exception as exc:
        vg.log("HEARTBEAT", f"读取 {what} 失败: {type(exc).__name__}: {exc}")
        return None


def load_prev_heartbeat(session):
    """上一轮 heartbeat.json: 死链计数必须跨轮累积, 不能存在本地 (Actions 每次都是干净目录)。"""
    text = fetch_text(session, vg.NODES_URL.rsplit("/", 1)[0] + "/" + HEARTBEAT_NAME, "上一轮 heartbeat.json")
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception as exc:
        vg.log("HEARTBEAT", f"上一轮 heartbeat.json 不是合法 JSON, 按首轮处理: {type(exc).__name__}")
        return {}


# Pages 的部署是「整站替换」而不是「增量覆盖」: upload-pages-artifact 上传什么,
# 站点上就只剩什么。心跳如果只写 nodes.txt 就部署, 其他产物 (index.html / status.json /
# check-worker.js / endpoints.json) 会**从线上消失** —— 页面变 404, 网页上取检测脚本的入口也没了。
# 所以每次部署前必须先把现有站点镜像到 public/, 再改其中需要改的文件。
_BASE = vg.NODES_URL.rsplit("/", 1)[0]
STAGE_FILES = (
    # (文件名, 是否必需)  —— 部署时整站替换, 少一个就等于从线上删掉一个
    ("data.json", False),
    ("index.html", False),
    ("status.json", False),
    ("check-worker.js", False),
    ("endpoints.json", False),
)


def stage_public(session, nodes_text):
    """把线上现存的整站产物镜像到 public/, 保证部署不会误删文件。

    返回 (ok, missing): ok 表示能构成一个可部署的站点 (nodes.txt + index.html 都在)。
    """
    os.makedirs(vg.PUBLIC_DIR, exist_ok=True)
    with open(os.path.join(vg.PUBLIC_DIR, NODES_FILENAME), "w", encoding="utf-8") as f:
        f.write(nodes_text)

    missing = []
    for name, _required in STAGE_FILES:
        text = fetch_text(session, f"{_BASE}/{name}", f"线上 {name}")
        if text is None:
            missing.append(name)
            continue
        with open(os.path.join(vg.PUBLIC_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)

    if "index.html" in missing:
        # 页面模板在仓库里有, 线上拿不到就用它兜底 —— 没有 index.html 的站点等于不可用
        try:
            with open(os.path.join(vg.PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as f:
                f.write(vg.render_html())
            missing.remove("index.html")
            vg.log("HEARTBEAT", "线上 index.html 缺失, 已用仓库模板兜底生成")
        except Exception as exc:
            vg.warn(f"生成兜底 index.html 失败: {type(exc).__name__}: {exc}")

    if missing:
        vg.log("HEARTBEAT", f"未能镜像的站点文件 (部署后可能缺失): {', '.join(missing)}")
    return ("index.html" not in missing), missing



def check_batch(targets, session, breaker):
    """复用 vpngate.check_one —— 归因、熔断、限速口径与主流程保持完全一致。"""
    limiter = vg.AdaptiveLimiter(vg.CHECK_MIN_INTERVAL, window=vg.GLOBAL_RL_WINDOW, ratio=vg.GLOBAL_RL_RATIO,
                                 pause=vg.GLOBAL_RL_PAUSE, max_interval=vg.ADAPTIVE_MAX_INTERVAL)
    results = {}
    with ThreadPoolExecutor(max_workers=HEARTBEAT_CONCURRENCY) as pool:
        futures = [pool.submit(vg.check_one, {"host": t["host"], "port": t["port"]}, session, limiter, breaker)
                   for t in targets]
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as exc:
                # 单个节点抛异常不该带崩整轮: 记为未测 (worker_error), decide 会保留它
                vg.log("HEARTBEAT", f"  检测线程异常: {type(exc).__name__}: {exc}")
                continue
            results[(r["host"].lower(), r["port"])] = r
    return results, limiter


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    session = requests.Session()
    started = time.time()

    vg.log("HEARTBEAT", f"预算 {HEARTBEAT_BUDGET} 个 / 并发 {HEARTBEAT_CONCURRENCY} / 超时 {HEARTBEAT_TIMEOUT}s / "
                        f"连续 {HEARTBEAT_CONFIRM} 轮确认才摘 / 摘后下限 {HEARTBEAT_MIN_ALIVE}")

    if not vg.WORKER_URL_OK:
        vg.warn("CHECK_WORKER 未配置或非法, 心跳无法检测 — 本轮不做任何改动")
        _set_output("changed", "false")
        _set_output("escalate", "false")
        return 0

    nodes_text = fetch_text(session, vg.NODES_URL, "线上 nodes.txt")
    if not nodes_text:
        # 线上产物都拿不到时唯一正确的动作是「什么都不做」:
        # 本地 public/ 是空的, 一旦继续往下走并部署, 就等于用空目录覆盖线上站点。
        vg.warn("拿不到线上 nodes.txt — 心跳跳过, 本次不部署 (线上保持原样)")
        _set_output("changed", "false")
        _set_output("escalate", "false")
        return 1

    items, skipped = parse_nodes_text(nodes_text)
    if skipped:
        vg.warn(f"线上 nodes.txt 有 {len(skipped)} 行无法解析, 已原样保留: {skipped[0][:80]}")
    if not items:
        vg.warn("线上 nodes.txt 解析出 0 个节点 — 产物异常, 心跳跳过 (交由主流程处理)")
        _set_output("changed", "false")
        _set_output("escalate", "false")
        return 1

    prev = load_prev_heartbeat(session)
    prev_streak = prev.get("dead_streak") or {}
    cursor = int(prev.get("cursor") or 0)
    vg.log("HEARTBEAT", f"线上节点 {len(items)} 个, 历史死链记录 {len(prev_streak)} 条, 采样游标 {cursor}")

    targets, next_cursor = select_targets(items, HEARTBEAT_BUDGET, cursor, prev_streak)
    vg.log("HEARTBEAT", f"本轮复检 {len(targets)} 个: " + ", ".join(f"{t['host']}:{t['port']}" for t in targets[:6]) +
                        (" ..." if len(targets) > 6 else ""))

    breaker = vg.PortCircuitBreaker(vg.PORT_CB_THRESHOLD)
    results, limiter = check_batch(targets, session, breaker)
    tripped = breaker.tripped()
    if tripped:
        vg.warn(f"目标侧限流熔断端口 {', '.join(str(p) for p in tripped)} — 这些端口本轮「未测」, 涉及的节点一律保留")

    verdict = decide(items, results, prev_streak, confirm=HEARTBEAT_CONFIRM,
                     min_alive=HEARTBEAT_MIN_ALIVE, max_dead_ratio=HEARTBEAT_MAX_DEAD_RATIO)
    vg.log("HEARTBEAT", verdict["reason"])

    # 先把整站镜像到 public/, 再在其上做修改 —— 顺序不能反 (原因见 stage_public 注释)
    staged, missing = stage_public(session, nodes_text)
    deploy_enabled = True
    if not staged:
        vg.warn("无法构成完整站点 (缺 index.html) — 本轮放弃部署, 只更新本地 heartbeat.json, 线上保持原样")
        deploy_enabled = False

    status = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration_s": round(time.time() - started, 1),
        "outcome": verdict["outcome"],
        "reason": verdict["reason"],
        "changed": bool(verdict["prune"]),
        "published_nodes": len(items),
        "checked": verdict["alive"] + verdict["dead"] + verdict["untested"],
        "alive": verdict["alive"],
        "dead": verdict["dead"],
        "untested": verdict["untested"],
        "dead_ratio": round(verdict["ratio"], 3),
        "pruned": sorted(verdict["prune"]),
        "pruned_count": len(verdict["prune"]),
        "remaining": len(items) - len(verdict["prune"]),
        "dead_streak": verdict["streak"],
        "cursor": next_cursor,
        "budget": HEARTBEAT_BUDGET,
        "confirm": HEARTBEAT_CONFIRM,
        "min_alive": HEARTBEAT_MIN_ALIVE,
        "rate_limited_ports": tripped,
        "throttle": limiter.snapshot() if hasattr(limiter, "snapshot") else {},
        "run_id": os.environ.get("GITHUB_RUN_ID") or None,
    }

    if verdict["prune"]:
        new_text = prune_nodes_text(items, verdict["prune"])
        problems = vg.validate_nodes_text(new_text)
        if problems:
            # 自检不过就不写: 宁可这轮不摘, 也不能把坏产物发出去。
            vg.warn(f"摘除后的 nodes.txt 未通过格式自检, 放弃本次改写: {problems[0]}")
            status.update({"outcome": "hold", "changed": False,
                           "reason": f"摘除后产物格式自检未通过: {problems[0]}", "pruned": [], "pruned_count": 0})
        else:
            with open(os.path.join(vg.PUBLIC_DIR, NODES_FILENAME), "w", encoding="utf-8") as f:
                f.write(new_text)
            vg.log("HEARTBEAT", f"已写入 {NODES_FILENAME}: 摘掉 {len(verdict['prune'])} 行, 剩 {status['remaining']} 行")

            data_path = os.path.join(vg.PUBLIC_DIR, DATA_FILENAME)
            if os.path.exists(data_path):
                try:
                    with open(data_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data = prune_data_json(data, verdict["prune"])
                    data["heartbeat"] = {"generated_at": status["generated_at"], "removed": status["pruned_count"],
                                         "remaining": status["remaining"], "dead_streak": verdict["streak"]}
                    vg.write_json(data_path, data)
                    vg.log("HEARTBEAT", f"已同步裁剪 {DATA_FILENAME}")
                except Exception as exc:
                    # data.json 裁剪失败不阻断: nodes.txt 才是订阅本体, 页面显示稍滞后可以接受
                    vg.warn(f"data.json 裁剪失败, 本轮只更新 nodes.txt: {type(exc).__name__}: {exc}")
            else:
                vg.warn("线上 data.json 未镜像到本地, 本轮只更新 nodes.txt (页面显示会滞后到下一轮主流程)")
    else:
        vg.log("HEARTBEAT", "无需改动, 不重写任何产物")

    vg.write_json(os.path.join(vg.PUBLIC_DIR, HEARTBEAT_NAME), status)

    if verdict["outcome"] == "escalate":
        vg.warn("建议升级为完整重跑 (workflow 将执行 vpngate.py)")
        _set_output("escalate", "true")
    else:
        _set_output("escalate", "false")
    _set_output("changed", "true" if status["changed"] else "false")
    _set_output("pruned", str(status["pruned_count"]))
    _set_output("remaining", str(status["remaining"]))
    # deploy=false 时 workflow 跳过 Pages 部署 —— 只用于「站点不完整」这一种情况,
    # 因为整站替换会把缺失的文件从线上删掉。其余情况一律部署, 让 heartbeat.json 保持新鲜。
    _set_output("deploy", "true" if deploy_enabled else "false")

    vg.log("HEARTBEAT", f"完成: outcome={status['outcome']} changed={status['changed']} 耗时 {status['duration_s']}s")
    if verdict["outcome"] == "escalate":
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        vg.die(f"心跳程序异常: {type(exc).__name__}: {exc} (完整 traceback 见上方)")
