import os, sys, json, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for k in list(os.environ):
    if "PROXY" in k.upper():
        del os.environ[k]

import importlib.util
spec = importlib.util.spec_from_file_location("vg", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpngate.py"))
vg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vg)

LIMIT = int(os.environ.get("DIAG_LIMIT", "24"))
s = __import__("requests").Session()
rows, src = vg.fetch_vpngate(s)
nodes = vg.dedupe(vg.to_sstp_nodes(rows))
print("源:", src, "原始", len(rows), "SSTP", len(nodes))

pref = collections.Counter(n["host"].split(".")[0][:4] for n in nodes)
print("主机名前缀分布:", pref.most_common())
ports = collections.Counter(n["port"] for n in nodes)
print("端口分布(top10):", ports.most_common(10))

# 住宅/机房预判
kind = collections.Counter(vg.classify_network(n["host"], "") for n in nodes)
print("类型预判:", kind.most_common())

sample = nodes[:LIMIT]
print(f"\n=== 实测 {len(sample)} 个节点 ===")
res = vg.check_all(sample, s)
ok = [r for r in res if r.get("success")]
print("成功", len(ok), "/", len(res))
errs = collections.Counter()
for r in res:
    if r.get("success"):
        continue
    e = (r.get("error") or "")[:90]
    errs[e] += 1
print("失败原因分布:")
for e, c in errs.most_common(12):
    print(f"   {c:3d}  {e}")
for r in ok[:8]:
    print("  OK:", r["host"], r["port"], r.get("latency_ms"), r.get("exit_ip"))
