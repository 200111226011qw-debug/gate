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
  4 = 站点镜像遇到临时故障 (503/超时等), 本轮不部署 —— 部署会删掉没镜像到的文件

规模假设 (必读)
---------------
每轮复检 HEARTBEAT_BUDGET 个节点, 所以「覆盖一整张节点表」需要 ceil(N/budget) 轮,
「同一节点拿到 confirm 次确认」需要 ceil(N/budget)*confirm 小时。要让它落在 12 小时的
发布周期内 (否则 check.yml 的整站部署会先把证据清掉, 见下), 就必须:
    ceil(N/budget) * confirm <= 12
  N=17  budget=12 confirm=2  ->  1*2 = 2h     ✓
  N=100 budget=12 confirm=2  ->  9*2 = 18h    ✗ (需要 budget>=17)
  N=200 budget=12 confirm=2  -> 17*2 = 34h    ✗ (需要 budget>=34)
budget 写 "auto" 时按上式自动取值 (下限 12, 上限 48)。超出时会在 heartbeat.json 里写
scale_warning 并打 ::warning:: —— 那时功能会退化成「只测不摘」, 不能假装看不见。

状态为什么要跨发布保留
---------------------
Pages 部署是整站替换: 谁部署, 站点上就只剩谁的 public/。check.yml 的产物里没有
heartbeat.json, 所以它在部署完的瞬间会把死链证据清零。check.yml 里那一步
"Preserve heartbeat state" 就是为此存在的 —— 没有它, confirm=2 在 N 稍大时永远凑不齐。
"""

import os
import sys
import math
import time
import json
import random
import traceback
import re
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


_BUDGET_RAW = (os.environ.get("HEARTBEAT_BUDGET") or "").strip()
HEARTBEAT_BUDGET_AUTO = _BUDGET_RAW.lower() == "auto"
HEARTBEAT_BUDGET = 12 if HEARTBEAT_BUDGET_AUTO else _env_int("HEARTBEAT_BUDGET", 12, 1)  # 单轮最多复检多少个节点
HEARTBEAT_BUDGET_FLOOR = _env_int("HEARTBEAT_BUDGET_FLOOR", 12, 1)
HEARTBEAT_BUDGET_CEIL = _env_int("HEARTBEAT_BUDGET_CEIL", 48, 1)
HEARTBEAT_CONCURRENCY = _env_int("HEARTBEAT_CONCURRENCY", 2, 1)  # 并发必须低于主流程: 这是在已发布产物上做增量探测
HEARTBEAT_TIMEOUT = float(os.environ.get("HEARTBEAT_TIMEOUT", "30") or 30)
HEARTBEAT_CONFIRM = _env_int("HEARTBEAT_CONFIRM", 2, 1)         # 连续 N 轮判死才摘
HEARTBEAT_MIN_ALIVE = _env_int("HEARTBEAT_MIN_ALIVE", vg.MIN_SUCCESS, 1)
HEARTBEAT_MAX_DEAD_RATIO = float(os.environ.get("HEARTBEAT_MAX_DEAD_RATIO", "0.5") or 0.5)
# 连续触发完整重跑的轮数上限: 超过后只记录不重跑, 把修复交回给 12 小时的 check.yml。
# 没有这道闸, 一批长期死透的节点会让每小时都跑一遍上百请求的完整流水线。
HEARTBEAT_ESCALATE_MAX = _env_int("HEARTBEAT_ESCALATE_MAX", 2, 0)
# 单轮检测结果的时序记录 (JSONL): 0 = 关闭, auto = 按「每轮行数 x 24 x 天数」反推。
# 写死行数是错的: 4000 行在 N=17 时够两周, 在 N=288、每轮全测时只够 3.5 天 ——
# 「保留约两周」这句话必须由 N 决定, 而不是一个拍脑袋的常数。
_HISTORY_RAW = (os.environ.get("HEARTBEAT_HISTORY_MAX") or "auto").strip().lower()
HEARTBEAT_HISTORY_AUTO = _HISTORY_RAW in ("", "auto")
HEARTBEAT_HISTORY_MAX = 0 if _HISTORY_RAW == "0" else _env_int("HEARTBEAT_HISTORY_MAX", 4000, 1)
HEARTBEAT_HISTORY_DAYS = _env_int("HEARTBEAT_HISTORY_DAYS", 7, 1)
HEARTBEAT_HISTORY_FLOOR = _env_int("HEARTBEAT_HISTORY_FLOOR", 4000, 1)

# 发布周期 (小时): check.yml 的 cron 是 */12。只用于规模自检, 不影响调度。
PUBLISH_PERIOD_H = 12

NODES_FILENAME = "nodes.txt"
DATA_FILENAME = "data.json"
HEARTBEAT_NAME = "heartbeat.json"
HISTORY_NAME = "heartbeat-history.jsonl"

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


def resolve_budget(n):
    """HEARTBEAT_BUDGET=auto 时按「confirm 轮确认要落在发布周期内」反推每轮预算。

    优先「一轮全测」: 当 n <= CEIL 时直接测完所有节点, 确认窗口 = confirm 小时。
    这不是为了省事, 是因为分批会让窗口成倍变长 —— N=17 时 ceil(17/12)*2 = 4h,
    一轮全测后只要 2h, 而这正是心跳存在的意义 (把发现死节点的时间压到最短)。

    n > CEIL 时才退回分批: 按 ceil(n / (PUBLISH_PERIOD_H / confirm)) 反推, 保证
    ceil(n/budget)*confirm <= PUBLISH_PERIOD_H。上限 CEIL 是请求量闸门 —— 它同时意味着
    N > CEIL*PUBLISH_PERIOD_H/confirm (= 288, 默认值下) 时 auto 也无法保证时效,
    那时只剩 scale_warning 起作用, 不能假装 auto 永远有效。
    """
    if not HEARTBEAT_BUDGET_AUTO:
        return HEARTBEAT_BUDGET
    if n <= HEARTBEAT_BUDGET_CEIL:
        return max(HEARTBEAT_BUDGET_FLOOR, n)
    per_round_hours = max(1, PUBLISH_PERIOD_H // max(1, HEARTBEAT_CONFIRM))
    need = int(math.ceil(n / float(per_round_hours)))
    return max(HEARTBEAT_BUDGET_FLOOR, min(HEARTBEAT_BUDGET_CEIL, need))


def resolve_history_max(per_round):
    """时序记录的保留行数。auto = 每轮行数 x 24 轮/天 x 天数, 且不低于 FLOOR。

    固定 4000 行只在 N 很小的时候够两周; 一旦一轮全测 48 个节点, 4000 行只剩 3.5 天,
    两周后回头看只剩最近三四天的数据, 「按历史存活率排序」就没有历史可用了。
    """
    if not HEARTBEAT_HISTORY_AUTO:
        return HEARTBEAT_HISTORY_MAX
    return max(HEARTBEAT_HISTORY_FLOOR, per_round * 24 * HEARTBEAT_HISTORY_DAYS)


def scale_warning(n, budget):
    """N 太大时必须主动喊出来。

    否则故障表现是「每小时都在跑、从来不摘任何节点」—— 最容易被误读成「节点都很健康」。
    """
    rounds = int(math.ceil(n / float(max(1, budget))))
    window = rounds * HEARTBEAT_CONFIRM
    if window <= PUBLISH_PERIOD_H:
        return None
    need = int(math.ceil(n * HEARTBEAT_CONFIRM / float(PUBLISH_PERIOD_H)))
    return (f"{n} 个节点 / 每轮 {budget} 个 -> 覆盖一轮 {rounds}h, 连续 {HEARTBEAT_CONFIRM} 轮确认要 {window}h "
            f"> 发布周期 {PUBLISH_PERIOD_H}h: 死链证据会随 check 部署失效, 实际退化成「只测不摘」。"
            f"调法: HEARTBEAT_BUDGET=auto, 或把预算提到 >= {need}")


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
        # countries / success / residential_est / datacenter_est 描述**当前状态**: 与 available 同步裁剪,
        # 否则面板会说「日本 5 个」而 available 里只剩 4 个。
        stats["countries"] = len(countries)
        stats["success"] = total
        stats["residential_est"] = sum(g["residential"] for g in countries.values())
        stats["datacenter_est"] = sum(g["datacenter"] for g in countries.values())
        # failed 保持「checked - 当前可用」: checked 是那一轮的历史, success 是当前,
        # 两者相减的语义是「检查过但现在用不了」(含被心跳摘掉的), 面板上仍能自洽。
        # raw_nodes / sstp_nodes / checked / carried_over 一律不动 —— 它们是那一轮的快照。
        stats["failed"] = max(0, int(stats.get("checked") or 0) - total)
        data["stats"] = stats
    return data


# ---------------------------------------------------------------------------
# 时序记录: 每轮检测结果追加一行 JSONL
# ---------------------------------------------------------------------------
def history_lines(items, results, pruned, ts):
    """把本轮每个被测节点摊成一行。

    这是「按历史存活率排序」唯一缺的东西 —— 数据本身是检测的副产品, 攒的是时间。
    字段刻意用短名: 这个文件会长期增长 (默认上限 4000 行), 每行多 20 字节就是 80KB。
    """
    out = []
    for item in items:
        r = results.get((item["host"], item["port"]))
        if r is None:
            continue
        out.append(json.dumps({
            "t": ts,
            "h": item["host"],
            "p": item["port"],
            "ok": bool(r.get("success")),
            "kind": r.get("failure_kind"),
            "ms": r.get("latency_ms"),
            "act": "pruned" if node_key(item) in pruned else "kept",
        }, ensure_ascii=False, separators=(",", ":")))
    return out


def write_history(session, new_lines):
    """追加到 JSONL 并保留最近 resolve_history_max() 行。

    两种「读不到」必须分开, 因为它们该触发完全相反的动作:
      * 404 (从没跑过 / 首轮): 从空开始, 正常追加。
      * 5xx/超时 (文件在线上, 这一瞬间没拿到): **本轮不追加**。用空历史覆盖线上,
        等于把攒了几周的数据一次删光, 比少记一轮严重得多。
    注意「不追加」只影响这个文件: 判定、摘除、heartbeat.json 全都照常 ——
    时序记录是可观测性的副产品, 不能反过来阻塞主判定。
    """
    keep = resolve_history_max(len(new_lines))
    if keep <= 0 or not new_lines:
        return None
    state, value = fetch_probe(session, f"{_BASE}/{HISTORY_NAME}", "线上历史记录")
    if state == "error":
        vg.warn(f"历史记录读取失败 ({value}) — 本轮只跳过追加, 判定与摘除照常")
        return None
    old = [ln for ln in (value or "").splitlines() if ln.strip()] if state == "ok" else []
    merged = (old + new_lines)[-keep:]
    with open(os.path.join(vg.PUBLIC_DIR, HISTORY_NAME), "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + "\n")
    vg.log("HEARTBEAT", f"历史记录 {len(old)} -> {len(merged)} 行 (上限 {keep}, {HISTORY_NAME})")
    return len(merged)


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
def _status_of(exc):
    """从异常里抠出 HTTP 状态码。

    vg._http_get 对 4xx 是**抛 _NoRetryHTTPError**、对 5xx 是重试后抛 RuntimeError,
    也就是说 404 永远走不到 `r.status_code` 那个分支。要区分「文件不存在」和「这次没读到」,
    只能从异常里把状态码取回来 —— 否则一个首次部署还没生成 endpoints.json 的站点,
    会被判成「读取失败」而永久跳过部署。

    三级取值, 都不猜:
      1. exc.status_code —— 自定义异常带的话直接用。
      2. exc.response.status_code —— requests.HTTPError (408/429 重试耗尽时抛的那个)
         只挂 response, 没有 status_code; 少了这一步就只能靠正则去啃字符串。
      3. `\bHTTP[ /](\\d{3})\\b` —— vg 自己的文案是 "HTTP 404 (不重试)" / "HTTP 502",
         加词边界避免把 "...HTTP 5020..." 这类无关数字当成状态码。
    取不到就返回 None, 由调用方按「读失败」处理 (保守方向, 不会误删线上文件)。
    """
    code = getattr(exc, "status_code", None)
    if not isinstance(code, int):
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\bHTTP[ /](\d{3})\b", str(exc))
    return int(m.group(1)) if m else None


def fetch_probe(session, url, what):
    """返回 (state, value): state ∈ {"ok", "missing", "error"}。

    「线上没有这个文件」和「这次没读到」必须分开, 因为它们的正确反应完全相反:
      * missing (404/410): 该文件本来就不存在 —— 首轮、或某个可选产物没生成。可以继续。
      * error  (5xx/超时/空 body): 文件在线上, 只是这一瞬间没拿到。若当成 missing 处理,
        镜像就不完整, 整站部署会把那个文件从线上删掉 —— 一次网络抖动换一个线上故障。
    """
    try:
        r = vg._http_get(session, f"{url}{'&' if '?' in url else '?'}t={int(time.time())}")
    except Exception as exc:
        if _status_of(exc) in (404, 410):
            return "missing", None
        return "error", f"{type(exc).__name__}: {exc}"
    if r.status_code in (404, 410):
        return "missing", None
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code}"
    if not (r.text or "").strip():
        # HTTP 200 + 空 body: Pages 在部署切换的瞬间会这样。既不能当"有内容", 也不能当
        # "没有这个文件", 归为 error 让调用方放弃本轮部署最安全。
        return "error", "HTTP 200 但内容为空"
    return "ok", r.text


def fetch_text(session, url, what):
    state, value = fetch_probe(session, url, what)
    if state != "ok":
        vg.log("HEARTBEAT", f"{what} 不可用 ({state}: {value or '不存在'})")
        return None
    return value


def load_prev_heartbeat(session, published=0):
    """上一轮 heartbeat.json: 死链计数必须跨轮累积, 不能存在本地 (Actions 每次都是干净目录)。

    线上「没有 heartbeat.json」有两种原因, 后果完全不同:
      * 首轮: 正常, 从空状态开始。
      * 状态被清掉: check.yml 的 Preserve 步骤失败 (或人为清理)。此时 streak 归零,
        连判过一轮的节点要重新攒够 confirm 轮 —— 功能静默退化, 而表面上一切正常。
    单看这一个文件无法区分两者 (没有第三方状态可对照), 所以只能在「明明有已发布节点、
    却读不到状态」时喊一声, 让日志里至少留下痕迹。
    """
    state, text = fetch_probe(session, vg.NODES_URL.rsplit("/", 1)[0] + "/" + HEARTBEAT_NAME,
                              "上一轮 heartbeat.json")
    if state == "missing":
        if published > 0:
            vg.warn(f"线上有 {published} 个已发布节点, 却读不到 heartbeat.json — "
                    f"首轮, 或状态被上一次 check 部署清掉了 (死链证据从零重攒, 本轮不会摘任何节点)")
        return {}
    if state == "error":
        vg.warn(f"读取 heartbeat.json 失败 ({text}) — 本轮按无历史处理, 已攒的死链证据这轮用不上")
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

    返回 (ok, missing, errors):
      ok      : 能构成一个可部署的站点 (index.html 有着落, 且没有临时读取失败)
      missing : 线上确实不存在 (404) —— 允许缺, 但要记录
      errors  : 存在但这次没读到 (5xx / 超时 / 空 body) —— 有任何一个就不许部署
    """
    os.makedirs(vg.PUBLIC_DIR, exist_ok=True)
    with open(os.path.join(vg.PUBLIC_DIR, NODES_FILENAME), "w", encoding="utf-8") as f:
        f.write(nodes_text)

    missing, errors = [], []
    for name, _required in STAGE_FILES:
        state, value = fetch_probe(session, f"{_BASE}/{name}", f"线上 {name}")
        if state == "missing":
            missing.append(name)
            continue
        if state != "ok":
            errors.append(f"{name}: {value}")
            continue
        with open(os.path.join(vg.PUBLIC_DIR, name), "w", encoding="utf-8") as f:
            f.write(value)

    if "index.html" in missing:
        # 页面模板在仓库里有, 线上确实没有就用它兜底 —— 没有 index.html 的站点等于不可用
        try:
            with open(os.path.join(vg.PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as f:
                f.write(vg.render_html())
            missing.remove("index.html")
            vg.log("HEARTBEAT", "线上 index.html 不存在, 已用仓库模板兜底生成")
        except Exception as exc:
            vg.warn(f"生成兜底 index.html 失败: {type(exc).__name__}: {exc}")

    if missing:
        vg.log("HEARTBEAT", f"线上不存在的站点文件 (404, 部署后仍缺): {', '.join(missing)}")
    if errors:
        # 这条不能降级成 warning: 缺文件的整站部署 = 主动删线上文件
        vg.warn(f"镜像站点时读取失败, 本轮不许部署 (否则会从线上删掉这些文件): {'; '.join(errors)}")
    return ("index.html" not in missing) and not errors, missing, errors



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

    vg.log("HEARTBEAT", f"并发 {HEARTBEAT_CONCURRENCY} / 超时 {HEARTBEAT_TIMEOUT}s / "
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

    prev = load_prev_heartbeat(session, len(items))
    prev_streak = prev.get("dead_streak") or {}
    prev_escalate = int(prev.get("escalate_streak") or 0)
    cursor = int(prev.get("cursor") or 0)

    # 预算必须按真实节点数定, 不能只看环境变量: 同一个值在 N=17 时够用, 在 N=200 时
    # 会让确认窗口远远超出发布周期, 而症状只是「从来不摘」。
    budget = resolve_budget(len(items))
    scale = scale_warning(len(items), budget)
    if scale:
        vg.warn(scale)
    vg.log("HEARTBEAT", f"线上节点 {len(items)} 个, 本轮预算 {budget} 个, 历史死链记录 {len(prev_streak)} 条, "
                        f"采样游标 {cursor}" + (f", 上一轮连续升级重跑 {prev_escalate} 次" if prev_escalate else ""))

    targets, next_cursor = select_targets(items, budget, cursor, prev_streak)
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
    staged, missing, stage_errors = stage_public(session, nodes_text)
    deploy_enabled = staged
    if stage_errors:
        vg.warn("镜像站点时遇到临时读取失败 — 本轮不部署, 线上保持原样 (整站替换会删掉没镜像到的文件)")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    checked = verdict["alive"] + verdict["dead"] + verdict["untested"]

    # 连续升级重跑的闸门: 完整流水线一轮上百个请求, 不能每小时都跑一次。
    # 超过 HEARTBEAT_ESCALATE_MAX 后仍然记录状态, 但不再重跑 —— 长期大面积失效是 12h 主流程的活,
    # 每小时重跑只会把配额和 Actions 分钟数一起烧掉, 并不能让节点变活。
    escalate_streak = (prev_escalate + 1) if verdict["outcome"] == "escalate" else 0
    escalate_allowed = verdict["outcome"] == "escalate" and escalate_streak <= HEARTBEAT_ESCALATE_MAX
    outcome, reason = verdict["outcome"], verdict["reason"]
    if outcome == "escalate" and not escalate_allowed:
        outcome = "escalate_cooldown"
        reason = (f"{reason}; 已连续 {escalate_streak} 轮触发升级, 超过 HEARTBEAT_ESCALATE_MAX="
                  f"{HEARTBEAT_ESCALATE_MAX}, 本轮只记录不重跑 (等 check.yml 的 12h 主流程)")

    status = {
        "generated_at": ts,
        "duration_s": round(time.time() - started, 1),
        "outcome": outcome,
        "reason": reason,
        "changed": bool(verdict["prune"]),
        "published_nodes": len(items),
        "checked": checked,
        "alive": verdict["alive"],
        "dead": verdict["dead"],
        "untested": verdict["untested"],
        # untested 占比是「心跳是否还有效」的体检指标: 持续高于 30% 说明检测额度不够
        # (大量 worker_error / 熔断), 此时心跳实际上在「只测不摘」。
        "untested_ratio": round(verdict["untested"] / float(checked), 3) if checked else 0.0,
        "dead_ratio": round(verdict["ratio"], 3),
        "pruned": sorted(verdict["prune"]),
        "pruned_count": len(verdict["prune"]),
        "remaining": len(items) - len(verdict["prune"]),
        "dead_streak": verdict["streak"],
        "escalate_streak": escalate_streak,
        "cursor": next_cursor,
        "budget": budget,
        "budget_auto": HEARTBEAT_BUDGET_AUTO,
        "confirm": HEARTBEAT_CONFIRM,
        "min_alive": HEARTBEAT_MIN_ALIVE,
        "rate_limited_ports": tripped,
        "throttle": limiter.snapshot() if hasattr(limiter, "snapshot") else {},
        "run_id": os.environ.get("GITHUB_RUN_ID") or None,
        "deploy": deploy_enabled,
        "staging_missing": missing,
        "staging_errors": stage_errors,
    }
    if scale:
        status["scale_warning"] = scale

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

    # 时序记录独立于摘除动作: 没摘也要记, 否则「存活率」永远只有失败样本。
    history_count = write_history(session, history_lines(items, results, verdict["prune"], ts))
    if history_count is not None:
        status["history_lines"] = history_count

    vg.write_json(os.path.join(vg.PUBLIC_DIR, HEARTBEAT_NAME), status)

    if escalate_allowed:
        vg.warn(f"建议升级为完整重跑 (workflow 将执行 vpngate.py; 第 {escalate_streak}/{HEARTBEAT_ESCALATE_MAX} 次)")
        _set_output("escalate", "true")
    else:
        if verdict["outcome"] == "escalate":
            vg.warn("本轮已进入升级冷却期, 不再重跑完整流水线")
        _set_output("escalate", "false")
    _set_output("changed", "true" if status["changed"] else "false")
    _set_output("pruned", str(status["pruned_count"]))
    _set_output("remaining", str(status["remaining"]))
    # deploy=false 时 workflow 跳过 Pages 部署 —— 用于「站点不完整」或「镜像遇到临时故障」,
    # 因为整站替换会把缺失的文件从线上删掉。其余情况一律部署, 让 heartbeat.json 保持新鲜。
    _set_output("deploy", "true" if deploy_enabled else "false")

    vg.log("HEARTBEAT", f"完成: outcome={status['outcome']} changed={status['changed']} 耗时 {status['duration_s']}s")

    if stage_errors:
        return 4
    if escalate_allowed:
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
