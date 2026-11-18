#!/usr/bin/env python3
"""两个 batch-*.json 的逐题配对 + McNemar 精确检验。

用法：
    python mcnemar_batch.py A.json B.json [--label-a 3B] [--label-b 7B]

为什么单独写一个：`grids/compare.py` 吃的是四格的 jsonl，而 harness 产出的是
batch-*.json（顶层 records，判定在 records[i].local.solved）。两者结构不同，
混用会读出全 None 还以为是模型全错（踩过）。

McNemar 用精确二项检验，不用卡方近似：本项目的题量小（54），
且翻转数经常 < 25，卡方近似在这种规模下不可靠。
"""
import argparse
import json
import math
import sys
from collections import Counter


def load_solved(path):
    """→ {id: bool}。优先 local.solved（首写结果），退化到 final_solved。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    recs = d.get("records") or []
    out = {}
    for r in recs:
        local = r.get("local") or {}
        ok = local.get("solved")
        if ok is None:
            ok = r.get("final_solved")
        out[r["id"]] = bool(ok)
    return out, d.get("meta") or {}


def mcnemar_exact(b, c):
    """双侧精确 McNemar：2 * P(X <= min(b,c))，X ~ Binomial(b+c, 0.5)。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    args = ap.parse_args()

    A, ma = load_solved(args.a)
    B, mb = load_solved(args.b)
    common = sorted(set(A) & set(B))
    only_a = sorted(set(A) - set(B))
    only_b = sorted(set(B) - set(A))
    if only_a or only_b:
        print(f"⚠ 题集不完全重叠：仅 A 有 {len(only_a)} 道，仅 B 有 {len(only_b)} 道，"
              f"配对只用交集 {len(common)} 道")

    a_only = [i for i in common if A[i] and not B[i]]
    b_only = [i for i in common if B[i] and not A[i]]
    both = [i for i in common if A[i] and B[i]]
    neither = [i for i in common if not A[i] and not B[i]]

    pa = sum(A[i] for i in common) / len(common) * 100
    pb = sum(B[i] for i in common) / len(common) * 100
    p = mcnemar_exact(len(a_only), len(b_only))

    print(f"\n配对题数 N={len(common)}")
    print(f"  {args.label_a:>10}: {sum(A[i] for i in common)}/{len(common)} ({pa:.1f}%)")
    print(f"  {args.label_b:>10}: {sum(B[i] for i in common)}/{len(common)} ({pb:.1f}%)")
    print(f"  差          : {pb - pa:+.1f} pp")
    print(f"\n四格：都过 {len(both)} / 都不过 {len(neither)} / "
          f"{args.label_a}独过 {len(a_only)} / {args.label_b}独过 {len(b_only)}")
    print(f"McNemar 精确检验 p = {p:.4f}", end="  ")
    if p < 0.05:
        print(f"⇒ {args.label_b if len(b_only) > len(a_only) else args.label_a} 显著更强")
    else:
        print("⇒ 不显著（差异落在配对噪声内）")

    if b_only:
        print(f"\n{args.label_b} 独过（{args.label_a} 没做出来的）：")
        for i in b_only:
            print("   +", i)
    if a_only:
        print(f"\n{args.label_a} 独过（{args.label_b} 反而没做出来）：")
        for i in a_only:
            print("   -", i)


if __name__ == "__main__":
    sys.exit(main())
