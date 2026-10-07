"""复现 2026-10-06 那次「0 秒失败」: 用 CI 的真实环境 (不设 EDGE_HOSTS / EDGE_HOST_LOCKED) 跑三个版本。

结论应为:
  run3 (a2c3f20, 上一次成功)  -> 正常完成
  head (415ca23, 失败的那次)  -> import 阶段 AttributeError, 秒级退出 1  <== 与 Actions 记录完全一致
  fixed (加固版)              -> 正常完成
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
RUNS = TESTS / "runs-repro"

VERSIONS = {
    "run3-ok": ROOT / "snapshots" / "run3" / "gate-a2c3f209a1e93dda3974892111756fb4e7e07770",
    "head-failed": ROOT / "snapshots" / "head" / "gate-main",
    "fixed": ROOT / "out",
}

# 与 .github/workflows/check.yml 完全一致的 env (只把 Worker 指向本地替身), 刻意不设 EDGE_HOSTS / EDGE_HOST_LOCKED
CI_ENV = {
    "CHECK_WORKER": "https://worker.example/check?sstp=vpn:vpn@",
    "CHECK_CONCURRENCY": "32",
    "CHECK_TIMEOUT": "90",
    "NODES_URL": "https://last.good/gate/nodes.txt",
    "DATA_URL": "https://last.good/gate/data.json",
    "FAKE_NODES": "12",
    "FAKE_DELAY": "0.001",
    "FAKE_WORKER": "ok",
    "PYTHONIOENCODING": "utf-8",
}


def run(name, src_dir):
    run_dir = RUNS / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "web").mkdir(parents=True)
    shutil.copy2(src_dir / "vpngate.py", run_dir / "vpngate.py")
    web = src_dir / "web" / "index.html"
    if web.exists():
        shutil.copy2(web, run_dir / "web" / "index.html")

    env = dict(os.environ)
    env.update(CI_ENV)
    env["PUBLIC_DIR"] = str(run_dir / "public")
    env["PYTHONPATH"] = str(TESTS)
    for var in ("EDGE_HOSTS", "EDGE_HOST_LOCKED"):
        env.pop(var, None)

    started = time.time()
    proc = subprocess.run([sys.executable, str(run_dir / "vpngate.py")], cwd=str(run_dir), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    seconds = round(time.time() - started, 2)
    (run_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")

    nodes = run_dir / "public" / "nodes.txt"
    nodes_lines = len([ln for ln in nodes.read_text(encoding="utf-8").splitlines() if ln.strip()]) if nodes.exists() else None
    first_error = ""
    for line in reversed((proc.stderr or "").strip().splitlines()):
        if line.strip():
            first_error = line.strip()
            break
    return {"version": name, "exit": proc.returncode, "seconds": seconds,
            "nodes_lines": nodes_lines, "last_stderr_line": first_error}


def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    print(f"{'版本':<14}{'退出码':<8}{'耗时':<9}{'nodes.txt':<11}stderr 末尾")
    print("-" * 110)
    rows = []
    for name, src in VERSIONS.items():
        r = run(name, src)
        rows.append(r)
        print(f"{r['version']:<14}{r['exit']:<8}{str(r['seconds']) + 's':<9}{str(r['nodes_lines']):<11}{r['last_stderr_line'][:70]}")

    print("-" * 110)
    ok = True
    if rows[0]["exit"] != 0:
        ok = False
        print("FAIL: run3 版本本应正常完成")
    if rows[1]["exit"] != 1 or rows[1]["seconds"] > 3 or "AttributeError" not in rows[1]["last_stderr_line"]:
        ok = False
        print("FAIL: head 版本应复现「秒级退出 1 且 AttributeError」")
    if rows[2]["exit"] != 0 or not (rows[2]["nodes_lines"] or 0) > 0:
        ok = False
        print("FAIL: 加固版应正常完成并产出节点")
    print("复现结果: " + ("符合预期 —— 失败原因确认" if ok else "与预期不符"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
