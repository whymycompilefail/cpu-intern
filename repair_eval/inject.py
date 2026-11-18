"""从 seeds.py 生成「修 bug 题集」，并**机械验证**每道题的真值。

它做五件事：
  1) 对每个 seed 的 correct 源码跑全部测试 —— 必须全过，否则直接抛错（防止我手写测试写错）。
  2) 用 (old, new) 精确替换生成变异体，要求**恰好命中 1 次**（防止改错位置）。
  3) 跑测试算出 F2P（变异体下失败）/ P2P（变异体下仍通过）——**这是真值，不是我标的**。
     保留条件：F2P >= 1（确实被杀死）且 P2P >= 2（有惰性测试，否则「把函数删空」也能过）。
  4) **主测组 variant=mixed（所有难度档）**：混入 3 个干扰函数，且目标函数的位置
     按题号打散 —— 定位成本 > 0，同时把「bug 永远在最后一个函数」这条免费线索消掉。
  5) **控制组 variant=solo（level=0）**：模块里只有目标函数，定位成本为 0。
     它**不是难度档**，存在的唯一目的是把「定位」本身变成一个可观测量：
     mixed 与 solo 的差 = 定位带来的代价。评测器默认只跑 mixed。

用法：
    python repair_eval/inject.py            # 生成 problems.json
    python repair_eval/inject.py --check    # 只打印统计表，不写文件
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 题池生成器（seeds）不随仓库发布：它掺了出题偏好，发出去也没法当 benchmark。
# harness 只 import changed_lines / function_spans 这两个纯 AST 函数，不碰 SEEDS，
# 所以缺它照常工作；下面那些依赖 SEEDS 的组装函数在发布版里自然失效。
try:
    from seeds import SEEDS, DISTRACTOR_POOL  # noqa: E402
except ModuleNotFoundError:  # 发布版走这条
    SEEDS, DISTRACTOR_POOL = [], []

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "problems.json")

BY_ID = {s["id"]: s for s in SEEDS}


# --------------------------------------------------------------------- 执行

def _exec_module(src):
    ns = {}
    exec(compile(src, "<module>", "exec"), ns)
    return ns


def run_tests(module_src, tests):
    """返回 {测试名: bool}。模块本身加载失败 -> 全 False。"""
    try:
        base_ns = _exec_module(module_src)
    except Exception:
        return {name: False for name, _ in tests}
    results = {}
    for name, src in tests:
        prog = "def __t():\n" + textwrap.indent(src, "    ") + "\n__t()"
        ns = dict(base_ns)          # 每个测试独立命名空间，防止互相污染
        try:
            exec(compile(prog, "<test:%s>" % name, "exec"), ns)
            results[name] = True
        except Exception:
            results[name] = False
    return results


# --------------------------------------------------------------------- 定位
# 下面两个函数是「定位真值」的唯一来源，评测器直接 import 复用，避免两处实现漂移。

def function_spans(src):
    """顶层函数名 -> (起始行, 结束行)，1-based，按源码顺序（dict 保序）。

    只认**顶层** def：嵌套函数/类方法不会出现在这里，正好符合本题集形态。
    """
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node.lineno, node.end_lineno)
    return out


def changed_lines(buggy_src, other_src):
    """other 相对 buggy 被改动的行号（1-based，**以 buggy_src 为坐标基准**）。

    ⚠️ 坐标基准必须写清楚：bug_lines 与「候选答案改了哪几行」都用 buggy 侧坐标，
    否则定位精度会因为候选增删行导致的坐标漂移而算错。
    """
    a = buggy_src.splitlines()
    b = other_src.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:                      # replace / delete：buggy 侧真实被改的行
            for i in range(i1, i2):
                if a[i].strip():
                    out.add(i + 1)
        else:                            # insert：只记录插入点
            out.add(min(i1 + 1, max(len(a), 1)))
    return sorted(x for x in out if x >= 1)


def _seed_int(s):
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


def assemble(distractor_srcs, target_src, sid, mid):
    """把目标函数插到干扰函数**中间**，位置由题号确定（可复现）。

    ⚠️ 绝不能固定放末尾：那会让「bug 总在最后一个函数」变成免费线索。
    """
    blocks = [d.rstrip() for d in distractor_srcs]
    pos = _seed_int("%s|%s" % (sid, mid)) % (len(blocks) + 1)
    blocks.insert(pos, target_src.rstrip())
    return "\n\n\n".join(blocks) + "\n"


def apply_mutant(correct, old, new):
    n = correct.count(old)
    if n != 1:
        raise ValueError("替换目标命中 %d 次（应为 1 次）: %r" % (n, old[:60]))
    return correct.replace(old, new, 1)


def _fn_name(src):
    return src.split("(")[0].replace("def ", "").strip()


# --------------------------------------------------------------------- 生成

def build(dropped):
    problems = []

    for seed in SEEDS:
        sid = seed["id"]
        tests = seed["tests"]
        test_names = [t[0] for t in tests]

        # ---- 1) 强制验证 correct 全过
        res0 = run_tests(seed["correct"], tests)
        bad = [n for n in test_names if not res0[n]]
        if bad:
            raise AssertionError("seed %s 的 correct 源码没通过测试: %s" % (sid, bad))

        # ---- 2) 预生成干扰块（复合模块用；名字要加前缀，见下）
        # L1 极短题只用 2 个干扰函数，降低定位负担，给 1B 进 band 的机会；
        # 其余难度维持 3 个（保留「未定位」测量轴）。
        n_distract = 2 if seed["level"] == 1 else 3
        d_picks = [d for d in DISTRACTOR_POOL if d != sid][:n_distract]
        d_srcs = [BY_ID[d]["correct"] for d in d_picks]
        # ⚠️ 测试名必须加前缀：不同 seed 之间测试名会撞（都有 t_empty/t_exact），
        # 直接拼会让 dict 覆盖、把「失败」误判成「通过」——踩过一次，别去掉前缀。
        dtests = [("%s::%s" % (d, n_), s_)
                  for d in d_picks for n_, s_ in BY_ID[d]["tests"][:2]]

        # 目标函数的测试在复合体里同样加前缀，避免与干扰测试重名
        comp_tests = [("%s::%s" % (sid, n_), s_) for n_, s_ in tests] + dtests
        comp_names = [t[0] for t in comp_tests]
        comp_of = {"%s::%s" % (sid, n_): n_ for n_ in test_names}   # 复合名 -> solo 名

        for mid, mtype, old, new, desc in seed["mutants"]:
            try:
                buggy_fn = apply_mutant(seed["correct"], old, new)
            except ValueError as e:
                dropped.append((sid, mid, "替换失败: %s" % e))
                continue

            # 失败用例必须**按变异体**算：哪些测试挂取决于具体变异，
            # 按种子写死的 visible 会出现「展示的症状其实没在失败」——踩过，别退回去。
            res = run_tests(buggy_fn, tests)
            f2p = [n for n in test_names if not res[n]]
            p2p = [n for n in test_names if res[n]]
            vis_names = [n for n in seed["visible"] if n in f2p] or f2p[:1]

            if not f2p:
                dropped.append((sid, mid, "未被杀死 f2p=0"))
                continue
            if len(p2p) < 2:
                dropped.append((sid, mid, "p2p=%d(<2)，反硬编码没牙齿" % len(p2p)))
                continue

            tname = _fn_name(buggy_fn)

            # ---------- 控制组：只有目标函数（定位成本 = 0）
            problems.append(dict(
                id="%s__%s__solo" % (sid, mid),
                seed=sid, mutation=dict(mid=mid, mtype=mtype, desc=desc),
                level=0, axis="solo", variant="solo",
                module_source=buggy_fn, reference_source=seed["correct"],
                bug_lines=changed_lines(buggy_fn, seed["correct"]),
                target_function=tname,
                func_spans=function_spans(buggy_fn),
                target_span=list(function_spans(buggy_fn)[tname]),
                target_index=0, n_functions=1,
                spec=seed["spec"],
                visible=[(n_, s_) for n_, s_ in tests if n_ in set(vis_names)],
                f2p=[t for t in tests if t[0] in f2p],
                p2p=[t for t in tests if t[0] in p2p],
                n_f2p=len(f2p), n_p2p=len(p2p),
                single_f2p=(len(f2p) == 1),
            ))

            # ---------- 主测组：混入 3 个干扰函数，目标函数位置打散
            comp_src = assemble(d_srcs, buggy_fn, sid, mid)
            comp_ref = assemble(d_srcs, seed["correct"], sid, mid)
            res_ref = run_tests(comp_ref, comp_tests)
            bad_ref = [n for n in comp_names if not res_ref[n]]
            if bad_ref:
                dropped.append((sid, mid, "复合参考解未全过: %s" % bad_ref))
                continue
            res_c = run_tests(comp_src, comp_tests)
            cf2p = [n for n in comp_names if not res_c[n]]
            cp2p = [n for n in comp_names if res_c[n]]
            # 一致性：solo 里的失败，在复合体里也必须失败（复合名加了前缀）
            if not {"%s::%s" % (sid, n) for n in f2p} <= set(cf2p):
                dropped.append((sid, mid, "复合体 F2P 与 solo 不一致"))
                continue
            # 干扰函数的测试必须全在 P2P —— 否则说明我拼错了文件
            bad_d = [n for n, _ in dtests if n not in cp2p]
            if bad_d:
                dropped.append((sid, mid, "干扰测试未通过: %s" % bad_d))
                continue
            if len(cf2p) < 2:
                dropped.append((sid, mid, "复合体 f2p=%d(<2)" % len(cf2p)))
                continue
            if len(cp2p) < 2:
                dropped.append((sid, mid, "复合体 p2p=%d(<2)" % len(cp2p)))
                continue

            spans = function_spans(comp_src)
            problems.append(dict(
                id="%s__%s__mixed" % (sid, mid),
                seed=sid, mutation=dict(mid=mid, mtype=mtype, desc=desc),
                level=seed["level"], axis="distractor" if seed["level"] < 3 else "helper_chain",
                variant="mixed",
                module_source=comp_src, reference_source=comp_ref,
                bug_lines=changed_lines(comp_src, comp_ref),
                target_function=tname,
                func_spans={k: list(v) for k, v in spans.items()},
                target_span=list(spans[tname]),
                target_index=list(spans).index(tname), n_functions=len(spans),
                spec=seed["spec"] + "（注意：这个模块里还有别的函数，别改坏它们。）",
                visible=[(n_, s_) for n_, s_ in comp_tests
                         if comp_of.get(n_) in set(vis_names)],
                f2p=[t for t in comp_tests if t[0] in cf2p],
                p2p=[t for t in comp_tests if t[0] in cp2p],
                n_f2p=len(cf2p), n_p2p=len(cp2p),
                single_f2p=(len(cf2p) == 1),
            ))

    return problems


# --------------------------------------------------------------------- 报告

def report(problems, dropped):
    from collections import Counter
    mixed = [p for p in problems if p["variant"] == "mixed"]
    solo = [p for p in problems if p["variant"] == "solo"]

    print("=" * 78)
    print("生成题集：%d 道 = 主测 mixed %d 道 + 控制 solo %d 道（丢弃 %d 个变异体）"
          % (len(problems), len(mixed), len(solo), len(dropped)))
    print("=" * 78)

    print("\n--- 主测组按难度档（迁移后的 level 只反映 bug 本身复杂度，干扰恒定） ---")
    for lv in sorted({p["level"] for p in mixed}):
        g = [p for p in mixed if p["level"] == lv]
        print("  L%d : %3d 道   axis=%s  函数数=%s"
              % (lv, len(g), sorted({p["axis"] for p in g}),
                 sorted({p["n_functions"] for p in g})))

    print("\n--- 目标函数在模块中的位置分布（必须分散；固定末尾=免费线索） ---")
    for k, v in sorted(Counter(p["target_index"] for p in mixed).items()):
        print("  第 %d 个函数: %2d 道" % (k + 1, v))

    print("\n--- 按种子（mixed/solo） ---")
    cm = Counter(p["seed"] for p in mixed)
    cs = Counter(p["seed"] for p in solo)
    for s in SEEDS:
        print("  %-24s L%d  mixed %2d  solo %2d" % (s["id"], s["level"], cm[s["id"]], cs[s["id"]]))

    print("\n--- 按变异类型（= 诊断题真值） ---")
    for k, v in Counter(p["mutation"]["mtype"] for p in mixed).most_common():
        print("  %-14s %3d" % (k, v))

    print("\n--- 主测组 P2P 数量分布（含干扰函数测试；越多越能抓「撞坏邻居」） ---")
    for k, v in sorted(Counter(len(p["p2p"]) for p in mixed).items()):
        print("  p2p=%-3d %3d 道" % (k, v))

    print("\n--- 主测组 F2P 数量分布（=1 的题只靠 P2P 挡硬编码） ---")
    for k, v in sorted(Counter(len(p["f2p"]) for p in mixed).items()):
        print("  f2p=%-3d %3d 道" % (k, v))
    print("  f2p==1 的题: %d 道" % sum(1 for p in mixed if p["single_f2p"]))

    print("\n--- 丢弃的变异体（存活/无惰性测试/替换失败） ---")
    for sid, mid, why in dropped:
        print("  %-24s %-18s %s" % (sid, mid, why))
    if not dropped:
        print("  （无）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只打印统计，不写文件")
    a = ap.parse_args()

    dropped = []
    problems = build(dropped)
    report(problems, dropped)

    if not a.check:
        payload = {"n": len(problems), "problems": problems}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("\n已写出 %s （%.1f KB）" % (OUT, os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
