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
    """读取逗号分隔的环境变量; 未设置时使用 default。

    default 允许是字符串或字符串序列 —— 这里必须做类型归一化:
    旧版直接把元组 _DEFAULT_EDGE_HOSTS 传给 os.environ.get 的默认值, 结果
    os.environ.get("EDGE_HOSTS", _DEFAULT_EDGE_HOSTS) 返回 tuple, 再 .split(",")
    会在 import 阶段抛 AttributeError, 整轮运行 0 秒退出 1 (2026-10-06 那次失败就是这个原因)。
    """
    raw = os.environ.get(name)
    if raw is None:
        raw = ",".join(default) if isinstance(default, (tuple, list)) else str(default)
    return raw

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
    for s in _env_csv("VPNGATE_SOURCES", f"csv:{VPNGATE_API},json:{VPNGATE_MIRROR}").split(",")
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
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", os.path.join(REPO_DIR, "public"))
TEMPLATE_HTML = os.path.join(REPO_DIR, "web", "index.html")
STATUS_NAME = "status.json"

# 降级策略
MIN_SUCCESS = max(1, int(os.environ.get("MIN_SUCCESS", "5")))          # 成功数低于该值时, 若上一版更多则保留上一版
KEEP_LAST_GOOD = os.environ.get("KEEP_LAST_GOOD", "1") not in ("0", "false", "False")
STRICT = os.environ.get("STRICT", "0") in ("1", "true", "True")        # 1 = 降级也算失败 (退出码 1)

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
        rows.append({"host": host, "ip": ip, "country_long": str(s.get("countrylong") or s.get("country_long") or s.get("country") or "").strip(), "country_short": str(s.get("countryshort") or s.get("country_short") or "").strip(), "config_b64": str(s.get("openvpn_configdata_base64") or s.get("config_b64") or "").strip()})
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
    r"(429|too many requests|rate.?limit|quota|lookup request failed|/api/lookup|"
    r"internal error|bad gateway|gateway time-?out|service unavailable|temporarily unavailable|"
    r"connection reset|econn|fetch failed|worker exceeded|exceeded cpu)",
    re.I,
)
# 节点侧故障的特征 ("SSTP server connection timed out" 等): 不重试, 属于节点真的不通
_NODE_SIDE_RE = re.compile(r"^\s*SSTP\b", re.I)


def is_worker_side_error(err):
    """判断失败原因属于「检测服务本身出问题」还是「节点真的不通」。"""
    if not err:
        return False
    text = str(err)
    if _NODE_SIDE_RE.search(text):
        return False
    return bool(_WORKER_TROUBLE_RE.search(text))


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


def check_one(node, session, limiter=None):
    url = WORKER_CHECK_URL + quote(f"{node['host']}:{node['port']}", safe="")
    out = dict(node)
    out["protocol"] = "sstp"
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["status"] = "failed"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    out["worker_error"] = False

    attempts = CHECK_RETRIES + 1
    last_error = "check failed"
    worker_side = False
    for attempt in range(1, attempts + 1):
        if limiter is not None:
            limiter.wait()
        try:
            r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            worker_side = True
        else:
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                worker_side = r.status_code in (408, 425, 429, 500, 502, 503, 504)
            else:
                try:
                    j = r.json()
                except Exception as exc:
                    last_error = f"响应不是合法 JSON: {type(exc).__name__}"
                    worker_side = True
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
                        return out
                    # Worker 正常返回但该节点不通: 也要区分是 Worker 侧问题还是节点侧问题
                    last_error = str(j.get("error") or j.get("message") or "check failed")
                    worker_side = is_worker_side_error(last_error)
                    out["colo"] = j.get("colo")
                    out["latency_ms"] = j.get("responseTime")
        out["attempts"] = attempt
        if worker_side and attempt < attempts:
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


def check_all(nodes, session):
    limiter = RateLimiter(CHECK_MIN_INTERVAL)
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(check_one, n, session, limiter) for n in nodes]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results

# ---------------------------------------------------------------------------
# 生成数据
# ---------------------------------------------------------------------------
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
    "cmcc.cc.cd:443,cdn.204910.best:443,cf.qq.ms:443,egov.uscis.gov:443,www.zendesk.com:443,img.css.sd:443,www.blibli.com:443,"
    "cf.nyanya.moe:443,auto.dolby.dpdns.org:443,cdn.jwcmdr.top:443,224322.xyz:443,www.jp.pima.gov:443,cf.92555.xyz:443,"
    "cf.254301.xyz:443,01-cctv.com:443,stores.staples.com:443,cdn.555586.xyz:443,www.5h.com:443,lt.1930812.xyz:443,www.bis.gov:443,"
    "cf.yj250.bond:443,test.509666.xyz:443,www.sage.com:443,01-qq.com:443,www.crazygames.fr:443,so.360832.xyz:443,"
    "www.mastervolt.com:443,cf.itv888.cn:443,neko.cloudd.eu.org:443,versantstore.pearson.com:443,cdn.667891.xyz:443,"
    "neko.cloudflaree.eu.org:443,aqua-aria.company:443,cdn.ctn32.us.kg:443,www.udacity.com:443,cfplus.255520.xyz:443,cdn.2x.nz:443,"
    "ahrefs.com:443,ali.nonull.pp.ua:443,www.mc.js.cool:443,cf2.996616.xyz:443,d.lma.de5.net:443,markmonitor.com:443,"
    "img.856518.xyz:443,cfip-ct.stoeaves.us.ci:443,cf.777791.xyz:443,login.rockwellautomation.com:443,idc.urkeji.com:443,"
    "jellyfin.roddy.eu.cc:443,www.galgamex.net:443,kniu.cc:443,baota.us.kg:443,op.chinwa.eu.cc:443",
)
EDGE_HOSTS = [
    h.strip()
    for h in _env_csv("EDGE_HOSTS", _DEFAULT_EDGE_HOSTS).split(",")
    if h.strip()
]

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
CN_DNS_LIST = [
    s.strip()
    for s in _env_csv("CN_DNS", "223.5.5.5,119.29.29.29,114.114.114.114").split(",")
    if s.strip()
]
EDGE_DNS_CHECK = os.environ.get("EDGE_DNS_CHECK", "1") not in ("0", "false", "False")

# 黑名单: 已知无法作为 Cloudflare 优选入口的域名, 直接剔除, 不参与检测。
#   www.bilibili.com —— 实测解析到 119.84.x / 183.131.x 等国内 IP, 不走 Cloudflare 网段。
#   TCP 虽通, 但 TLS 无法路由到 Worker, 作为入口无效。
EDGE_HOST_BLACKLIST = {
    s.strip() for s in _env_csv("EDGE_HOST_BLACKLIST", "www.bilibili.com").split(",") if s.strip()
}
# 白名单: 经检测确认「不走 CF 网段但确实可用」的入口, 跳过网段校验只测连通性。
#   默认为空 —— 当前 20 个入口中仅 bilibili 异常, 已归入黑名单, 无需豁免。
#   若日后新增 CNAME 类入口(解析不在 CF 段但 TLS 可达), 把域名填进来即可。
EDGE_HOST_WHITELIST = {
    s.strip() for s in _env_csv("EDGE_HOST_WHITELIST", "").split(",") if s.strip()
}
# 锁定名单: 默认 = 默认入口池 (用户本地实测优选域名), 更新时「不动」——
# 这些域名跳过 DNS 污染检测与 TCP 检测, 在自动更新中永远保留。
# 若某个域名希望参与检测, 用 EDGE_HOST_LOCKED 环境变量传入不含它的列表即可。
# 注意: 按「主机名」存储 (去掉 :端口), 与 filter_edge_hosts 里 _split_host_port 的比较口径一致;
#       否则带端口的条目永远匹配不上去端口后的 host, 锁定形同虚设。
EDGE_HOST_LOCKED = {
    s.strip().split(":")[0] for s in _env_csv("EDGE_HOST_LOCKED", _DEFAULT_EDGE_HOSTS).split(",") if s.strip()
}
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


def build_nodes_text(data, edge_hosts=None):
    """生成纯节点行版本 (无注释): 每行 = 入口地址#名字$sstp://..."""
    countries = data["countries"]
    _entry = os.environ.get("HOSTS_ENTRY", "").strip()
    base = [e.strip() for e in _entry.split(",") if e.strip()] or EDGE_HOSTS
    # DNS 污染过滤 (仅在未显式指定 HOSTS_ENTRY 时生效, 保证人工指定优先)
    if not _entry:
        base = filter_edge_hosts(base)
    # 兜底: 极端情况下 (如黑名单清空列表) 回退到原始 EDGE_HOSTS, 避免除零/空轮换
    if not base:
        log("EDGE DNS", "警告: 筛选结果为空, 回退到完整入口列表")
        base = list(EDGE_HOSTS)
    edge = edge_hosts or base
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


def write_outputs(data, status):
    """正常路径: 写入本轮新生成的产物。"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    data["status"] = status
    data_path = write_json(os.path.join(PUBLIC_DIR, "data.json"), data)
    html_path = os.path.join(PUBLIC_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html())
    nodes_path = os.path.join(PUBLIC_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8") as f:
        f.write(build_nodes_text(data))
    status_path = write_json(os.path.join(PUBLIC_DIR, STATUS_NAME), status)
    return data_path, html_path, nodes_path, status_path


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
    stats = status.get("stats") or {}
    if stats:
        print("  本轮统计    : " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if status.get("worker_errors"):
        print(f"  检测服务异常: {status['worker_errors']} 次 (Worker 上游限速/网关问题, 不是节点不通)")
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
    log_diagnostics(status)
    if status["outcome"] == "stale" and not STRICT:
        return 0
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    session = requests.Session()
    started = time.time()

    log("ENV", f"python {sys.version.split()[0]} | requests {getattr(requests, '__version__', '?')} | 并发 {CONCURRENCY} | 检测重试 {CHECK_RETRIES} | Worker {'已配置' if WORKER_URL_OK else '未配置/非法'}")

    if not WORKER_URL_OK:
        return finish_degraded(
            session,
            "CHECK_WORKER 未配置或非法: 请在仓库 Settings → Secrets and variables → Actions 里把 DOMAIN 设为 "
            "https://你的Worker域名/check?sstp=vpn:vpn@ (注意结尾的 check?sstp=vpn:vpn@ 不能少)",
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

    uniq = dedupe(sstp_nodes)
    if MAX_CHECK_NODES > 0:
        uniq = uniq[:MAX_CHECK_NODES]

    log("VPN GATE", f"原始节点 {raw_count} / SSTP {sstp_count} / 去重后 {len(uniq)} (来源 {source})")
    log("CLOUDFLARE WORKER", f"提交检测: {len(uniq)} (并发 {CONCURRENCY}, 单请求超时 {CHECK_TIMEOUT}s, 服务异常重试 {CHECK_RETRIES} 次)")

    t0 = time.time()
    results = check_all(uniq, session)
    elapsed = time.time() - t0

    success = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    worker_errors = [r for r in failed if r.get("worker_error")]
    node_failed = [r for r in failed if not r.get("worker_error")]

    log("CLOUDFLARE WORKER", f"检测成功: {len(success)}")
    log("CLOUDFLARE WORKER", f"检测失败: {len(failed)} (节点侧不通 {len(node_failed)}, 检测服务异常 {len(worker_errors)})")
    log("CLOUDFLARE WORKER", f"耗时: {elapsed:.1f}s")

    stats = {
        "raw_nodes": raw_count,
        "sstp_nodes": sstp_count,
        "checked": len(results),
        "success": len(success),
        "node_failed": len(node_failed),
        "worker_errors": len(worker_errors),
    }

    if not success:
        if worker_errors and len(worker_errors) == len(results):
            reason = f"检测服务全部异常 (共 {len(worker_errors)} 个请求失败, 典型原因: {top_error(worker_errors)}) — 不是节点问题"
        elif worker_errors:
            reason = (f"本轮无可用节点: 节点侧不通 {len(node_failed)} 个, 检测服务异常 {len(worker_errors)} 个 "
                      f"(典型原因: {top_error(failed)})")
        else:
            reason = f"本轮所有节点都检测不通 ({len(node_failed)} 个, 典型原因: {top_error(node_failed)})"
        return finish_degraded(session, reason, started, source=source, **stats)

    if len(success) < MIN_SUCCESS:
        last_good = load_last_good(session)
        if last_good and last_good["count"] > len(success):
            reason = (f"本轮只有 {len(success)} 个可用节点 (< MIN_SUCCESS={MIN_SUCCESS}), 上一版有 {last_good['count']} 个, "
                      f"为避免订阅变差保留上一版 (本轮检测服务异常 {len(worker_errors)} 次)")
            return finish_degraded(session, reason, started, last_good=last_good, source=source, **stats)

    data = build_outputs(results, raw_count, sstp_count, source)
    status = build_status(
        "fresh",
        f"正常更新 (检测服务异常 {len(worker_errors)} 次)" if worker_errors else "正常更新",
        started,
        source=source,
        stats=data["stats"],
        worker_errors=len(worker_errors),
    )
    paths = write_outputs(data, status)

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
