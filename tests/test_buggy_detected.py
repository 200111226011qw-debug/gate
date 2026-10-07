"""端到端证明: 这类「入口按字符拆开」的事故, 现在的流水线一定能挡住。

两个子场景:
  A. 加固前的形态 (坏入口池 + 没有任何自检) -> 产物里入口变成单字母, run_tests 的
     逐行格式断言必须报「入口不是域名」。
  B. 加固后的形态 (坏入口池 + 保留发布前自检) -> 脚本拒绝发布坏产物, 自动降级沿用
     上一版 (status.outcome=stale), 订阅不会被打坏 —— 这正是这道闸的价值。

运行: python tests/test_buggy_detected.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import run_tests as rt  # noqa: E402
from test_output_format import synthesize_buggy  # noqa: E402


def detect(script, label, expect_fresh):
    rt.TARGETS[label] = (script, rt.WEB_DIR)
    rt.EXPECT[label] = dict(rt.EXPECT["_hardened"])
    res = rt.run_case(label, "happy", {})
    problems = rt.check(rt.EXPECT[label]["happy"], res)
    return res, problems


def main():
    fixed = ROOT / "vpngate.py"
    if not fixed.exists():
        fixed = ROOT / "out" / "vpngate.py"
    if not fixed.exists():
        print("找不到被测脚本 (需要仓库根 vpngate.py 或 out/vpngate.py)")
        return 1

    ok = True

    # A. 没有发布前自检的事故版: 端到端测试必须抓到坏入口
    buggy_plain = synthesize_buggy(fixed, "_synthesized_buggy_noguard.py", disable_selfcheck=True)
    res_a, problems_a = detect(buggy_plain, "buggy_noguard", True)
    caught_a = any("入口不是域名" in p or "格式非法" in p for p in problems_a)
    print("A. 事故版(无自检) 跑 happy:")
    print(f"   退出码={res_a['exit']} nodes.txt 行数={res_a['nodes_lines']} 入口前 6 个={res_a['entry_hosts'][:6]}")
    print(f"   run_tests 结论: {'; '.join(problems_a) if problems_a else '(没抓到!)'}")
    print(f"   -> 端到端能抓到坏产物: {'是 ✅' if caught_a else '否 ❌'}")
    ok = ok and caught_a

    # B. 坏入口池 + 保留自检: 必须拒绝发布并降级
    buggy_guarded = synthesize_buggy(fixed, "_synthesized_buggy_guard.py", disable_selfcheck=False)
    res_b, problems_b = detect(buggy_guarded, "buggy_guard", True)
    entries_ok = bool(res_b["entry_hosts"]) and all("." in h for h in res_b["entry_hosts"])
    degraded = res_b["status_outcome"] == "stale" and res_b["exit"] == 0
    print("\nB. 事故版(保留发布前自检) 跑 happy:")
    print(f"   退出码={res_b['exit']} status={res_b['status_outcome']} nodes.txt 行数={res_b['nodes_lines']} 入口={res_b['entry_hosts'][:3]}")
    print(f"   status.reason: {res_b['status_reason']}")
    print(f"   -> 拒绝发布坏产物并沿用上一版: {'是 ✅' if degraded and entries_ok else '否 ❌'}")
    ok = ok and degraded and entries_ok

    print("\n结论: 事故既能在测试里被抓到 (A), 也不会再流到线上订阅 (B): " + ("成立 ✅" if ok else "不成立 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
