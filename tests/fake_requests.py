"""离线测试替身: 冒充 requests 模块, 由环境变量控制每个依赖的行为。

用法: PYTHONPATH=<tests dir> python <vpngate.py>
环境变量:
  FAKE_PRIMARY   ok | timeout | http500 | empty      官方 CSV 数据源
  FAKE_MIRROR    ok | timeout | http404 | empty      GitHub 镜像数据源
  FAKE_WORKER    ok | all429 | http503 | timeout | mixed
  FAKE_LASTGOOD  present | missing                   Pages 上的上一版产物
  FAKE_NODES     生成多少个原始节点 (默认 12)
  FAKE_LOG       把每次请求追加记录到这个文件
"""
import base64
import json
import os
import threading
import time
from urllib.parse import unquote

__version__ = "fake-1.0"

_LOG_LOCK = threading.Lock()


class FakeError(Exception):
    """模拟 requests 的网络/HTTP 异常。"""


class _Response:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("fake: 响应不是 JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeError(f"HTTP {self.status_code}: fake error")


def _log(line):
    """并发安全地记录一次请求 (多线程下逐行追加, 用于统计请求/重试次数)。"""
    path = os.environ.get("FAKE_LOG")
    if not path:
        return
    with _LOG_LOCK:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def _delay():
    time.sleep(float(os.environ.get("FAKE_DELAY", "0.002")))


def _config_text(host, port, proto):
    return f"client\ndev tun\ndev-type tun\nproto {proto}\nremote {host} {port}\nresolv-retry infinite\n"


def _nodes():
    n = int(os.environ.get("FAKE_NODES", "12"))
    out = []
    countries = [("Japan", "JP"), ("Korea", "KR"), ("United States", "US")]
    for i in range(n):
        host = f"vpn{100000000 + i}"
        country, code = countries[i % len(countries)]
        proto = "udp" if i % 6 == 5 else "tcp"          # 每 6 个里有 1 个不是 TCP -> 应被过滤掉
        port = 443 + i
        out.append({
            "hostname": host,
            "ip": f"10.0.{i // 250}.{i % 250}",
            "countrylong": country,
            "countryshort": code,
            "proto": proto,
            "port": port,
            "config_b64": base64.b64encode(_config_text(f"{host}.opengw.net", port, proto).encode()).decode(),
        })
    return out


def _csv_text():
    # real  = 与官方 API 实际返回一致 (#HostName,... 且数据行不带 * 前缀)
    # spaced= 表头写成 "# HostName,..." 且数据行带 * 前缀 (用于验证解析器的容错)
    style = os.environ.get("FAKE_CSV_STYLE", "real")
    if style == "spaced":
        header = ("# HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,"
                  "TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64")
        prefix = "*"
    else:
        header = ("#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,"
                  "TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64")
        prefix = ""
    lines = [header]
    for i, n in enumerate(_nodes()):
        lines.append(f"{prefix}{n['hostname']},{n['ip']},100,50,1000000,{n['countrylong']},{n['countryshort']},"
                     f"10,100,20,300,2weeks,AS0,,{n['config_b64']}")
    return "\n".join(lines) + "\n"


def _mirror_payload():
    return [{"servers": [
        {
            "hostname": n["hostname"], "ip": n["ip"], "countrylong": n["countrylong"],
            "countryshort": n["countryshort"], "openvpn_configdata_base64": n["config_b64"],
        } for n in _nodes()
    ]}]


LAST_GOOD_NODES = (
    "entry-a.example:443#日本-住宅-01$sstp://vpn:vpn@vpn900000001.opengw.net:443\n"
    "entry-b.example:443#日本-住宅-02$sstp://vpn:vpn@vpn900000002.opengw.net:443\n"
    "entry-c.example:443#韩国-住宅-01$sstp://vpn:vpn@vpn900000003.opengw.net:443\n"
    "entry-d.example:443#韩国-机房-01$sstp://vpn:vpn@vpn900000004.opengw.net:443\n"
    "entry-e.example:443#美国-住宅-01$sstp://vpn:vpn@vpn900000005.opengw.net:443\n"
    "entry-f.example:443#美国-住宅-02$sstp://vpn:vpn@vpn900000006.opengw.net:443\n"
    "entry-g.example:443#日本-机房-01$sstp://vpn:vpn@vpn900000007.opengw.net:443\n"
    "entry-h.example:443#韩国-住宅-03$sstp://vpn:vpn@vpn900000008.opengw.net:443\n"
)
LAST_GOOD_DATA = {
    "generated_at": "2026-10-06 21:40:08 UTC",
    "source": "vpngate.net",
    "worker": "https://worker.example/check?sstp=vpn:vpn@",
    "stats": {"raw_nodes": 98, "sstp_nodes": 83, "checked": 83, "success": 8, "failed": 75, "countries": 3},
    "countries": {},
    "available": [],
}


def _port_of(target):
    try:
        return int(target.rsplit(":", 1)[1])
    except Exception:
        return 0


def _worker(target, mode):
    if mode == "timeout":
        raise FakeError("ConnectTimeout: fake worker timeout")
    if mode == "http503":
        return _Response(503, "Service Unavailable")
    if mode == "all429":
        return _Response(200, payload={
            "success": False, "colo": "AMS", "responseTime": 5200,
            "error": "Target /api/lookup request failed: HTTP/1.1 429 Too Many Requests",
        })
    if mode == "partial_429":
        # 端口决定结果, 完全可复现: %3==0 成功 / %3==1 撞上游限速 / %3==2 节点不通
        bucket = _port_of(target) % 3
        if bucket == 0:
            return _Response(200, payload={
                "success": True, "colo": "NRT", "responseTime": 200,
                "exit": {"ip": "121.143.44.4", "country": "Korea", "country_code": "KR", "city": "Seoul",
                         "continent": "AS", "is_datacenter": False,
                         "asn": {"asn": 4766, "org": "Korea Telecom", "type": "isp"}},
            })
        if bucket == 1:
            return _Response(200, payload={
                "success": False, "colo": "AMS", "responseTime": 5200,
                "error": "Target /api/lookup request failed: HTTP/1.1 429 Too Many Requests",
            })
        return _Response(200, payload={
            "success": False, "colo": "NRT", "responseTime": 9000,
            "error": "SSTP server connection timed out",
        })
    if mode == "mixed_low":
        # 只有前 2 个节点可用, 其余节点侧不通 (模拟节点大面积失效)
        ok_targets = {"vpn100000000.opengw.net:443", "vpn100000001.opengw.net:444"}
        if target in ok_targets:
            return _Response(200, payload={
                "success": True, "colo": "NRT", "responseTime": 200,
                "exit": {"ip": "121.143.44.4", "country": "Korea", "country_code": "KR", "city": "Seoul",
                         "continent": "AS", "is_datacenter": False,
                         "asn": {"asn": 4766, "org": "Korea Telecom", "type": "isp"}},
            })
        return _Response(200, payload={
            "success": False, "colo": "NRT", "responseTime": 8000,
            "error": "SSTP server connection timed out",
        })
    # ok: 约 2/3 的节点可用 (由端口决定, 可复现); 其余节点侧不通
    if _port_of(target) % 3 != 0:
        return _Response(200, payload={
            "success": True, "colo": "NRT", "responseTime": 120 + _port_of(target) % 300,
            "exit": {"ip": "121.143.44.4", "country": "Korea", "country_code": "KR", "city": "Seoul",
                     "continent": "AS", "is_datacenter": False,
                     "asn": {"asn": 4766, "org": "Korea Telecom", "type": "isp"}},
        })
    return _Response(200, payload={
        "success": False, "colo": "NRT", "responseTime": 9000,
        "error": "SSTP server connection timed out",
    })


class Session:
    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None, headers=None, **kwargs):
        _delay()
        if "vpngate.net" in url:
            mode = os.environ.get("FAKE_PRIMARY", "ok")
            _log(f"primary:{mode}")
            if mode == "timeout":
                raise FakeError("ConnectTimeout: fake primary timeout")
            if mode == "http500":
                return _Response(500, "Internal Server Error")
            if mode == "empty":
                return _Response(200, "# HostName,IP\n")
            return _Response(200, _csv_text())
        if "Vpngate-Scraper-API" in url:
            mode = os.environ.get("FAKE_MIRROR", "ok")
            _log(f"mirror:{mode}")
            if mode == "timeout":
                raise FakeError("ConnectTimeout: fake mirror timeout")
            if mode == "http404":
                return _Response(404, "Not Found")
            if mode == "empty":
                return _Response(200, payload=[])
            return _Response(200, payload=_mirror_payload())
        if "/check?sstp=" in url:
            mode = os.environ.get("FAKE_WORKER", "ok")
            target = unquote(url.split("vpn:vpn@")[-1])
            _log(f"worker:{mode}:{target}")
            return _worker(target, mode)
        if url.split("?")[0].endswith("nodes.txt"):
            mode = os.environ.get("FAKE_LASTGOOD", "present")
            _log(f"lastgood-nodes:{mode}")
            if mode == "missing":
                return _Response(404, "Not Found")
            return _Response(200, LAST_GOOD_NODES)
        if url.split("?")[0].endswith("data.json"):
            mode = os.environ.get("FAKE_LASTGOOD", "present")
            _log(f"lastgood-data:{mode}")
            if mode == "missing":
                return _Response(404, "Not Found")
            return _Response(200, payload=LAST_GOOD_DATA)
        raise FakeError(f"fake: 未处理的 URL {url}")


def get(url, **kwargs):
    return Session().get(url, **kwargs)
