"""配置与产物格式的单元级校验 (回归防护)。

背景: 2026-10-07 发现加固版把 `_env_csv()` 返回的字符串直接 for 遍历, 入口池被按「字符」
拆成 2447 个单字母条目 —— 而当时的测试只数行数、不校验格式, 于是「测试全绿、产物全废」。
本文件专门校验「内容」而不是「数量」, 并且会**现场合成事故版**来证明测试确实有效。

校验对象 (存在哪个就测哪个):
  <仓库根>/vpngate.py     线上/仓库里的脚本 (预期通过)
  out/vpngate.py          工作区加固版 (预期通过)
  snapshots/pushed/...    线上已推送快照 (预期通过)
  现场合成的事故版         预期必须被判定为有问题

用法: python tests/test_output_format.py
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(TESTS))

DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,}$")
NODE_LINE_RE = re.compile(
    r"^(?P<entry>[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?)#(?P<name>[^$]+)"
    r"\$sstp://vpn:vpn@(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*):(?P<port>\d+)$"
)

CLEAR_ENV = ["EDGE_HOSTS", "EDGE_HOST_LOCKED", "HOSTS_ENTRY", "EDGE_HOST_BLACKLIST", "EDGE_HOST_WHITELIST", "CN_DNS", "VPNGATE_SOURCES"]
SET_ENV = {"EDGE_DNS_CHECK": "0"}
SYNTH_PATH = TESTS / "_synthesized_buggy.py"

SAMPLE_DATA = {
    "generated_at": "2026-10-07 00:00:00 UTC",
    "source": "unit-test",
    "worker": "https://worker.example/check?sstp=vpn:vpn@",
    "stats": {"countries": 2},
    "countries": {
        "日本": {"code": "JP", "count": 2, "nodes": [
            {"host": "vpn111111111.opengw.net", "port": 443, "residential": "residential", "latency_ms": 120, "country": "日本", "country_code": "JP"},
            {"host": "vpn222222222.opengw.net", "port": 8443, "residential": "unknown", "latency_ms": 300, "country": "日本", "country_code": "JP"},
        ]},
        "美国": {"code": "US", "count": 1, "nodes": [
            {"host": "vpn333333333.opengw.net", "port": 1955, "residential": "datacenter", "latency_ms": 200, "country": "美国", "country_code": "US"},
        ]},
    },
    "available": [],
}


def load_module(path, env_overrides=None, clear_env=()):
    """按给定环境变量执行一遍被测脚本 (只做模块级初始化, 不跑 main)。"""
    saved = {k: os.environ.get(k) for k in list(env_overrides or {}) + list(clear_env)}
    for k in clear_env:
        os.environ.pop(k, None)
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    mod = types.ModuleType("vg_" + path.stem)
    mod.__dict__["__name__"] = mod.__name__
    mod.__dict__["__file__"] = str(path)
    mod.__dict__["__package__"] = None
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    try:
        exec(code, mod.__dict__)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def synthesize_buggy(fixed_path, out_name="_synthesized_buggy.py", disable_selfcheck=True):
    """由加固版现场合成 2026-10-07 的事故版。

    忠实还原当时那份代码的形态: `_env_csv()` 返回**字符串**, 其余调用点都写了 `.split(",")`,
    只有 `EDGE_HOSTS` / `EDGE_HOST_LOCKED` 忘了写 —— 于是这两处按「字符」遍历。
    (若把返回类型改回字符串却忘了补其余调用点, 数据源列表也会被拆开, 那就不是当时的形态了。)
    """
    text = fixed_path.read_text(encoding="utf-8")
    replacements = [
        # 1) 返回类型改回字符串
        ('    return [item.strip() for item in str(raw).split(",") if item.strip()]',
         '    return raw  # 事故版: 返回字符串'),
        # 2) 其余调用点补上 split (当时它们是对的)
        ('    for s in _env_csv("VPNGATE_SOURCES", f"csv:{VPNGATE_API},json:{VPNGATE_MIRROR}")',
         '    for s in _env_csv("VPNGATE_SOURCES", f"csv:{VPNGATE_API},json:{VPNGATE_MIRROR}").split(",")'),
        ('CN_DNS_LIST = _env_csv("CN_DNS", "223.5.5.5,119.29.29.29,114.114.114.114")',
         'CN_DNS_LIST = [s.strip() for s in _env_csv("CN_DNS", "223.5.5.5,119.29.29.29,114.114.114.114").split(",") if s.strip()]'),
        ('EDGE_HOST_BLACKLIST = set(_env_csv("EDGE_HOST_BLACKLIST", "www.bilibili.com"))',
         'EDGE_HOST_BLACKLIST = {s.strip() for s in _env_csv("EDGE_HOST_BLACKLIST", "www.bilibili.com").split(",") if s.strip()}'),
        ('EDGE_HOST_WHITELIST = set(_env_csv("EDGE_HOST_WHITELIST", ""))',
         'EDGE_HOST_WHITELIST = {s.strip() for s in _env_csv("EDGE_HOST_WHITELIST", "").split(",") if s.strip()}'),
        # 3) 这两处就是当时漏掉的 split -> 按字符遍历
        ('EDGE_HOSTS = _env_csv("EDGE_HOSTS", _DEFAULT_EDGE_HOSTS)',
         'EDGE_HOSTS = [h.strip() for h in _env_csv("EDGE_HOSTS", _DEFAULT_EDGE_HOSTS) if h.strip()]'),
        ('EDGE_HOST_LOCKED = {entry.split(":")[0].strip() for entry in _env_csv("EDGE_HOST_LOCKED", _DEFAULT_EDGE_HOSTS)}',
         'EDGE_HOST_LOCKED = {s.strip() for s in _env_csv("EDGE_HOST_LOCKED", _DEFAULT_EDGE_HOSTS) if s.strip()}'),
    ]
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(f"无法合成事故版: 源码里找不到预期片段 -> {old[:70]}... (请更新本测试)")
        text = text.replace(old, new, 1)
    if disable_selfcheck:
        # 必须插在 __main__ 守卫之前 —— 追加到文件末尾的话, 脚本一运行就 sys.exit 了, 注入不会生效
        marker = 'if __name__ == "__main__":'
        if marker not in text:
            raise RuntimeError("无法合成事故版: 找不到 __main__ 守卫 (请更新本测试)")
        text = text.replace(
            marker,
            "# [测试注入] 模拟加固前的版本: 没有发布前产物自检\n"
            "def validate_nodes_text(text):\n"
            "    return []\n\n\n" + marker,
            1,
        )
    out = TESTS / out_name
    out.write_text(text, encoding="utf-8")
    return out


def check_version(path, should_pass):
    problems = []
    mod = load_module(path, SET_ENV, CLEAR_ENV)

    default_list = [h.strip() for h in ",".join(mod._DEFAULT_EDGE_HOSTS).split(",") if h.strip()]
    edge_hosts = list(mod.EDGE_HOSTS)
    locked = set(mod.EDGE_HOST_LOCKED)

    # 1) 入口池必须是「完整条目」, 不是被拆开的字符
    if len(edge_hosts) != len(default_list):
        problems.append(f"EDGE_HOSTS 条目数 {len(edge_hosts)} != 默认池 {len(default_list)} (疑似按字符拆开了)")
    bad_entries = [h for h in edge_hosts if not DOMAIN_RE.match(h.split(":")[0])]
    if bad_entries:
        problems.append(f"EDGE_HOSTS 有 {len(bad_entries)} 个条目不是域名, 例如 {bad_entries[:5]}")

    # 2) 锁定名单必须按主机名存 (不带端口), 否则 filter_edge_hosts 永远匹配不上
    with_port = [h for h in locked if ":" in h]
    if with_port:
        problems.append(f"EDGE_HOST_LOCKED 有 {len(with_port)} 个条目带端口 (比较口径不一致), 例如 {with_port[:3]}")

    # 3) 默认池必须 100% 命中锁定名单
    hit = sum(1 for h in default_list if h.split(":")[0] in locked)
    if hit != len(default_list):
        problems.append(f"锁定命中 {hit}/{len(default_list)} (应为全部命中)")

    # 4) 环境变量覆盖后必须仍是完整条目
    mod2 = load_module(path, {**SET_ENV, "EDGE_HOSTS": "a.example:443,b.example:443,c.example:443"}, CLEAR_ENV)
    if list(mod2.EDGE_HOSTS) != ["a.example:443", "b.example:443", "c.example:443"]:
        problems.append(f"EDGE_HOSTS 环境变量解析错误: {list(mod2.EDGE_HOSTS)[:6]}")

    # 5) 产物格式: 每行都必须是 入口[#名字]$sstp://vpn:vpn@主机:端口
    text = mod.build_nodes_text(SAMPLE_DATA, edge_hosts=["entry-a.example:443", "entry-b.example:443"])
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 3:
        problems.append(f"应有 3 行节点, 实际 {len(lines)}: {lines[:3]}")
    for ln in lines:
        m = NODE_LINE_RE.match(ln)
        if not m:
            problems.append(f"节点行格式非法: {ln[:80]}")
            continue
        if m.group("entry").split(":")[0] not in ("entry-a.example", "entry-b.example"):
            problems.append(f"入口不是预期域名: {m.group('entry')!r}")
        if not DOMAIN_RE.match(m.group("host")):
            problems.append(f"节点主机不是域名: {m.group('host')!r}")

    # 6) 发布前自检函数本身 (加固版新增)
    if should_pass and hasattr(mod, "validate_nodes_text"):
        if mod.validate_nodes_text("entry.example:443#日本-住宅-01$sstp://vpn:vpn@vpn1.opengw.net:443\n"):
            problems.append("validate_nodes_text 对合法行误报")
        if not mod.validate_nodes_text("e#日本-住宅-01$sstp://vpn:vpn@vpn1.opengw.net:443\n"):
            problems.append("validate_nodes_text 没抓到单字母入口")
        if not mod.validate_nodes_text(""):
            problems.append("validate_nodes_text 没抓到空文本")

    # 7) 官方 CSV 表头容错
    if hasattr(mod, "parse_csv"):
        for label2, txt in (("标准表头", "#HostName,IP\n*vpn1,10.0.0.1\n"), ("带空格表头", "# HostName,IP\n*vpn1,10.0.0.1\n")):
            try:
                mod.parse_csv(txt)
            except Exception as exc:
                problems.append(f"parse_csv 无法解析{label2}: {type(exc).__name__}: {exc}")

    return problems


def collect_versions():
    out = []
    for label, path in (("repo(仓库根)", ROOT / "vpngate.py"),
                        ("out(工作区)", ROOT / "out" / "vpngate.py"),
                        ("pushed(快照)", ROOT / "snapshots" / "pushed" / "vpngate.py")):
        if path.exists():
            out.append((label, path, True))
    fixed = next((p for _, p, _ in out), None)
    if fixed is None:
        print("找不到任何被测脚本 (需要仓库根 vpngate.py 或 out/vpngate.py)")
        return None, None
    out.append(("合成事故版", synthesize_buggy(fixed), False))
    return out, fixed


def equivalence_check(fixed_path):
    """加固版与线上推送版 (若快照存在) 的产物必须逐字节一致。"""
    pushed = ROOT / "snapshots" / "pushed" / "vpngate.py"
    if not pushed.exists() or pushed == fixed_path:
        return []
    problems = []
    a = load_module(fixed_path, SET_ENV, CLEAR_ENV)
    b = load_module(pushed, SET_ENV, CLEAR_ENV)
    ta = a.build_nodes_text(SAMPLE_DATA, edge_hosts=["entry-a.example:443", "entry-b.example:443"])
    tb = b.build_nodes_text(SAMPLE_DATA, edge_hosts=["entry-a.example:443", "entry-b.example:443"])
    if ta != tb:
        problems.append("两版 build_nodes_text 输出不一致:\n  fixed : " + ta.replace("\n", " | ") + "\n  pushed: " + tb.replace("\n", " | "))
    if (sorted(a.EDGE_HOSTS), sorted(a.EDGE_HOST_LOCKED)) != (sorted(b.EDGE_HOSTS), sorted(b.EDGE_HOST_LOCKED)):
        problems.append("两版入口池/锁定名单配置不一致")
    return problems


def main():
    versions, fixed = collect_versions()
    if versions is None:
        return 1

    failures = []
    print(f"{'版本':<16}{'结果':<10}说明")
    print("-" * 100)
    for label, path, should_pass in versions:
        try:
            problems = check_version(path, should_pass)
        except Exception as exc:
            problems = [f"执行异常: {type(exc).__name__}: {exc}"]
        ok = not problems
        if should_pass:
            verdict = "PASS" if ok else "FAIL"
            if not ok:
                failures.append((label, problems))
        else:
            verdict = "PASS(已抓到)" if not ok else "FAIL(没抓到!)"
            if ok:
                failures.append((label, ["预期应失败, 实际全部通过 —— 测试无效"]))
        print(f"{label:<16}{verdict:<10}{'' if ok else '; '.join(problems[:3])}")

    eq = equivalence_check(fixed)
    if eq:
        failures.append(("等价性", eq))
    print("-" * 100)
    if failures:
        print("未达预期:")
        for name, probs in failures:
            print(f"  - {name}: {'; '.join(probs)}")
        return 1
    print("配置与产物格式校验: 全部符合预期 (正常版本通过, 合成事故版被成功抓到)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
