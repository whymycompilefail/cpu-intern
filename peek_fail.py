"""抽几道失败题，把模型的原始回复打出来看形态。

不判分、不落盘，只回答「它到底吐了什么」。module 契约（harness 默认）。
用法：python peek_fail.py <id> [<id> ...]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "repair_eval"))
import run_repair  # noqa: E402

BASE = "http://127.0.0.1:8299"

probs = json.load(open(os.path.join(ROOT, "repair_eval", "problems_hard.json"),
                       encoding="utf-8"))["problems"]
by_id = {p["id"]: p for p in probs}

for pid in sys.argv[1:]:
    p = by_id[pid]
    prompt = run_repair.build_prompt(p, "module")
    r = run_repair.chat(BASE, prompt, "nothink", 1024, 300.0)
    print("=" * 78)
    print("###", pid, "| mtype:", p["mutation"]["mtype"], "| target:", p["target_function"],
          "| bug_lines:", p["bug_lines"])
    print("--- spec:", p["spec"])
    print("--- 模块源码 ---")
    print(p["module_source"].rstrip())
    print("--- 模型回复 (%d tok, %s) ---" % (r["completion_tokens"], r["finish_reason"]))
    print(r["content"])
    code, how = run_repair.extract(r["content"])
    print("--- extract -> how=%s, %d 字符" % (how, len(code)))
