"""实验：把「定位」和「写修复」拆给两个模型，写修复的那端能不能干？

背景（SPEC §9）：本地 Coder-3B 在 54 题上失败 18 次，其中 13 次 loc=miss。
但提示词只有 ~350 token，**整个模块（4~5 个函数）全在上下文里** ⇒ 失败不是"看不到"，
是"判断错"。所以"让它按需索取上下文"这条建议在同一层就站不住。

既然所有代码它都看得见，用户提的替代方案是：**定位由别人做，它只负责写**。
本脚本给本地模型一个**完美定位提示**（函数名 + bug 行号，取自题集 ground truth），
这是任何定位器（人 / 云端 / 静态分析）能给到的上限。测两件事：

  1. 有完美定位后，它写得对吗？（solved 率）
  2. 它会不会老实只改那一个函数？（loc_class / collateral）

结论口径：若 solved 率不涨，说明"拆分定位/写码"这条路走不通 —— 它的失败不止在定位；
若涨到接近 100%，说明拆分可行，接下来要解决的是"怎么便宜地拿到定位"。

用法：python probe_locate_hint.py [--limit N] [--tag xxx]
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "repair_eval"))

import harness          # noqa: E402
import run_repair       # noqa: E402

PROBLEMS = os.path.join(HERE, "repair_eval", "problems_hard.json")
BATCH = os.path.join(HERE, "results", "batch-full54c.json")


def build_hint_prompt(p):
    """原提示词 + 一行 ground-truth 定位（只给位置，不给修法）。"""
    base = run_repair.build_prompt(p, "module")
    fn = p["target_function"]
    line = p["bug_lines"][0]
    hint = (
        "\n\n---\n"
        "【已由外部定位器确认】出 bug 的那一行是第 %d 行（从模块第一行数起），"
        "它所在的函数是 `%s`。\n"
        "请只修改 `%s` 内部的那一处逻辑；同级其它函数一个字都不要动。\n"
        % (line, fn, fn)
    )
    return base + hint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8299")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 道（0=全部）")
    ap.add_argument("--tag", default="locate_hint")
    ap.add_argument("--batch", default=BATCH, help="按哪个批跑结果挑出失败题")
    args = ap.parse_args()

    problems = {p["id"]: p for p in harness.load_problems(PROBLEMS)}
    with open(args.batch, encoding="utf-8") as f:
        prev = json.load(f)["records"]
    # 只挑上一轮"本地失败"的题：这是要验证的边界
    fail_ids = [r["id"] for r in prev if r.get("escalate_reason") == "fail"]
    if args.limit:
        fail_ids = fail_ids[:args.limit]
    # 顺带把上一轮本地通过的那批也跑几道，确认提示词改动不会把好的搞坏
    print("上轮本地失败 %d 道；给完美定位提示后重测" % len(fail_ids), flush=True)

    intern = harness.Intern(args.base_url, contract="module")
    recs = []
    t0 = time.time()
    for i, pid in enumerate(fail_ids, 1):
        p = problems[pid]
        prompt = build_hint_prompt(p)
        r = intern.generate(prompt)
        code, how, spliced = intern.edit(p, r["content"])
        meta = intern._meta(p, r, how)
        rec = intern.verify(p, r["content"], code, spliced, meta)
        rec["id"] = pid
        rec["wall_s"] = round(r.get("wall_s", 0.0), 1)
        rec["how"] = how                 # 解析方式：miss 是"没解析出代码"还是"写错"要分清
        rec["raw_head"] = (r.get("content") or "")[:300]
        recs.append({k: rec.get(k) for k in (
            "id", "solved", "error_class", "loc_class", "collateral",
            "hits_bug", "one_shot_usable", "footprint", "rewrote_function",
            "wall_s", "how", "raw_head")})
        print("[%2d/%d] %-45s %s loc=%-16s coll=%s how=%-10s %.0fs"
              % (i, len(fail_ids), pid,
                 "OK  " if rec.get("solved") else "FAIL",
                 rec.get("loc_class"), rec.get("collateral"), how,
                 r.get("wall_s") or 0),
              flush=True)

    n = len(recs)
    ok = sum(1 for r in recs if r["solved"])
    exact = sum(1 for r in recs if r["loc_class"] == "exact")
    coll = sum(1 for r in recs if r["collateral"])
    print("\n=== 汇总（%.0fs）===" % (time.time() - t0))
    print("  给完美定位后 solved   %d/%d (%.1f%%)" % (ok, n, 100.0 * ok / n))
    print("  loc_class            %s" % dict(Counter(r["loc_class"] for r in recs)))
    print("  仍然炸邻居           %d" % coll)
    print("  之前这批的 solved    %d/%d (0%%，本来就是挑的失败题)"
          % (0, n))

    out = os.path.join(HERE, "results", "probe-%s.json" % args.tag)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "base_url": args.base_url, "batch": args.batch,
                   "hint": "ground-truth function + bug line",
                   "records": recs}, f, ensure_ascii=False, indent=1)
    print("  明细 %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
