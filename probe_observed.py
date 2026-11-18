"""实验二：瓶颈到底是「不会定位」还是「不会在脑子里跑代码」？

实验一（`probe_locate_hint.py`）已经排除了"定位"：给到 ground-truth 的函数名 + 行号，
18 道本地失败题只救回 6 道，其中 9 道把错误代码**原样抄回**（footprint=0）。

剩下的嫌疑是**模拟执行**：`top2([3,1,2])` 要得 `(3,2)`，必须在脑子里跑一遍循环。
那就把这一步也自动化掉 —— harness 本来就会跑测试，只要把「实际返回了什么」也算出来，
模型就只剩"比较两个值、找出产生它的那一行"，不需要模拟。

本脚本对比三档提示（同一批 18 道题，同一模型、temperature=0）：
  A 原样（= 复现批跑失败）           ← 参照，理论 0/18
  B + 完美定位（函数名 + 行号）       ← 实验一：6/18
  C + 完美定位 + 实测值/期望值        ← 本实验：?

用法：python probe_observed.py
"""
import argparse
import ast
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


def measure(module_src, visible):
    """跑一遍 buggy 模块，把「调用 → 实际返回 / 期望值」算出来。

    只处理 `assert <call> == <expected>` 这一形状（题集里绝大多数是），
    其它形状跳过 —— 宁可少给信息，也不要给错信息。
    """
    g = {}
    try:
        exec(compile(module_src, "<module>", "exec"), g)   # noqa: S102
    except Exception as e:                                  # noqa: BLE001
        return []
    rows = []
    for _name, body in visible:
        try:
            tree = ast.parse(body.strip(), mode="exec")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            t = node.test
            if not (isinstance(t, ast.Compare) and len(t.comparators) == 1
                    and isinstance(t.ops[0], ast.Eq)):
                continue
            left = ast.unparse(t.left)
            right_src = ast.unparse(t.comparators[0])
            try:
                actual = eval(left, g)                       # noqa: S307
                actual_s = repr(actual)
            except Exception as e:                           # noqa: BLE001
                actual_s = "抛了 %s: %s" % (type(e).__name__, e)
            try:
                want_s = repr(eval(right_src, g))             # noqa: S307
            except Exception:                                 # noqa: BLE001
                want_s = right_src
            rows.append((left, actual_s, want_s))
    return rows


def build_prompt(p, mode):
    if mode == "D":
        # 「缩小范围」的忠实版本：只给它被点名的那个函数，干扰函数全部拿掉，
        # 输出契约同时改成「只交这一个函数」（结构上不可能炸邻居）。
        only = run_repair.extract_function(p["module_source"], p["target_function"])
        if not only:
            return None
        return run_repair.PROMPT_FUNC.format(
            spec=p["spec"], module=only.rstrip(),
            visible=run_repair.render_visible(p["visible"]),
            target=p["target_function"], n=run_repair.N_MTYPES,
            slash=run_repair.MTYPES_SLASH)
    base = run_repair.build_prompt(p, "module")
    if mode == "A":
        return base
    fn = p["target_function"]
    line = p["bug_lines"][0]
    if mode == "B":
        return base + (
            "\n\n---\n【已由外部定位器确认】出 bug 的那一行是第 %d 行（从模块第一行数起），"
            "它所在的函数是 `%s`。\n请只修改 `%s` 内部的那一处逻辑；同级其它函数一个字都不要动。\n"
            % (line, fn, fn))
    # mode == "C"：定位 + 实测值
    rows = measure(p["module_source"], p["visible"])
    obs = ""
    if rows:
        obs = "\n下面是同一个断言**实际跑出来**的结果（不是让你去算，是已经跑过的）：\n"
        for call, got, want in rows:
            obs += "  · `%s` 实际返回 `%s`，但这个函数应该返回 `%s`。\n" % (call, got, want)
        obs += "请利用上面的实际值找到产生错误结果的那一行（第 %d 行附近）并修正。\n" % line
    return base + (
        "\n\n---\n【已由外部定位器确认】出 bug 的那一行是第 %d 行（从模块第一行数起），"
        "它所在的函数是 `%s`。\n请只修改 `%s` 内部的那一处逻辑；同级其它函数一个字都不要动。\n"
        % (line, fn, fn)) + obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8299")
    ap.add_argument("--modes", default="C", help="要跑哪几档，如 ABC")
    ap.add_argument("--tag", default="observed")
    args = ap.parse_args()

    problems = {p["id"]: p for p in harness.load_problems(PROBLEMS)}
    with open(BATCH, encoding="utf-8") as f:
        prev = json.load(f)["records"]
    fail_ids = [r["id"] for r in prev if r.get("escalate_reason") == "fail"]

    intern = harness.Intern(args.base_url, contract="module")
    all_recs = {}
    for mode in args.modes:
        recs = []
        print("\n######## 模式 %s ########" % mode, flush=True)
        for i, pid in enumerate(fail_ids, 1):
            p = problems[pid]
            prompt = build_prompt(p, mode)
            if prompt is None:
                print("[%2d/%d] %-45s SKIP（取不到 target 函数）"
                      % (i, len(fail_ids), pid), flush=True)
                continue
            contract = "func" if mode == "D" else "module"
            r = intern.generate(prompt)
            code, how, spliced = run_repair.prepare(p, r["content"], contract)
            meta = intern._meta(p, r, how)
            rec = intern.verify(p, r["content"], code, spliced, meta)
            recs.append({k: rec.get(k) for k in (
                "solved", "error_class", "loc_class", "collateral",
                "one_shot_usable", "footprint")} | {"id": pid})
            print("[%2d/%d] %-45s %s loc=%-16s foot=%s"
                  % (i, len(fail_ids), pid,
                     "OK  " if rec.get("solved") else "FAIL",
                     rec.get("loc_class"), rec.get("footprint")), flush=True)
        ok = sum(1 for x in recs if x["solved"])
        print("  -> 模式 %s：solved %d/%d (%.1f%%)  loc=%s"
              % (mode, ok, len(recs), 100.0 * ok / len(recs),
                 dict(Counter(x["loc_class"] for x in recs))), flush=True)
        all_recs[mode] = recs

    out = os.path.join(HERE, "results", "probe-%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "base_url": args.base_url, "modes": args.modes,
                   "records": all_recs}, f, ensure_ascii=False, indent=1)
    print("\n明细 %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
