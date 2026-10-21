#!/usr/bin/env python3
"""
VPN Gate SSTP 节点检测流水线
=============================
流程:
  1. 获取 VPN Gate 原始节点 (多数据源 + 指数退避重试)
  2. 只保留带 TCP 入口的 SSTP 节点
  3. 去重
  4. 并发调用检测 Worker (限速 + 重试; Worker 侧 429/5xx 单独归类)
  5. 生成 public/data.json + public/index.html + public/nodes.txt + public/status.json

健壮性约定 (加固说明):
  * 任何一环不可用都「不发布空订阅」: 自动沿用上一次已发布的产物 (Pages 上的 nodes.txt/data.json),
    并以退出码 0 结束, 同时打印 ::warning:: 注解与诊断摘要。设 STRICT=1 可让降级也返回失败退出码。
  * 只有「既拿不到新数据、又没有上一版产物」时才以退出码 1 结束 —— 此时不部署, 线上站点保持原样。
  * 所有异常都打印完整 traceback, 便于在 Actions 日志里一眼定位。
"""

import base64
import csv
import io
import json
import os
import random
import re
import socket
import struct
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

try:
    import requests
except Exception as _exc:  # 依赖缺失/损坏时给出可执行的修复提示, 而不是一行难懂的 traceback
    print(f"[FATAL] 无法导入 requests: {type(_exc).__name__}: {_exc}")
    print("        修复: 用同一个解释器安装依赖 -> python -m pip install -r requirements.txt")
    sys.exit(1)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _env_csv(name, default):
    """读取逗号分隔的环境变量, 返回「非空条目」列表 (list, 不是字符串); 未设置或为空时用 default。

    这里集中做两件容易出错的事, 调用方不再自己处理:
      1) 类型归一化: default 允许是字符串或字符串序列。旧版直接把元组 _DEFAULT_EDGE_HOSTS
         传给 os.environ.get 的默认值 → 返回 tuple → 再 .split(",") 会在 import 阶段抛
         AttributeError, 整轮运行 0 秒退出 1 (2026-10-06 那次失败就是这个原因)。
      2) 保证返回值是「条目列表」: 2026-10-07 的回归事故是调用方拿着字符串直接 for 遍历,
         于是按「字符」拆成了 2447 个单字母入口, 生成的 nodes.txt 每行入口变成单个字母。
         返回 list 之后, 任何调用方都不可能再忘记 split。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        raw = ",".join(default) if isinstance(default, (tuple, list)) else str(default)
    return [item.strip() for item in str(raw).split(",") if item.strip()]

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

VPNGATE_API = os.environ.get("VPNGATE_API", "http://www.vpngate.net/api/iphone/")
VPNGATE_MIRROR = os.environ.get(
    "VPNGATE_MIRROR",
    "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/main/json/data.json",
)
# 数据源: 按顺序尝试, 全部失败才判定「数据源不可用」。格式 "csv:<url>" / "json:<url>", 也可直接写 URL。
VPNGATE_SOURCES = [
    s.strip()
    for s in _env_csv("VPNGATE_SOURCES", f"csv:{VPNGATE_API},json:{VPNGATE_MIRROR}")
    if s.strip()
]
FETCH_RETRIES = max(1, int(os.environ.get("FETCH_RETRIES", "3")))      # 单个数据源最多尝试次数
FETCH_BACKOFF = float(os.environ.get("FETCH_BACKOFF", "3"))            # 重试退避基数(秒), 线性增长+抖动

# 检测 Worker 地址, 形如 https://xxx.workers.dev/check?sstp=vpn:vpn@ (Actions 里由 secrets.DOMAIN 注入)
WORKER_CHECK_URL = (os.environ.get("CHECK_WORKER") or "").strip()
WORKER_PLACEHOLDER = "https://你的域名/check?sstp=vpn:vpn@"
WORKER_URL_OK = bool(re.match(r"^https?://[^\s/]+/", WORKER_CHECK_URL)) and "你的域名" not in WORKER_CHECK_URL
CONCURRENCY = max(1, int(os.environ.get("CHECK_CONCURRENCY", "4")))
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "90"))
CHECK_RETRIES = max(0, int(os.environ.get("CHECK_RETRIES", "2")))      # 检测服务异常时的额外重试次数
CHECK_BACKOFF = float(os.environ.get("CHECK_BACKOFF", "2"))            # 检测重试退避基数(秒)
CHECK_MIN_INTERVAL = float(os.environ.get("CHECK_MIN_INTERVAL", "0.25"))  # 请求最小间隔(秒), 防止打爆 Worker 上游额度
MAX_CHECK_NODES = int(os.environ.get("MAX_CHECK_NODES", "0"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))

# --- 目标侧限流治理 (2026-10-21) -------------------------------------------
# 实测结论: 检测 Worker 返回的 "Target /api/lookup request failed: HTTP/1.1 429"
# 是「目标服务器对 Cloudflare 出口 IP 的限流」, 不是 Worker 上游额度问题。
# 两轮重复测试: :443 端口 100% 返回 429, 非 443 端口(995/1250~1957)大多成功。
# 因此 429 必须单独归类、默认不重试 —— 旧逻辑把它当成「检测服务异常」重试 2 次
# (退避仅 2~5 秒), 对持续限流无效, 反而把请求量放大 3 倍、加剧限流。
CHECK_BUDGET = max(0, int(os.environ.get("CHECK_BUDGET", "0")))             # 单轮最多检测多少个节点, 0=不限
PORT_CB_THRESHOLD = max(1, int(os.environ.get("PORT_CB_THRESHOLD", "4")))   # 同一端口连续 N 次目标侧限流即熔断
TARGET_RL_RETRIES = max(0, int(os.environ.get("TARGET_RL_RETRIES", "0")))   # 目标侧限流的额外重试次数, 默认 0
GLOBAL_RL_WINDOW = float(os.environ.get("GLOBAL_RL_WINDOW", "60"))          # 全局退避滑窗(秒)
GLOBAL_RL_RATIO = float(os.environ.get("GLOBAL_RL_RATIO", "0.35"))          # 滑窗内限流占比超过该值才全局退避
GLOBAL_RL_PAUSE = float(os.environ.get("GLOBAL_RL_PAUSE", "10"))            # 全局暂停(秒)
ADAPTIVE_MAX_INTERVAL = float(os.environ.get("ADAPTIVE_MAX_INTERVAL", "5")) # 请求最小间隔的自适应上限(秒)
# 检测额度优先给「上一版没出现过的新节点」—— 直接对症「抓不到新节点」
PREFER_NEW_NODES = os.environ.get("PREFER_NEW_NODES", "1") not in ("0", "off", "false", "False")
# 合并发布: 本轮通过的节点 + 上一版仍可用节点。默认关闭 (不改变现有行为), 置 1 开启
MERGE_LAST_GOOD = os.environ.get("MERGE_LAST_GOOD", "0") in ("1", "true", "True")
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", os.path.join(REPO_DIR, "public"))
TEMPLATE_HTML = os.path.join(REPO_DIR, "web", "index.html")
STATUS_NAME = "status.json"

# 降级策略
MIN_SUCCESS = max(1, int(os.environ.get("MIN_SUCCESS", "5")))          # 成功数低于该值时, 若上一版更多则保留上一版
KEEP_LAST_GOOD = os.environ.get("KEEP_LAST_GOOD", "1") not in ("0", "false", "False")
STRICT = os.environ.get("STRICT", "0") in ("1", "true", "True")        # 1 = 降级也算失败 (退出码 1)

# 端点体检 (edgetunnel / 订阅入口等)。注意: 这与 nodes.txt 里的「优选入口域名」是两层东西 ——
#   nodes.txt 的入口 = 客户端实际连接的 Cloudflare 优选域名 (由 filter_edge_hosts 检测)
#   这里探测的端点 = 你自己的订阅/Worker 域名 (如 https://zai.ppdk.eu.cc/)
# 只做体检并写入 endpoints.json / status.json, **绝不参与 nodes.txt 生成**, 因此不会打坏链式代理。
ENDPOINT_URLS = _env_csv("ENDPOINT_URLS", "")                          # 逗号分隔; 空 = 关闭该功能 (零行为变化)
ENDPOINT_TIMEOUT = float(os.environ.get("ENDPOINT_TIMEOUT", "15"))
_EDGE_ERROR_CODES = (520, 521, 522, 523, 524, 525, 526, 527)
_endpoint_report = []                                                  # 由 main() 填充, build_status() 读取

DATA_CENTER_ORG_KEYWORDS = [
    "GOOGLE", "AMAZON", "AWS", "MICROSOFT", "OVH", "HETZNER", "DIGITALOCEAN",
    "AKAMAI", "CLOUDFLARE", "FASTLY", "RACKSPACE", "EQUINIX", "LINODE", "VULTR",
    "HURRICANE", "TENCENT", "ALIBABA", "ALIYUN", "LEASWEB",
]
RESIDENTIAL_ORG_KEYWORDS = [
    "NTT EAST", "NTT WEST", "NTT COMMUNICATIONS", "NTT BROADBAND", "KDDI", "DOCOMO",
    "SOFTBANK", "AU COMMUNICATIONS", "J:COM", "JCOM", "OCN", "BIGLOBE",
    "IIJ", "SEIKO", "CLEVER-NET", "AT&T", "COMCAST", "XFINITY", "VERIZON",
    "TELUS", "ROGERS", "BELL CANADA", "VODAFONE", "ORANGE", "DEUTSCHE TELEKOM",
    "BREEZE", "TIM S.P.A", "LIBERO", "FASTWEB", "FREE FRANCE", "BT OPEN",
]

COUNTRY_ZH = {
    "JP": "日本", "KR": "韩国", "US": "美国", "CA": "加拿大", "RU": "俄罗斯",
    "RO": "罗马尼亚", "TH": "泰国", "VN": "越南", "DE": "德国", "FR": "法国",
    "GB": "英国", "UK": "英国", "SG": "新加坡", "TW": "台湾", "HK": "香港",
    "CN": "中国", "AU": "澳大利亚", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "IT": "意大利", "ES": "西班牙", "PL": "波兰", "IN": "印度", "BR": "巴西",
    "MX": "墨西哥", "ID": "印度尼西亚", "MY": "马来西亚", "PH": "菲律宾",
    "TR": "土耳其", "UA": "乌克兰", "CZ": "捷克", "GR": "希腊", "PT": "葡萄牙",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "BE": "比利时",
    "AT": "奥地利", "HU": "匈牙利", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "NZ": "新西兰", "ZA": "南非", "IL": "以色列", "AE": "阿联酋", "SA": "沙特",
    "EG": "埃及", "HR": "克罗地亚", "BY": "白俄罗斯", "GD": "格林纳达",
    "LV": "拉脱维亚", "EE": "爱沙尼亚", "LT": "立陶宛", "SK": "斯洛伐克",
    "SI": "斯洛文尼亚", "BG": "保加利亚", "RS": "塞尔维亚", "GE": "格鲁吉亚",
    "MD": "摩尔多瓦", "AM": "亚美尼亚", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "MN": "蒙古", "NP": "尼泊尔", "LK": "斯里兰卡", "MM": "缅甸",
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_section = None

def log(section, msg=""):
    global _section
    if section != _section:
        print(f"========== {section} ==========")
        _section = section
    if msg:
        print(msg, flush=True)

def warn(msg):
    """非致命问题: 打印告警并生成 GitHub Actions 注解 (运行页可见)。"""
    log("WARN", f"[降级] {msg}")
    print(f"::warning::{msg}", flush=True)

def die(msg):
    log("FATAL", f"[失败] {msg}")
    print(f"::error::{msg}", flush=True)
    sys.exit(1)

# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------
def _http_get(session, url, **kwargs):
    """带指数退避重试的 GET。网络抖动 / 5xx 会重试, 最后一次失败抛异常。"""
    last_exc = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = session.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"},
                **kwargs,
            )
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < FETCH_RETRIES:
                delay = FETCH_BACKOFF * attempt + random.uniform(0, 1.5)
                log("VPN GATE", f"  第 {attempt}/{FETCH_RETRIES} 次尝试失败 ({type(exc).__name__}: {exc}), {delay:.1f}s 后重试")
                time.sleep(delay)
    raise last_exc


def fetch_vpngate(session):
    """按顺序尝试所有数据源; 全部失败返回 (None, None) —— 交由 main 走降级流程, 不直接结束进程。"""
    for spec in VPNGATE_SOURCES:
        if spec.lower().startswith(("http://", "https://")):
            kind = "json" if spec.lower().split("?")[0].endswith(".json") else "csv"
            url = spec
        else:
            kind, _, url = spec.partition(":")
            kind = (kind or "csv").lower()
        host = url.split("//")[-1].split("/")[0] if "//" in url else url
        try:
            log("VPN GATE", f"尝试数据源 [{kind}] {url}")
            resp = _http_get(session, url)
            rows = parse_mirror_json(resp.json()) if kind == "json" else parse_csv(resp.text)
            if not rows:
                raise RuntimeError("解析结果 0 行")
            log("VPN GATE", f"数据源可用: {host} -> {len(rows)} 个原始节点")
            return rows, host
        except Exception as exc:
            log("VPN GATE", f"数据源失败: {url} -> {type(exc).__name__}: {exc}")
    return None, None

def parse_csv(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        # 容错: 官方 API 是 "#HostName,...", 但有的镜像/副本会写成 "# HostName,..."
        if ln.lstrip("#").strip().lower().startswith("hostname"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("找不到 CSV 表头行 (HostName)")

    header = lines[header_idx].lstrip("#").split(",")
    data_lines = lines[header_idx + 1:]
    idx = {}
    for col in ("hostname", "ip", "countrylong", "countryshort", "openvpn_configdata_base64"):
        for i, h in enumerate(header):
            if h.strip().lstrip("*").lower() == col:
                idx[col] = i
                break
    if "openvpn_configdata_base64" not in idx:
        for i, h in enumerate(header):
            if "base64" in h.lower():
                idx["openvpn_configdata_base64"] = i
                break
    pos = {"hostname": idx.get("hostname", 0), "ip": idx.get("ip", 1), "countrylong": idx.get("countrylong", 5), "countryshort": idx.get("countryshort", 6), "openvpn_configdata_base64": idx.get("openvpn_configdata_base64", len(header) - 1)}

    rows = []
    for ln in data_lines:
        fields = next(csv.reader(io.StringIO(ln)))
        if len(fields) < 7: continue
        host = fields[pos["hostname"]].strip().lstrip("*")   # 官方 CSV 文件版会给数据行加 "*" 前缀
        ip = fields[pos["ip"]].strip()
        if not host or not ip: continue
        rows.append({"host": host, "ip": ip, "country_long": fields[pos["countrylong"]].strip(), "country_short": fields[pos["countryshort"]].strip(), "config_b64": fields[pos["openvpn_configdata_base64"]].strip()})
    return rows

def parse_mirror_json(data):
    servers = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("servers"), list):
            servers.extend(item["servers"])
        elif isinstance(item, dict):
            servers.append(item)
    rows = []
    for s in servers:
        host = str(s.get("hostname") or s.get("host") or "").strip()
        ip = str(s.get("ip") or "").strip()
        if not host or not ip: continue
        rows.append({"host": host, "ip": ip, "country_long": str(s.get("countrylong") or s.get("country_long") or s.get("country") or "").strip(), "country_short": str(s.get("countryshort") or s.get("country_short") or s.get("country") or "").strip(), "config_b64": str(s.get("openvpn_configdata_base64") or s.get("config_b64") or "").strip()})
    return rows

# ---------------------------------------------------------------------------
# 筛选 SSTP 节点
# ---------------------------------------------------------------------------
_PROTO_TCP_RE = re.compile(r"^proto\s+(tcp|tcp4|tcp6)\b", re.M)
_REMOTE_RE = re.compile(r"^remote\s+\S+\s+(\d+)", re.M)

def to_sstp_nodes(rows):
    nodes = []
    for r in rows:
        cfg = ""
        if r["config_b64"]:
            try:
                cfg = base64.b64decode(r["config_b64"], validate=False).decode("utf-8", "replace")
            except Exception:
                cfg = ""
        if not _PROTO_TCP_RE.search(cfg): continue
        m = _REMOTE_RE.search(cfg)
        if not m: continue
        port = int(m.group(1))
        if not (1 <= port <= 65535): continue
        host = r["host"]
        if not host.endswith(".opengw.net"):
            host = f"{host}.opengw.net"
        nodes.append({"host": host, "port": port, "ip": r["ip"], "country": r["country_long"], "country_code": r["country_short"]})
    return nodes

def dedupe(nodes):
    seen = set()
    out = []
    for n in nodes:
        key = (n["host"].lower(), n["port"], "sstp")
        if key in seen: continue
        seen.add(key)
        out.append(n)
    return out

# ---------------------------------------------------------------------------
# 检测 Worker
# ---------------------------------------------------------------------------
def classify_network(host, exit_org, is_datacenter=None):
    if is_datacenter is True: return "datacenter"
    if is_datacenter is False: return "residential"
    org = (exit_org or "").upper()
    if org:
        if any(k in org for k in DATA_CENTER_ORG_KEYWORDS): return "datacenter"
        if any(k in org for k in RESIDENTIAL_ORG_KEYWORDS): return "residential"
    h = host.lower()
    if h.startswith("public-vpn"): return "datacenter"
    if re.match(r"^vpn\d{5,}", h) or re.match(r"^vpnv\d+", h): return "residential"
    return "unknown"

# 检测服务侧故障的特征 (Worker 上游限速/网关/内部错误) —— 这类失败要重试, 且不能算作「节点不通」
_WORKER_TROUBLE_RE = re.compile(
    r"(\b429\b|too many requests|rate.?limit|quota|lookup request failed|/api/lookup|"
    r"internal error|bad gateway|gateway time-?out|service unavailable|temporarily unavailable|"
    r"connection reset|econn|fetch failed|worker exceeded|exceeded cpu)",
    re.I,
)
# 节点侧故障的特征 ("SSTP server connection timed out" 等): 不重试, 属于节点真的不通
_NODE_SIDE_RE = re.compile(r"^\s*SSTP\b", re.I)
# 目标侧限流: 429 / too many requests / rate limit
# 与「Worker 上游额度耗尽」区分开: 后者是 Worker 直接返回 HTTP 429, 属于服务异常、要重试;
# 这里是 Worker 正常返回 200, 但 JSON 里的 error 说明「目标服务器把这次请求限流了」。
# 实测该错误 100% 可复现 (两轮重复测试 :443 全灭), 重试只会加剧限流。
# \b429\b: 加词边界, 避免把 "1429" / "4290" 这类数字子串误判成限流
_TARGET_RL_RE = re.compile(r"(\b429\b|too many requests|rate.?limit)", re.I)


def classify_failure(err):
    """把失败原因分成三类, 决定「要不要重试」:

      target_ratelimit  目标服务器限流 (429) —— 重试无效且会加剧限流, 默认不重试
      worker_error      检测服务本身异常 (5xx / 网关 / 超时) —— 要重试
      node_dead         节点真的不通 ("SSTP ..." 等) —— 不重试
    """
    text = str(err or "")
    if not text:
        return "node_dead"
    if _NODE_SIDE_RE.search(text):
        return "node_dead"
    if _TARGET_RL_RE.search(text):
        return "target_ratelimit"
    if _WORKER_TROUBLE_RE.search(text):
        return "worker_error"
    return "node_dead"


def is_worker_side_error(err):
    """判断失败原因属于「不是节点的锅」(检测服务异常 或 目标侧限流) 还是「节点真的不通」。

    保持旧语义: 只要不是节点侧故障就返回 True, 因此 status.worker_errors 的含义
    (非节点侧失败计数) 不变; 需要区分 429 的地方请用 classify_failure()。
    """
    return classify_failure(err) != "node_dead"


class RateLimiter:
    """请求最小间隔限速器: 避免并发瞬时打爆 Worker 上游的免费额度 (实测会出现大面积 429)。"""

    def __init__(self, interval):
        self.interval = max(0.0, float(interval))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if sleep_for > 0:
            time.sleep(sleep_for)


class PortCircuitBreaker:
    """按端口熔断目标侧限流。

    实测 :443 端口 100% 返回 429, 而非 443 端口大多可用。对这种「整个端口被限流」
    的情况, 一个个试下去只是浪费额度 —— 连续 N 次之后本轮直接跳过该端口,
    把检测额度让给其它端口。熔断只在本轮内有效, 下轮重新计数, 因此不会永久误杀
    (443 哪天恢复可用了会自动重新参与检测)。
    """

    def __init__(self, threshold):
        self.threshold = max(1, int(threshold))
        self._lock = threading.Lock()
        self._counts = {}
        self._open = set()
        self.skipped = 0

    def record(self, port):
        """记录一次目标侧限流; 返回 True 表示本次刚好触发熔断。"""
        with self._lock:
            n = self._counts.get(port, 0) + 1
            self._counts[port] = n
            if n >= self.threshold and port not in self._open:
                self._open.add(port)
                return True
        return False

    def is_open(self, port):
        with self._lock:
            return port in self._open

    def skip(self):
        """记录一次「因熔断而未发送」的请求。加锁: check_one 是多线程并发调用的。"""
        with self._lock:
            self.skipped += 1

    def tripped(self):
        with self._lock:
            return sorted(self._open)


class AdaptiveLimiter(RateLimiter):
    """在「最小请求间隔」基础上加一层全局自适应退避。

    两层保护:
      1. 间隔自适应: 滑窗内目标侧限流占比升高 -> 请求间隔翻倍 (上限 ADAPTIVE_MAX_INTERVAL);
         限流消失后逐步回落到基准值。
      2. 全局暂停: 占比高于阈值的 2 倍 -> 整体暂停 GLOBAL_RL_PAUSE 秒, 防止把自己
         的出口 IP 打进更长的封禁。
    """

    def __init__(self, interval, window=60.0, ratio=0.35, pause=10.0, max_interval=5.0):
        super().__init__(interval)
        self.base = max(0.0, float(interval))
        self.window = max(1.0, float(window))
        self.ratio = float(ratio)
        self.pause = max(0.0, float(pause))
        self.max_interval = max(self.base, float(max_interval))
        self._ev_lock = threading.Lock()
        self._events = []
        self._pause_until = 0.0
        self.pauses = 0
        self.interval = self.base

    def wait(self):
        super().wait()
        with self._ev_lock:
            until = self._pause_until
        if until > time.monotonic():
            time.sleep(until - time.monotonic())

    def note(self, kind):
        """kind: 'ok' | 'target_ratelimit' | 'other'"""
        with self._ev_lock:
            now = time.monotonic()
            cutoff = now - self.window
            self._events = [e for e in self._events if e[0] >= cutoff]
            self._events.append((now, kind))
            total = len(self._events)
            if total < 8:
                return
            rl = sum(1 for _, k in self._events if k == "target_ratelimit")
            r = rl / total
            if r > self.ratio * 2 and self.pause > 0:
                if self._pause_until <= now:
                    self.pauses += 1
                self._pause_until = max(self._pause_until, now + self.pause)
                self.interval = min(self.max_interval, max(self.base, self.interval * 2))
            elif r > self.ratio:
                self.interval = min(self.max_interval, max(self.base, self.interval * 2))
            elif r < self.ratio / 2:
                self.interval = max(self.base, self.interval / 2)

    def snapshot(self):
        with self._ev_lock:
            return {"interval_s": round(self.interval, 3), "pauses": self.pauses}


def check_one(node, session, limiter=None, breaker=None):
    url = WORKER_CHECK_URL + quote(f"{node['host']}:{node['port']}", safe="")
    out = dict(node)
    out["protocol"] = "sstp"
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["status"] = "failed"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    out["worker_error"] = False
    out["failure_kind"] = None
    out["skipped_by_breaker"] = False

    # 端口已熔断: 不发请求, 直接判「目标侧限流未测」—— 不发请求就不会加剧限流。
    # 注意: 这是「未测」而不是「测过且不通」, 绝不能据此把节点判成质量差。
    if breaker is not None and breaker.is_open(node["port"]):
        breaker.skip()
        out["skipped_by_breaker"] = True
        out["failure_kind"] = "target_ratelimit"
        # 必须置 worker_error=True: 它是「没测」, 不是「测过且节点不通」。
        # 否则会同时落进 node_failed (not worker_error) 与 rate_limited, 导致两类计数之和 > 失败总数。
        out["worker_error"] = True
        out["error"] = f"端口 {node['port']} 已熔断 (连续 {breaker.threshold} 次目标侧限流), 本轮跳过检测"
        out["attempts"] = 0
        return out

    attempts = CHECK_RETRIES + 1
    last_error = "check failed"
    worker_side = False
    kind = "node_dead"
    retriable = True                  # 「归为 worker_error」与「要不要重试」已解耦, 见下方 4xx 分支
    for attempt in range(1, attempts + 1):
        if limiter is not None:
            limiter.wait()
        retriable = True
        try:
            r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            kind = "worker_error"
            retriable = True
        else:
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                if r.status_code in (408, 425, 429) or 500 <= r.status_code < 600:
                    # 408/425/429/5xx: 服务端瞬时故障或限流, 重试有意义
                    kind, retriable = "worker_error", True
                elif 400 <= r.status_code < 500:
                    # 4xx 非 408/425/429 (典型 403/404): Worker 路由配错 / 鉴权缺失 /
                    # Cloudflare 前置拦截 —— 是「请求或配置」问题, 不是节点不通。
                    # 归 worker_error (非节点侧), 但重试同一个请求只会得到同样的 4xx, 故不重试。
                    kind, retriable = "worker_error", False
                else:
                    # 3xx (session 未跟随重定向) 或非标准状态码: 保守归服务异常
                    kind, retriable = "worker_error", True
            else:
                try:
                    j = r.json()
                except Exception as exc:
                    last_error = f"响应不是合法 JSON: {type(exc).__name__}"
                    kind = "worker_error"
                    retriable = True
                else:
                    if j.get("success"):
                        out["success"] = True
                        out["status"] = "success"
                        out["latency_ms"] = j.get("responseTime")
                        out["colo"] = j.get("colo")
                        out["error"] = None
                        exit_info = j.get("exit") or {}
                        if exit_info:
                            asn = exit_info.get("asn") or {}
                            org = asn.get("org") or asn.get("name") or ""
                            out["exit"] = {"ip": exit_info.get("ip"), "country": exit_info.get("country"), "country_code": exit_info.get("country_code"), "city": exit_info.get("city"), "continent": exit_info.get("continent"), "asn": asn.get("asn"), "org": org, "type": asn.get("type"), "is_datacenter": exit_info.get("is_datacenter")}
                            out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))
                        else:
                            out["residential"] = classify_network(out["host"], None, None)
                        out["attempts"] = attempt
                        if limiter is not None and hasattr(limiter, "note"):
                            limiter.note("ok")
                        return out
                    # Worker 正常返回但该节点不通: 三类归因 (目标限流 / 服务异常 / 节点不通)
                    last_error = str(j.get("error") or j.get("message") or "check failed")
                    kind = classify_failure(last_error)
                    # target_ratelimit 走自己的 TARGET_RL_RETRIES 逻辑; node_dead 不重试
                    retriable = (kind == "worker_error")
                    out["colo"] = j.get("colo")
                    out["latency_ms"] = j.get("responseTime")
        worker_side = kind != "node_dead"          # 非节点侧故障都计入 status.worker_errors
        out["failure_kind"] = kind
        out["attempts"] = attempt

        if kind == "target_ratelimit":
            # 目标服务器把我们限流了: 记入端口熔断 + 全局退避, 默认不重试。
            # 重试只会再发一次请求、把限流窗口拉得更长, 对产出零收益。
            if limiter is not None and hasattr(limiter, "note"):
                limiter.note("target_ratelimit")
            if breaker is not None and breaker.record(node["port"]):
                log("CLOUDFLARE WORKER", f"  端口 {node['port']} 连续 {breaker.threshold} 次目标侧限流 -> 本轮熔断该端口, 额度让给其它端口")
            if attempt <= TARGET_RL_RETRIES:
                delay = max(CHECK_BACKOFF, GLOBAL_RL_PAUSE) * attempt + random.uniform(0, 1)
                log("CLOUDFLARE WORKER", f"  目标侧限流 ({last_error[:60]}) -> {delay:.1f}s 后重试 {attempt}/{TARGET_RL_RETRIES}: {node['host']}:{node['port']}")
                time.sleep(delay)
                continue
            break

        if limiter is not None and hasattr(limiter, "note"):
            limiter.note("other")
        if kind == "worker_error" and retriable and attempt < attempts:
            delay = CHECK_BACKOFF * attempt + random.uniform(0, 1)
            log("CLOUDFLARE WORKER", f"  检测服务异常 ({last_error}) -> {delay:.1f}s 后重试 {attempt}/{CHECK_RETRIES}: {node['host']}:{node['port']}")
            time.sleep(delay)
            continue
        break

    out["success"] = False
    out["status"] = "failed"
    out["error"] = last_error
    out["worker_error"] = worker_side
    return out


def check_all(nodes, session, breaker=None):
    limiter = AdaptiveLimiter(CHECK_MIN_INTERVAL, window=GLOBAL_RL_WINDOW, ratio=GLOBAL_RL_RATIO,
                              pause=GLOBAL_RL_PAUSE, max_interval=ADAPTIVE_MAX_INTERVAL)
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(check_one, n, session, limiter, breaker) for n in nodes]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results, limiter


def select_check_targets(nodes, known_keys=None, budget=0):
    """在检测额度内挑选目标: 新节点优先, 其次住宅型 > 未知 > 机房型。

    known_keys: 上一版已发布节点的 (host, port) 集合 —— 不在集合里的就是「新节点」。
    budget: 单轮最多检测多少个, 0 = 不限。
    """
    ordered = list(nodes)
    if PREFER_NEW_NODES and known_keys:
        kind_rank = {"residential": 0, "unknown": 1, "datacenter": 2}
        # 新节点优先: 不在 known_keys 里的取 0 (排前面), 已知的取 1 (排后面)。
        # 注意别写反 —— 写成 `0 if in known_keys else 1` 会变成「老节点优先」,
        # 与 PREFER_NEW_NODES 的语义完全相反, 且 CHECK_BUDGET=0 时不会暴露。
        ordered.sort(key=lambda n: (
            0 if (n["host"].lower(), n["port"]) not in known_keys else 1,
            kind_rank.get(classify_network(n["host"], ""), 1),
            n["host"],
        ))
    if budget and len(ordered) > budget:
        ordered = ordered[:budget]
    return ordered

# ---------------------------------------------------------------------------
# 生成数据
# ---------------------------------------------------------------------------
def probe_endpoints(session):
    """体检用户自己的端点 (订阅/Worker 域名)。只读探测, 不改任何产物语义。

    分类:
      ok          2xx/3xx              -> 正常
      reachable   4xx (含 401/403/404) -> 站点活着 (鉴权/路径问题另说)
      edge_error  52x                  -> Cloudflare 边缘连不上后端 (本次 522 事故的形态)
      server_error 5xx                 -> 后端自己报错
      down        连接类异常/超时/DNS  -> 完全不可达
    """
    report = []
    for url in ENDPOINT_URLS:
        started = time.time()
        rec = {"url": url, "status": "unknown", "http": None, "ms": None, "error": None}
        try:
            resp = session.get(url, timeout=ENDPOINT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-endpoint-probe)"})
            rec["http"] = resp.status_code
            if resp.status_code in _EDGE_ERROR_CODES:
                rec["status"], rec["error"] = "edge_error", f"Cloudflare {resp.status_code}"
            elif resp.status_code >= 500:
                rec["status"], rec["error"] = "server_error", f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                rec["status"] = "reachable"
            else:
                rec["status"] = "ok"
        except Exception as exc:
            rec["status"] = "down"
            rec["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            rec["ms"] = int((time.time() - started) * 1000)
        report.append(rec)
        log("ENDPOINT", f"  {rec['status']:<12} {url}  HTTP={rec['http']} {rec['ms']}ms {rec['error'] or ''}")
    return report


def endpoint_summary(report):
    total = len(report or [])
    bad = [r for r in (report or []) if r["status"] not in ("ok", "reachable")]
    return {"total": total, "alive": total - len(bad), "abnormal": len(bad),
            "abnormal_urls": [r["url"] for r in bad]}


def build_outputs(results, raw_count, sstp_count, source):
    available = [r for r in results if r.get("success")]
    countries = {}
    for n in available:
        c = n["country"] or "未知"
        countries.setdefault(c, {"code": n["country_code"] or "?", "nodes": []})["nodes"].append(n)

    stats = {"raw_nodes": raw_count, "sstp_nodes": sstp_count, "checked": len(results), "success": len(available), "failed": len(results) - len(available), "countries": len(countries), "residential_est": sum(1 for n in available if n["residential"] == "residential"), "datacenter_est": sum(1 for n in available if n["residential"] == "datacenter")}
    by_country = {}
    for name, grp in countries.items():
        grp["count"] = len(grp["nodes"])
        grp["residential"] = sum(1 for n in grp["nodes"] if n["residential"] == "residential")
        grp["datacenter"] = sum(1 for n in grp["nodes"] if n["residential"] == "datacenter")
        grp["nodes"].sort(key=lambda n: (n.get("latency_ms") is None, n.get("latency_ms") or 0, n["host"]))
        by_country[name] = grp

    data = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "source": source, "worker": WORKER_CHECK_URL, "stats": stats, "countries": by_country, "available": available}
    return data

# edgetunnel 入口地址池
# 默认池 = 用户本地实测优选域名 (bestcf 三批结果合并去重, 电信网络实测 64-128ms, JP NRT / SG SIN 机房)。
# 这些域名「更新时不动」: 见下方 EDGE_HOST_LOCKED —— 在自动更新中锁定, 不参与 DNS/TCP 检测。
_DEFAULT_EDGE_HOSTS = (
    "bbs.alipansou.com:443,cf-cname.xingpingcn.top:443,www.mfyx.cn:443,www.bangbenjiaju.com:443,www.giannidelprete.it:443,"
    "c-power.com.cn:443,cf.090227.xyz:443,saas.sin.fan:443,www.sofi.com:443,cloudflare.idc.rocks:443,linear.app:443,uspto.gov:443,"
    "www.dg22.top:443,m.iyf.tv:443,securecircle.com:443,dongbanghong.com:443,funko.com:443,dnew.cc:443,p.etime.vip:443,"
    "www.vastnovel.com:443,vps.cheng2001.top:443,staticdelivery.nexusmods.com:443,www.dentoncounty.gov:443,eii.at:443,"
    "www.broadcom.com:443,53.fs1.hubspotusercontent-na1.net:443,www.xflash.vip:443,ex.warspite.dpdns.org:443,cdn.7zz.cn:443,"
    "wppaunz.com:443,saas.072159.xyz:443,www.sloomb.com:443,cf.3666888.xyz:443,serviceshub.samsclub.com:443,api.gzcrtw.com:443,"
    "hzytjy.cn:443,spring.io:443,cf.xreak.top:443,www.vmware.com:443,cf.877774.xyz:443,store.ubi.com:443,www.swowd.com:443,"
    "101yaoye.com:443,cdns.doon.eu.org:443,www.5199dy.com:443,cf.468123.xyz:443,openai.com:443,coreweave.com:443,cdn.cnno.de:443,"
    "kickstarter.com:443,mfa.gov.ua:443,www.shopify.com:443,www.deepl.com:443,www.dbs.com.sg:443,cdn.ddeed.de:443,"
    "academy.7shifts.com:443,www.akasantech.com:443,www.wuduanyun.com:443,fn.130519.xyz:443,guide.for.edu.sg:443,"
    "mail.notion.com:443,www.xiaoshuofen.com:443,www.gov.il:443,thebeat.gehealthcare.com:443,jobsdb.com:443,www.leics.police.uk:443,"
    "prizepicks.com:443,constitution.congress.gov:443,tt.78607323.xyz:443,www.carousell.sg:443,ikankeji.com:443,www.wto.org:443,"
    "cdn.204910.best:443,cf.qq.ms:443,egov.uscis.gov:443,www.zendesk.com:443,img.css.sd:443,www.blibli.com:443,"
    "cf.nyanya.moe:443,auto.dolby.dpdns.org:443,cdn.jwcmdr.top:443,224322.xyz:443,www.jp.pima.gov:443,cf.92555.xyz:443,"
    "cf.254301.xyz:443,01-cctv.com:443,stores.staples.com:443,cdn.555586.xyz:443,www.5h.com:443,lt.1930812.xyz:443,www.bis.gov:443,"
    "cf.yj250.bond:443,test.509666.xyz:443,www.sage.com:443,01-qq.com:443,www.crazygames.fr:443,so.360832.xyz:443,"
    "www.mastervolt.com:443,cf.itv888.cn:443,neko.cloudd.eu.org:443,versantstore.pearson.com:443,cdn.667891.xyz:443,"
    "neko.cloudflaree.eu.org:443,aqua-aria.company:443,cdn.ctn32.us.kg:443,www.udacity.com:443,cfplus.255520.xyz:443,cdn.2x.nz:443,"
    "ahrefs.com:443,ali.nonull.pp.ua:443,www.mc.js.cool:443,cf2.996616.xyz:443,d.lma.de5.net:443,markmonitor.com:443,"
    "img.856518.xyz:443,cfip-ct.stoeaves.us.ci:443,cf.777791.xyz:443,login.rockwellautomation.com:443,idc.urkeji.com:443,"
    "jellyfin.roddy.eu.cc:443,www.galgamex.net:443,kniu.cc:443,baota.us.kg:443,op.chinwa.eu.cc:443",
)
EDGE_HOSTS = _env_csv("EDGE_HOSTS", _DEFAULT_EDGE_HOSTS)

# ---------------------------------------------------------------------------
# 内置自动优选 API (告别手动优选)
# ---------------------------------------------------------------------------
# 背景: 静态 EDGE_HOSTS 是「一次性测速、永远不变」的域名池, 优选 IP 会随时间劣化,
# 需要人肉重测才能换 —— 这正是 edgetunnel 面板「自定义优选IP」长期要手工维护的痛点。
# 这里每轮运行都去优选 API 现取一批当轮最优 IP, 放在池首优先使用, 静态域名降为兜底。
#
# API 响应格式: text/plain, 每行 "IP#名字" 或裸 "IP", **不带端口** (port 只是查询参数),
# 所以必须由本模块补 ":443" —— 直接把返回行塞进 nodes.txt 会过不了 validate_nodes_text。
#
# 支持多个接口 (英文逗号分隔), 逐个尝试; 任何一个失败都不影响其余, 全部失败则纯用静态池。
# 想关掉这个功能: OPTIMAL_API=off (或 0 / none / false)。
_DEFAULT_OPTIMAL_API = (
    "https://cf.090227.xyz/ct?ips=6&port=443,"     # 电信
    "https://cf.090227.xyz/cu?port=443,"           # 联通
    "https://cf.090227.xyz/cmcc?ips=8&port=443",   # 移动
)
_opt_raw = os.environ.get("OPTIMAL_API")
OPTIMAL_API = (
    [] if (_opt_raw is not None and _opt_raw.strip().lower() in ("0", "off", "none", "false", "disable"))
    else _env_csv("OPTIMAL_API", _DEFAULT_OPTIMAL_API)
)
OPTIMAL_TIMEOUT = float(os.environ.get("OPTIMAL_TIMEOUT", "20"))   # 单个优选接口超时(秒)
OPTIMAL_MAX = max(1, int(os.environ.get("OPTIMAL_MAX", "24")))     # 单轮最多采纳多少个优选 IP
OPTIMAL_HEADERS = {"User-Agent": "gate-vpngate/1.0 (+auto-optimal)"}

NODES_URL = os.environ.get("NODES_URL", "https://200111226011qw-debug.github.io/gate/nodes.txt")
# 上一版产物 (用于降级): data.json 默认与 nodes.txt 同目录
DATA_URL = os.environ.get("DATA_URL") or NODES_URL.rsplit("/", 1)[0] + "/data.json"

# ---------------------------------------------------------------------------
# 入口域名 DNS 污染检测
# ---------------------------------------------------------------------------
# 背景: 入口域名若被 DNS 污染 (解析到无关 IP), 客户端连接会超时,
#表现为 v2rayN / Clash 里延迟全是 -1。这里在生成节点前主动剔除。
#
# 注意: 检测结果反映的是「运行本脚本的这台机器」的网络环境。
# 若在 GitHub Actions (境外 runner) 上运行, 检测到的是境外视角,
# 无法反映墙内污染 —— 此时信号1 通常不会命中, 不会误剔除。
CN_DNS_LIST = _env_csv("CN_DNS", "223.5.5.5,119.29.29.29,114.114.114.114")
EDGE_DNS_CHECK = os.environ.get("EDGE_DNS_CHECK", "1") not in ("0", "false", "False")

# 黑名单: 已知无法作为 Cloudflare 优选入口的域名, 直接剔除, 不参与检测。
#   www.bilibili.com —— 实测解析到 119.84.x / 183.131.x 等国内 IP, 不走 Cloudflare 网段。
#   TCP 虽通, 但 TLS 无法路由到 Worker, 作为入口无效。
EDGE_HOST_BLACKLIST = set(_env_csv("EDGE_HOST_BLACKLIST", "www.bilibili.com"))
# 白名单: 经检测确认「不走 CF 网段但确实可用」的入口, 跳过网段校验只测连通性。
#   默认为空 —— 当前 20 个入口中仅 bilibili 异常, 已归入黑名单, 无需豁免。
#   若日后新增 CNAME 类入口(解析不在 CF 段但 TLS 可达), 把域名填进来即可。
EDGE_HOST_WHITELIST = set(_env_csv("EDGE_HOST_WHITELIST", ""))
# 锁定名单: 默认 = 默认入口池 (用户本地实测优选域名), 更新时「不动」——
# 这些域名跳过 DNS 污染检测与 TCP 检测, 在自动更新中永远保留。
# 若某个域名希望参与检测, 用 EDGE_HOST_LOCKED 环境变量传入不含它的列表即可。
# 注意: 必须按「主机名」存 (去掉 :端口), 与 filter_edge_hosts 里 host in EDGE_HOST_LOCKED 的比较口径一致。
#       否则带端口的条目永远匹配不上「去掉端口后的 host」, 锁定形同虚设 (2026-10-07 修)。
EDGE_HOST_LOCKED = {entry.split(":")[0].strip() for entry in _env_csv("EDGE_HOST_LOCKED", _DEFAULT_EDGE_HOSTS)}
EDGE_DNS_TIMEOUT = float(os.environ.get("EDGE_DNS_TIMEOUT", "5"))
EDGE_TCP_TIMEOUT = float(os.environ.get("EDGE_TCP_TIMEOUT", "6"))

# Cloudflare IPv4 网段
CF_V4 = ((104, 16, 104, 31), (172, 64, 172, 127), (162, 158, 162, 159), (198, 41, 198, 41))


def _split_host_port(entry):
    """把 'example.com:443' 拆成 ('example.com', '443'), 无端口则端口为 None"""
    if ":" in entry and not entry.rstrip().endswith("]"):
        host, _, port = entry.rpartition(":")
        if host and port.isdigit():
            return host, port
    return entry, None


def _is_cloudflare(ip):
    if not ip:
        return False
    if ":" in ip:  # Cloudflare IPv6 段 2a06:4700::/32
        return ip.lower().startswith("2a06:4700")
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    for sa, sb, ea, eb in CF_V4:
        if a == sa and sb <= b <= eb:
            return True
    return False


def _dns_build_query(domain):
    qid = random.randint(1, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b""
    for label in domain.split("."):
        qname += bytes([len(label)]) + label.encode("ascii")
    return qid, header + qname + b"\x00" + struct.pack(">HH", 1, 1)


def _dns_skip_name(buf, offset):
    while True:
        if offset >= len(buf):
            raise ValueError("dns name out of range")
        length = buf[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def _dns_query_a(domain, server, timeout=EDGE_DNS_TIMEOUT):
    """UDP 查询 A 记录。失败抛异常, 无记录返回 []。"""
    qid, packet = _dns_build_query(domain)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    if len(data) < 12:
        raise ValueError("short dns response")
    rid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    if rid != qid:
        raise ValueError("dns id mismatch")
    if flags & 0x0F != 0:
        raise ValueError(f"dns rcode={flags & 0x0F}")
    if an == 0:
        return []
    offset = 12
    for _ in range(qd):
        offset = _dns_skip_name(data, offset) + 4
    ips = []
    for _ in range(an):
        offset = _dns_skip_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlen]
        offset += rdlen
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(rdata))
    return ips


def _tcp_ok(host, port=443, timeout=EDGE_TCP_TIMEOUT):
    try:
        socket.create_connection((host, int(port or 443)), timeout=timeout).close()
        return True
    except OSError:
        return False


# 优选 API 返回的入口既可能是裸 IP 也可能是域名, 两种都要认
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")


def _is_ip_literal(host):
    """入口是不是裸 IP (优选 API 的产物)。裸 IP 没有 DNS 记录可查, 检测逻辑必须区别对待。"""
    return bool(_IPV4_RE.match(host))


def _normalize_optimal_line(line):
    """把优选 API 的一行解析成 'host:port' 条目; 无法识别返回 None。

    实测该 API 返回 "104.18.33.230#CF 电信优选" —— 裸 IP + '#名字', 且**不带端口**
    (port 只是查询参数), 所以这里默认补 443; 若行里已带 ':端口' 则沿用。
    同时容忍 'https://1.2.3.4/path?x=1' 这种整行 URL 形态。

    裸 IPv6 (含多个 ':' 且无方括号) 会被 _split_host_port 误拆成 host/port, 这里直接判为
    不采用 —— 上述接口目前只返 IPv4, 真要支持得先给 _split_host_port 补方括号解析。
    """
    line = line.split("#", 1)[0].strip()
    if "://" in line:
        line = line.split("://", 1)[1]
    line = line.split("/")[0].split("?")[0].strip()
    if not line or (":" in line and line.count(":") > 1):
        return None
    host, port = _split_host_port(line)
    if not _IPV4_RE.match(host) and not _HOSTNAME_RE.match(host):
        return None
    return f"{host}:{port or 443}"


def _dedupe_entries(entries):
    """按原顺序去重入口条目 (保留首次出现), 空条目丢弃。"""
    seen, out = set(), []
    for entry in entries:
        entry = entry.strip()
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def fetch_optimal_hosts(session, limit=None):
    """按 OPTIMAL_API 逐个拉取当轮优选 IP, 返回 'host:port' 列表 (顺序保留, 已去重)。

    任何一个接口失败都只告警不中断; 全部失败返回 [] —— 调用方据此纯用静态 EDGE_HOSTS,
    绝不因为优选服务抽风就产出空订阅 (这条与 DNS 污染检测的兜底策略一致)。
    """
    if not OPTIMAL_API:
        log("OPTIMAL", "已用 OPTIMAL_API=off 关闭自动优选, 本轮只用静态入口池")
        return []

    cap = OPTIMAL_MAX if limit is None else max(1, limit)
    out, seen, failed = [], set(), 0
    for url in OPTIMAL_API:
        if len(out) >= cap:
            break
        try:
            resp = session.get(url, timeout=OPTIMAL_TIMEOUT, headers=OPTIMAL_HEADERS)
            resp.raise_for_status()
        except Exception as exc:
            failed += 1
            warn(f"OPTIMAL: 优选接口不可用 ({type(exc).__name__}: {exc}) -> {url}")
            continue
        got = 0
        for raw_line in (getattr(resp, "text", "") or "").splitlines():
            entry = _normalize_optimal_line(raw_line)
            if not entry or entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
            got += 1
            if len(out) >= cap:
                break
        log("OPTIMAL", f"{url} -> 采纳 {got} 个")

    if not out:
        warn("OPTIMAL: 所有优选接口都没返回可用 IP, 本轮沿用静态入口池 (不影响 nodes.txt 产出)")
    else:
        log("OPTIMAL", f"合计采纳 {len(out)} / 上限 {cap} 个优选 IP (接口失败 {failed} 个)")
    return out[:cap]


def filter_edge_hosts(entries):
    """剔除黑名单域名, 以及被 DNS 污染或 TCP 不通的入口域名。全部不可用时保留原列表。"""
    if not entries:
        return entries

    # 第 1 步: 黑名单直接剔除 (无需检测)
    if EDGE_HOST_BLACKLIST:
        kept, dropped = [], []
        for entry in entries:
            host, _ = _split_host_port(entry)
            (dropped if host in EDGE_HOST_BLACKLIST else kept).append(entry)
        if dropped:
            log("EDGE DNS", f"黑名单剔除: {', '.join(_split_host_port(e)[0] for e in dropped)}")
        entries = kept
        if not entries:
            log("EDGE DNS", "警告: 黑名单后列表为空, 跳过 DNS 检测")
            return []

    if not EDGE_DNS_CHECK or not entries:
        return entries

    log("EDGE DNS", f"检测入口域名污染: {len(entries)} 个 (DNS {', '.join(CN_DNS_LIST)})")

    checked = []
    for entry in entries:
        host, port = _split_host_port(entry)
        rec = {"host": host, "status": "ok", "ips": [], "reasons": []}
        # 第 0 步: 锁定域名直接保留 (更新时不动, 不参与 DNS/TCP 检测)
        if host in EDGE_HOST_LOCKED:
            log("EDGE DNS", f"  [LOCK] {host:<24} 锁定, 更新时不动")
            checked.append(rec)
            continue
        # 第 0.5 步: 优选 API 给的是裸 IP 入口, 没有 DNS 记录可解析, 只测 TCP 连通性。
        # 必须在这里分叉: 若让它走下面的 DNS 分支, _dns_query_a("104.18.33.230") 查不到
        # A 记录, 会被 100% 误判成「国内 DNS 无 A 记录」而全量剔除, 自动优选就白做了。
        if _is_ip_literal(host):
            rec["ips"] = [host]
            if not _tcp_ok(host, port):
                rec["status"] = "bad"
                rec["reasons"].append("TCP 连接失败")
            checked.append(rec)
            detail = "优选裸IP, TCP 可达" if rec["status"] == "ok" else "; ".join(rec["reasons"])
            log("EDGE DNS", f"  [{rec['status'].upper():<5}] {host:<24} {detail}")
            continue
        try:
            for server in CN_DNS_LIST:
                try:
                    ips = _dns_query_a(host, server)
                    if ips:
                        rec["ips"] = ips
                        break
                except Exception:
                    continue
            if not rec["ips"]:
                rec["status"] = "bad"
                rec["reasons"].append("国内 DNS 无 A 记录")
            else:
                # 信号1: 解析结果是否落在 Cloudflare 网段 (白名单跳过)
                if host not in EDGE_HOST_WHITELIST and not any(_is_cloudflare(ip) for ip in rec["ips"]):
                    rec["status"] = "bad"
                    rec["reasons"].append(f"解析非 Cloudflare 网段 (疑似污染): {','.join(rec['ips'][:3])}")
                # 信号2: TCP 可达性
                if rec["status"] == "ok" and not _tcp_ok(host, port):
                    rec["status"] = "bad"
                    rec["reasons"].append("TCP 连接失败")
        except Exception as exc:
            rec["status"] = "unknown"
            rec["reasons"].append(f"检测异常: {type(exc).__name__}")
        checked.append(rec)
        if rec["status"] == "ok":
            log("EDGE DNS", f"  [OK]   {host:<24} {','.join(rec['ips'][:2])}")
        else:
            log("EDGE DNS", f"  [{rec['status'].upper():<5}] {host:<24} {'; '.join(rec['reasons'])}")

    healthy = [r["host"] for r in checked if r["status"] == "ok"]
    bad = [r for r in checked if r["status"] != "ok"]

    # 重建原格式 entry (保留端口)
    healthy_entries = []
    for entry in entries:
        host, _ = _split_host_port(entry)
        if host in healthy:
            healthy_entries.append(entry)

    log("EDGE DNS", f"健康 {len(healthy_entries)} / 异常 {len(bad)}")
    if bad:
        log("EDGE DNS", f"剔除: {', '.join(r['host'] for r in bad)}")

    # 全部异常时保留原列表, 绝不产出空订阅
    if not healthy_entries:
        log("EDGE DNS", "警告: 无域名通过检测, 沿用完整列表 (避免产出空订阅)")
        return entries

    return healthy_entries


def build_nodes_text(data, edge_hosts=None, optimal_hosts=None):
    """生成纯节点行版本 (无注释): 每行 = 入口地址#名字$sstp://...

    optimal_hosts: 本轮优选 API 现取的裸 IP 入口, 置于入口池首优先使用; 传 None 则只用静态池。
    """
    countries = data["countries"]
    if edge_hosts:
        # 调用方显式给了入口列表 (测试/预览场景): 直接用, 不做黑名单与 DNS 检测
        edge = list(edge_hosts)
    else:
        _entry = os.environ.get("HOSTS_ENTRY", "").strip()
        base = [e.strip() for e in _entry.split(",") if e.strip()] or EDGE_HOSTS
        # DNS 污染过滤 (仅在未显式指定 HOSTS_ENTRY 时生效, 保证人工指定优先)
        if not _entry:
            # 自动优选 IP 必须排池首: 下面按 edge[idx % len(edge)] 顺序轮换, 而静态池有上百条、
            # 实际可用节点只有几十条, 轮得到的基本只有池首那一段 —— 不排首位就等于没启用。
            if optimal_hosts:
                log("EDGE", f"自动优选 {len(optimal_hosts)} 个 IP 置于入口池首, 静态域名降为兜底")
                base = _dedupe_entries(list(optimal_hosts) + base)
            base = filter_edge_hosts(base)
        # 兜底: 极端情况下 (如黑名单清空列表) 回退到原始 EDGE_HOSTS, 避免除零/空轮换
        if not base:
            log("EDGE DNS", "警告: 筛选结果为空, 回退到完整入口列表")
            base = list(EDGE_HOSTS)
        edge = base
    if not edge:
        log("EDGE DNS", "警告: 入口池为空, 无法生成任何节点行")
        return ""
    lines = []
    idx = 0
    ordered = sorted(countries.items(), key=lambda kv: (-int(kv[1].get("count") or 0), str(kv[1].get("code") or kv[0])))
    for cname, grp in ordered:
        code = str(grp.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else cname)
        nodes = sorted(grp["nodes"], key=lambda n: (0 if n.get("residential") == "residential" else 1, n.get("latency_ms") is None, n.get("latency_ms") or 0, n.get("host") or ""))
        res_nodes = [n for n in nodes if n.get("residential") == "residential"]
        dc_nodes = [n for n in nodes if n.get("residential") != "residential"]
        for i, n in enumerate(res_nodes, 1):
            entry = edge[idx % len(edge)]
            idx += 1
            lines.append(f"{entry}#{zh}-住宅-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
        for i, n in enumerate(dc_nodes, 1):
            entry = edge[idx % len(edge)]
            idx += 1
            lines.append(f"{entry}#{zh}-机房-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
    return "\n".join(lines) + "\n"

def render_html():
    """页面模板 (web/index.html, 缺失时用内置兜底页)。"""
    if os.path.exists(TEMPLATE_HTML):
        with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
            return f.read()
    return ("<html><head><meta charset='utf-8'><title>VPN Gate SSTP 节点</title></head>"
            "<body><h1>VPN Gate SSTP 节点</h1><pre id='out'></pre></body>"
            "<script>fetch('data.json').then(r=>r.json()).then(d=>out.textContent=JSON.stringify(d.stats)).catch(e=>out.textContent='加载失败:'+e)</script></html>")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def write_outputs(data, status, nodes_text):
    """正常路径: 写入本轮新生成的产物 (nodes_text 由调用方生成并校验非空)。"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    data["status"] = status
    data_path = write_json(os.path.join(PUBLIC_DIR, "data.json"), data)
    html_path = os.path.join(PUBLIC_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html())
    nodes_path = os.path.join(PUBLIC_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8") as f:
        f.write(nodes_text)
    status_path = write_json(os.path.join(PUBLIC_DIR, STATUS_NAME), status)
    return data_path, html_path, nodes_path, status_path


# 节点行格式: 入口[#名字]$sstp://vpn:vpn@主机:端口   (入口与主机都必须是域名)
NODE_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?#[^$]+\$sstp://vpn:vpn@[A-Za-z0-9][A-Za-z0-9._-]*:\d+$")


def validate_nodes_text(text):
    """自检产物格式: 返回问题列表 (空列表 = 每行都合法)。

    加这道自检是因为 2026-10-07 的事故: 入口池被按「字符」拆开, 生成的每行入口变成
    单个字母, 而当时的测试只数了行数, 没校验格式 —— 于是「测试全绿、产物全废」。
    """
    problems = []
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ["nodes.txt 为空"]
    for i, ln in enumerate(lines, 1):
        if not NODE_LINE_RE.match(ln):
            problems.append(f"第 {i} 行格式非法: {ln[:80]}")
            continue
        entry = ln.split("#", 1)[0].split(":")[0]
        if "." not in entry:
            problems.append(f"第 {i} 行入口不是域名: {entry!r}")
        host = ln.rsplit("@", 1)[-1].split(":")[0]
        if "." not in host:
            problems.append(f"第 {i} 行节点主机不是域名: {host!r}")
    return problems


# ---------------------------------------------------------------------------
# 降级: 沿用上一次已发布的产物 (public/ 不入库, 唯一可靠的上一版就在 Pages 上)
# ---------------------------------------------------------------------------
def count_nodes(text):
    return sum(1 for ln in (text or "").splitlines() if ln.strip())


def load_last_good(session):
    """下载上一次已发布的 nodes.txt / data.json; nodes.txt 拿不到就返回 None。"""
    if not KEEP_LAST_GOOD:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (gate-checker)", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    nodes_text, data = None, None
    try:
        r = session.get(f"{NODES_URL}{'&' if '?' in NODES_URL else '?'}t={int(time.time())}", timeout=HTTP_TIMEOUT, headers=headers)
        if r.status_code == 200 and r.text.strip():
            nodes_text = r.text
        else:
            log("FALLBACK", f"上一版 nodes.txt 不可用 (HTTP {r.status_code})")
    except Exception as exc:
        log("FALLBACK", f"读取上一版 nodes.txt 失败: {type(exc).__name__}: {exc}")
    try:
        r = session.get(f"{DATA_URL}{'&' if '?' in DATA_URL else '?'}t={int(time.time())}", timeout=HTTP_TIMEOUT, headers=headers)
        if r.status_code == 200:
            data = r.json()
    except Exception as exc:
        log("FALLBACK", f"读取上一版 data.json 失败: {type(exc).__name__}: {exc}")
    if not nodes_text:
        return None
    return {"nodes_text": nodes_text, "data": data, "count": count_nodes(nodes_text)}


def last_good_keys(last_good):
    """从上一版 nodes.txt 解析出 (host, port) 集合, 用于识别「本轮新出现的节点」。"""
    keys = set()
    if not last_good:
        return keys
    for ln in (last_good.get("nodes_text") or "").splitlines():
        ln = ln.strip()
        if not ln or not NODE_LINE_RE.match(ln):
            continue
        hp = ln.rsplit("@", 1)[-1]
        host, _, port = hp.rpartition(":")
        if host and port.isdigit():
            keys.add((host.lower(), int(port)))
    return keys


def carry_over_nodes(results, last_good):
    """合并发布: 把上一版仍可用、本轮未验证失败的节点并入本轮结果。

    只剔除本轮「确认节点不通」(node_dead) 的; 被限流未测 (target_ratelimit) 的
    不算失败, 上一版若还在就继续保留 —— 限流不等于节点坏了。
    """
    if not last_good:
        return []
    data = last_good.get("data")
    if not isinstance(data, dict):
        return []
    prev = data.get("available") or []
    if not isinstance(prev, list):
        return []

    have = {(str(r.get("host") or "").lower(), r.get("port")) for r in results}
    dead = {(str(r.get("host") or "").lower(), r.get("port"))
            for r in results if not r.get("success") and r.get("failure_kind") == "node_dead"}

    carried = []
    for item in prev:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        port = item.get("port")
        if not host or not isinstance(port, int):
            continue
        key = (host.lower(), port)
        if key in have or key in dead:
            continue
        node = dict(item)
        node.update({"host": host, "port": port, "success": True, "status": "success",
                     "protocol": "sstp", "link": f"sstp://vpn:vpn@{host}:{port}",
                     "worker_error": False, "failure_kind": None, "carried_over": True})
        node.setdefault("country", item.get("country") or "")
        node.setdefault("country_code", item.get("country_code") or "")
        node.setdefault("residential", item.get("residential") or "unknown")
        carried.append(node)
    return carried


def publish_last_good(last_good, status):
    """把上一版产物写进 public/, 保证 edgetunnel 订阅不中断。"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    text = last_good["nodes_text"]
    if not text.endswith("\n"):
        text += "\n"
    nodes_path = os.path.join(PUBLIC_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8") as f:
        f.write(text)

    data = last_good.get("data")
    if not isinstance(data, dict):
        data = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "source": "last-published",
            "worker": WORKER_CHECK_URL,
            "stats": {},
            "countries": {},
            "available": [],
        }
    data["status"] = status
    data_path = write_json(os.path.join(PUBLIC_DIR, "data.json"), data)

    html_path = os.path.join(PUBLIC_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html())

    status_path = write_json(os.path.join(PUBLIC_DIR, STATUS_NAME), status)
    return data_path, html_path, nodes_path, status_path


def top_error(rows, limit=140):
    """取出现次数最多的失败原因, 用于诊断摘要。"""
    counts = {}
    for r in rows:
        err = str(r.get("error") or "unknown")
        counts[err] = counts.get(err, 0) + 1
    if not counts:
        return "-"
    err, n = max(counts.items(), key=lambda kv: kv[1])
    return f"{err[:limit]}" + (f" (x{n})" if n > 1 else "")


def build_status(outcome, reason, started, **extra):
    status = {
        "outcome": outcome,
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration_s": round(time.time() - started, 1),
        "source": extra.pop("source", None),
        "worker": WORKER_CHECK_URL or None,
        "worker_configured": WORKER_URL_OK,
        "concurrency": CONCURRENCY,
        "check_retries": CHECK_RETRIES,
        "entry_hosts": len(EDGE_HOSTS),
        "entry_hosts_locked": len(EDGE_HOST_LOCKED),
        "endpoints": list(_endpoint_report),
        "endpoints_summary": endpoint_summary(_endpoint_report),
        "run_id": os.environ.get("GITHUB_RUN_ID") or None,
    }
    status.update({k: v for k, v in extra.items() if v is not None})
    return status


def log_diagnostics(status):
    """无论成功或降级都打印: 下次出问题照抄这段即可定位。"""
    log("诊断摘要", "")
    print(f"  结论        : {status['outcome']} ({status['reason']})")
    print(f"  python      : {sys.version.split()[0]} @ {sys.executable}")
    print(f"  requests    : {getattr(requests, '__version__', '?')}")
    print(f"  Worker      : {WORKER_CHECK_URL or '(未配置)'}  合法={WORKER_URL_OK}")
    print(f"  并发/重试   : {CONCURRENCY} / {CHECK_RETRIES} (最小间隔 {CHECK_MIN_INTERVAL}s)")
    print(f"  入口池      : {len(EDGE_HOSTS)} 个 (锁定 {len(EDGE_HOST_LOCKED)} 个)")
    es = status.get("endpoints_summary") or {}
    if es.get("total"):
        print(f"  端点体检    : {es['alive']}/{es['total']} 正常" + (f" — 异常: {', '.join(es['abnormal_urls'])}" if es.get("abnormal") else ""))
    stats = status.get("stats") or {}
    if stats:
        print("  本轮统计    : " + ", ".join(f"{k}={v}" for k, v in stats.items() if k != "failure_breakdown"))
    bd = status.get("failure_breakdown") or {}
    if bd:
        print(f"  失败归因    : 目标侧限流未测 {bd.get('target_ratelimit', 0)} / 检测服务异常 {bd.get('worker_error', 0)} / 节点不通 {bd.get('node_dead', 0)}")
    if status.get("rate_limited_ports"):
        print(f"  熔断端口    : {', '.join(str(p) for p in status['rate_limited_ports'])} (本轮未测, 不是确认不通)")
    if status.get("new_nodes"):
        print(f"  新节点      : 本轮新增可用 {status['new_nodes']} 个 (上一版没有的)")
    if status.get("worker_errors"):
        # 注意: status.worker_errors 是「非节点侧失败」的旧口径 (含目标侧限流),
        # 这里优先展示细分归因, 避免把「限流未测」误读成「检测服务坏了」。
        print(f"  检测服务异常: {bd.get('worker_error', status['worker_errors'])} 次")
    print(f"  耗时        : {status['duration_s']}s")
    print(f"  订阅地址    : {NODES_URL}")


def finish_degraded(session, reason, started, last_good=None, **stats):
    """降级路径: 尽量沿用上一版产物; 实在没有可发布的内容才判定失败 (不发布空订阅)。"""
    warn(reason)
    if last_good is None:
        last_good = load_last_good(session)
    if last_good:
        status = build_status("stale", reason, started, last_good_nodes=last_good["count"],
                              last_good_generated_at=(last_good.get("data") or {}).get("generated_at"), **stats)
        paths = publish_last_good(last_good, status)
        log("FALLBACK", f"已沿用上一版产物 ({last_good['count']} 个节点, 生成于 {status.get('last_good_generated_at') or '未知'}), 订阅不会中断")
        for p in paths:
            log("WEBSITE", f"发布 {os.path.relpath(p, REPO_DIR)}")
    else:
        status = build_status("failed", reason, started, **stats)
        log("FATAL", "既没有新数据, 也拿不到上一版产物 — 不发布空订阅, 本次运行判定失败 (线上站点保持原样)")
        print(f"::error::{reason}", flush=True)
        # 修复 (2026-10-21): 以前这条分支只 build 了 status 却没落盘, public/status.json 直接缺失,
        # Actions 的「运行结果汇总」会打印「脚本在生成产物前就退出了」, 把人往错误的方向带。
        # 失败也必须留下结论, 否则没法区分「脚本没跑」和「跑了但没得发」。
        try:
            os.makedirs(PUBLIC_DIR, exist_ok=True)
            write_json(os.path.join(PUBLIC_DIR, STATUS_NAME), status)
            log("FALLBACK", f"已写入 {STATUS_NAME} (outcome=failed), 便于排查")
        except Exception as exc:
            log("FALLBACK", f"写入 {STATUS_NAME} 失败: {type(exc).__name__}: {exc}")
    log_diagnostics(status)
    if status["outcome"] == "stale" and not STRICT:
        return 0
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    global _endpoint_report
    session = requests.Session()
    started = time.time()

    log("ENV", f"python {sys.version.split()[0]} | requests {getattr(requests, '__version__', '?')} | 并发 {CONCURRENCY} | 检测重试 {CHECK_RETRIES} | Worker {'已配置' if WORKER_URL_OK else '未配置/非法'}")

    if ENDPOINT_URLS:
        log("ENDPOINT", f"端点体检: {len(ENDPOINT_URLS)} 个 (仅体检, 不影响 nodes.txt)")
        _endpoint_report = probe_endpoints(session)
        summary = endpoint_summary(_endpoint_report)
        if summary["abnormal"]:
            warn(f"端点体检异常 {summary['abnormal']}/{summary['total']}: {', '.join(summary['abnormal_urls'])} "
                 f"(详情见 status.json / endpoints.json)")
        os.makedirs(PUBLIC_DIR, exist_ok=True)
        write_json(os.path.join(PUBLIC_DIR, "endpoints.json"), _endpoint_report)

    if not WORKER_URL_OK:
        return finish_degraded(
            session,
            "CHECK_WORKER 未配置或非法: 请在仓库 Settings → Secrets and variables → Actions 里把 DOMAIN 设为 "
            "https://你的Worker域名/check?sstp=vpn:vpn@ (注意结尾的 check?sstp=vpn:vpn@ 不能少)",
            started,
        )

    if not EDGE_HOSTS and not os.environ.get("HOSTS_ENTRY", "").strip():
        return finish_degraded(
            session,
            "入口池为空 (EDGE_HOSTS 与默认池都没有条目), 生成的订阅会是空的 — 不发布",
            started,
        )

    rows, source = fetch_vpngate(session)
    if not rows:
        return finish_degraded(session, "所有数据源都不可用 (官方 API 与 GitHub 镜像均获取失败)", started)

    raw_count = len(rows)
    sstp_nodes = to_sstp_nodes(rows)
    sstp_count = len(sstp_nodes)
    if sstp_count == 0:
        return finish_degraded(
            session,
            f"从 {raw_count} 个原始节点中没有解析出任何 SSTP(TCP) 节点 — 数据格式可能已变化, 需要人工适配",
            started,
            raw_nodes=raw_count,
        )

    # uniq_all = 去重后、未经任何截断的源规模; uniq = 可能被 MAX_CHECK_NODES 截断后的检测池。
    # 必须分开保留: candidates_in_source 要的是「数据源里该端口有多少」, 用被截断过的
    # uniq 统计会让这个字段名在设了 MAX_CHECK_NODES 之后撒谎 (误杀判断会偏)。
    uniq_all = dedupe(sstp_nodes)
    uniq = uniq_all
    if MAX_CHECK_NODES > 0:
        uniq = uniq[:MAX_CHECK_NODES]

    # 上一版产物: 既用于识别「本轮新出现的节点」, 也用于降级与合并发布
    last_good = load_last_good(session) if (PREFER_NEW_NODES or MERGE_LAST_GOOD) else None
    known_keys = last_good_keys(last_good)
    new_candidates = sum(1 for n in uniq if (n["host"].lower(), n["port"]) not in known_keys) if known_keys else 0

    selected = select_check_targets(uniq, known_keys or None, CHECK_BUDGET)
    log("VPN GATE", f"原始节点 {raw_count} / SSTP {sstp_count} / 去重后 {len(uniq)} (来源 {source})")
    if known_keys:
        log("VPN GATE", f"其中新节点 {new_candidates} 个 (上一版 {len(known_keys)} 个已知)" + (" -> 新节点优先占用检测额度" if PREFER_NEW_NODES else ""))
    if CHECK_BUDGET and len(selected) < len(uniq):
        log("VPN GATE", f"检测预算 CHECK_BUDGET={CHECK_BUDGET}: 只检测 {len(selected)}/{len(uniq)} 个")
    log("CLOUDFLARE WORKER", f"提交检测: {len(selected)} (并发 {CONCURRENCY}, 单请求超时 {CHECK_TIMEOUT}s, 服务异常重试 {CHECK_RETRIES} 次, 目标限流重试 {TARGET_RL_RETRIES} 次)")

    breaker = PortCircuitBreaker(PORT_CB_THRESHOLD)
    t0 = time.time()
    results, limiter = check_all(selected, session, breaker=breaker)
    elapsed = time.time() - t0
    checked_count = len(results)

    tripped = breaker.tripped()
    if tripped:
        warn(f"目标侧限流熔断端口: {', '.join(str(p) for p in tripped)} (共跳过 {breaker.skipped} 个节点) "
             f"— 这些节点本轮「未测」, 不是确认不通")
    throttle_state = limiter.snapshot() if hasattr(limiter, "snapshot") else {}
    if throttle_state.get("pauses"):
        log("CLOUDFLARE WORKER", f"全局自适应退避触发 {throttle_state['pauses']} 次, 当前请求间隔 {throttle_state.get('interval_s')}s")

    success = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    worker_errors = [r for r in failed if r.get("worker_error")]
    node_failed = [r for r in failed if not r.get("worker_error")]
    rate_limited = [r for r in failed if r.get("failure_kind") == "target_ratelimit"]
    svc_errors = [r for r in failed if r.get("failure_kind") == "worker_error"]

    if MERGE_LAST_GOOD:
        carried = carry_over_nodes(results, last_good)
        if carried:
            log("MERGE", f"合并发布: 并入上一版仍可用的 {len(carried)} 个节点 (本轮未覆盖/未被确认不通)")
            results = results + carried
            success = [r for r in results if r.get("success")]

    # 注意含关系: 被熔断跳过的节点 failure_kind 也是 target_ratelimit (它是「未测」不是「不通」),
    # 所以 target_ratelimit = 真撞 429 的数量 + 熔断跳过的数量。两个数字不并列、不能相加。
    probed_rl = [r for r in rate_limited if not r.get("skipped_by_breaker")]
    # 每个被熔断端口的候选总数: 没有它就无法判断「跳过 7 个」是正常熔断还是整段砍掉
    # 两个口径都要留: 一旦设了 CHECK_BUDGET / MAX_CHECK_NODES, 「数据源里该端口有多少候选」
    # 与「本轮计划检测多少」就是两个不同的数 —— 判断「是否整段误杀」要看前者。
    port_candidates = {}          # 本轮计划检测 (截断后)
    port_candidates_source = {}   # 数据源原始 (截断前)
    for n in selected:
        port_candidates[n["port"]] = port_candidates.get(n["port"], 0) + 1
    for n in uniq_all:
        port_candidates_source[n["port"]] = port_candidates_source.get(n["port"], 0) + 1
    # list-of-dict 而非 {port: {...}}: JSON 的 key 只能是字符串, 写成 dict 会让消费方
    # 拿到 "443" 而非 443, 直接按 int 取值会静默失败 (不会报错, 只会什么都取不到)。
    port_detail = []
    for p in tripped:
        port_detail.append({
            "port": p,
            "candidates": port_candidates.get(p, 0),
            "candidates_in_source": port_candidates_source.get(p, 0),
            "probed_429": sum(1 for r in probed_rl if r.get("port") == p),
            "skipped": sum(1 for r in failed if r.get("skipped_by_breaker") and r.get("port") == p),
        })

    breakdown = {"target_ratelimit": len(rate_limited), "worker_error": len(svc_errors),
                 "node_dead": len(node_failed), "breaker_skipped": breaker.skipped,
                 "target_ratelimit_probed": len(probed_rl)}
    # 自检: 三类归因必须恰好等于失败总数, 出现重复计数/漏计立刻暴露
    if sum(breakdown[k] for k in ("target_ratelimit", "worker_error", "node_dead")) != len(failed):
        log("CLOUDFLARE WORKER", f"  警告: 失败归因计数不自洽 {breakdown} vs 失败总数 {len(failed)}")
    if port_detail:
        for d in port_detail:
            log("CLOUDFLARE WORKER", f"  端口 {d['port']}: 候选 {d['candidates']} 个 (源 {d['candidates_in_source']} 个), 真撞 429 {d['probed_429']} 个节点, 熔断跳过 {d['skipped']} 个")

    log("CLOUDFLARE WORKER", f"检测成功: {len(success)}")
    log("CLOUDFLARE WORKER", f"检测失败: {len(failed)} (节点侧不通 {len(node_failed)}, 检测服务异常 {len(svc_errors)}, 目标侧限流未测 {len(rate_limited)})")
    log("CLOUDFLARE WORKER", f"耗时: {elapsed:.1f}s")

    stats = {
        "raw_nodes": raw_count,
        "sstp_nodes": sstp_count,
        "checked": checked_count,
        "success": len(success),
        "node_failed": len(node_failed),
        "worker_errors": len(worker_errors),
        "rate_limited": len(rate_limited),
        "failure_breakdown": breakdown,
    }

    if not success:
        if rate_limited and len(rate_limited) == len(results):
            reason = (f"本轮所有节点都被目标服务器限流 (429, 共 {len(rate_limited)} 个) — 不是节点不通, "
                      f"是目标侧对 Cloudflare 出口 IP 限速; 已在 status.json.failure_breakdown 中单列")
        elif worker_errors and len(worker_errors) == len(results):
            reason = f"检测服务全部异常 (共 {len(worker_errors)} 个请求失败, 典型原因: {top_error(worker_errors)}) — 不是节点问题"
        elif worker_errors or rate_limited:
            reason = (f"本轮无可用节点: 节点侧不通 {len(node_failed)} 个, 检测服务异常 {len(svc_errors)} 个, "
                      f"目标侧限流 {len(rate_limited)} 个 (典型原因: {top_error(failed)})")
        else:
            reason = f"本轮所有节点都检测不通 ({len(node_failed)} 个, 典型原因: {top_error(node_failed)})"
        return finish_degraded(session, reason, started, last_good=last_good, source=source,
                               rate_limited_ports=tripped, rate_limited_ports_detail=port_detail or None, **stats)

    if len(success) < MIN_SUCCESS:
        if last_good is None:
            last_good = load_last_good(session)
        if last_good and last_good["count"] > len(success):
            reason = (f"本轮只有 {len(success)} 个可用节点 (< MIN_SUCCESS={MIN_SUCCESS}), 上一版有 {last_good['count']} 个, "
                      f"为避免订阅变差保留上一版 (本轮检测服务异常 {len(svc_errors)} 次, 目标侧限流 {len(rate_limited)} 次)")
            return finish_degraded(session, reason, started, last_good=last_good, source=source,
                                   rate_limited_ports=tripped, rate_limited_ports_detail=port_detail or None, **stats)

    data = build_outputs(results, raw_count, sstp_count, source)
    # 自动优选: 在生成节点行之前现取当轮优选 IP。超时/接口挂了会自动退回静态池
    # (见 fetch_optimal_hosts), 这一步永远不阻断发布 —— 优选服务抽风不该让整轮流水线失败。
    optimal_hosts = fetch_optimal_hosts(session)
    data["optimal_api"] = {
        "count": len(optimal_hosts),
        "hosts": optimal_hosts,
        "sources": list(OPTIMAL_API),
    }
    nodes_text = build_nodes_text(data, optimal_hosts=optimal_hosts)
    format_problems = validate_nodes_text(nodes_text)
    if format_problems:
        reason = "生成的节点行未通过格式自检, 不发布 (可能是入口池配置异常): " + "; ".join(format_problems[:3])
        return finish_degraded(session, reason, started, last_good=last_good, source=source,
                               rate_limited_ports=tripped, rate_limited_ports_detail=port_detail or None, **stats)

    new_ok = 0
    if known_keys:
        new_ok = sum(1 for r in success if (str(r.get("host") or "").lower(), r.get("port")) not in known_keys)
    if rate_limited:
        fresh_reason = f"正常更新 (目标侧限流未测 {len(rate_limited)} 个, 检测服务异常 {len(svc_errors)} 次)"
        if tripped:
            fresh_reason += f"; 已熔断端口 {', '.join(str(p) for p in tripped)}"
    elif worker_errors:
        fresh_reason = f"正常更新 (检测服务异常 {len(svc_errors)} 次)"
    else:
        fresh_reason = "正常更新"

    status = build_status(
        "fresh",
        fresh_reason,
        started,
        source=source,
        stats=data["stats"],
        worker_errors=len(worker_errors),
        rate_limited=len(rate_limited),
        failure_breakdown=breakdown,
        rate_limited_ports=tripped, rate_limited_ports_detail=port_detail or None,
        new_nodes=new_ok,
        check_budget=CHECK_BUDGET or None,
        candidates=len(uniq),
        carried_over=sum(1 for r in success if r.get("carried_over")),
    )
    paths = write_outputs(data, status, nodes_text)

    log("RESULT", f"可用节点: {len(success)}")
    log("RESULT", f"国家数量: {data['stats']['countries']}")
    for p in paths:
        log("WEBSITE", f"生成 {os.path.relpath(p, REPO_DIR)}")
    log("USAGE", f"自动轮换: 把 {NODES_URL} 填入 edgetunnel 后台「自定义优选IP」框 (一次配置, 之后每 12 小时自动更新)")
    log("WEBSITE", "完成 (GitHub Pages 部署由 workflow 执行)")
    log_diagnostics(status)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        die(f"程序异常: {type(exc).__name__}: {exc} (完整 traceback 见上方)")
