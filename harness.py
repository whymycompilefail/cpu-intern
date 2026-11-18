#!/usr/bin/env python
"""实习生助手 —— 本地闭环 harness。

架构见 SPEC.md §3/§4：
- 实习生（本地小模型）= 无反思文本变换器 + 3 类 I/O 契约（生成 / 浏览 / 编辑）
- 验证 = harness 自动化（不是模型工具，模型只出文本、不碰文件系统 / 终端）
- 云端升级分支 = 本地验证失败 → 打包反馈 → CDP 发云端满血（escalate_to_cloud，默认关闭）

本文件**复用** repair_eval/run_repair.py 的纯函数
（chat / build_prompt / extract / prepare / grade_one / run_suite），不重写判分器。
修复任务的「验证」就是 F2P/P2P，已沉淀在 run_repair。

用法（大脑分配见 SPEC §4.1：命令 > 默认本地 > 失败升级）：
  # 先起本地 server（见 serve_mcp2b.sh 或手动起 llama-server.exe）
  # 默认：本地首写，失败时（--cloud 启用）自动升级云端
  python harness.py --base-url http://127.0.0.1:8299 \\
      --problems repair_eval/problems_hard.json --smoke --cloud
  # 命令式：直发云端，跳过本地
  python harness.py --base-url ... --problems ... --id <id> --brain cloud --cloud
  # 仅本地、永不云端：不加 --cloud
  python harness.py --base-url ... --problems ... --id <id>
本地具体模型由 --base-url 指向的 server 决定（coder/qwen35 = 不同 base-url）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPAIR_DIR = os.path.join(HERE, "repair_eval")
sys.path.insert(0, REPAIR_DIR)
import run_repair  # noqa: E402  # 复用纯函数（无副作用：仅定义 + import inject）

PROBLEMS_DEFAULT = os.path.join(REPAIR_DIR, "problems_hard.json")

# 云端专用提示词。本地提示词让模型「不要加 markdown 围栏」，因为本地聊天模板
# 是纯文本通道、裸文本原样返回。**网页版不能照抄**：网页会把回复按 markdown
# 渲染，未围栏的代码缩进会被折叠掉、`<<<LOCATION>>>` 的尖括号也会被 HTML
# 解析破坏。所以云端反过来要求「围栏代码块」，再由 CDP 从 <pre> 取原文。
CLOUD_PROMPT = """你是修 bug 的助手。下面这个 Python 模块里有**一个函数写错了**，请修好它。

## 该函数应有的行为
{spec}

## 模块源码（bug 可能在任意一个函数里，不要假设是最后一个）
```python
{module}
```

## 已确认失败的测试（修好后必须全部通过）
```python
{visible}
```

## 输出要求（严格遵守，违反则结果作废）
1. 第一行只写一行定位信息，格式固定为：
LOCATION: function=【有 bug 的函数名】 line=【行号数字】 type=【类别】
2. 空一行，然后输出**一个** ```python 围栏代码块，里面是修好后的**完整模块源码**：
   所有函数都要包含；除了出 bug 的那个函数，其它函数逐字保持原样。
3. 不要解释、不要总结、不要重复题目、不要输出第二个代码块。

类别只能从这 {n} 个里选一个：{csv}
"""

LOC_LINE_RE = re.compile(
    r"LOCATION:\s*function\s*=\s*(\S+?)\s+line\s*=\s*(\d+)\s+type\s*=\s*(\S+)",
    re.I)


def build_cloud_prompt(problem):
    return CLOUD_PROMPT.format(
        spec=problem["spec"],
        module=problem["module_source"].rstrip(),
        visible=run_repair.render_visible(problem["visible"]),
        n=run_repair.N_MTYPES, csv=run_repair.MTYPES_CSV)


def prepare_cloud(problem, fence_code, reply_raw):
    """把云端返回的围栏代码块变成候选源码。

    与本地 `prepare` 的区别：**不依赖 <<<FIXED>>> 哨兵**（网页渲染会破坏尖括号），
    直接把整块代码当「完整模块」。解析不出全部函数时，再降级为只替换目标函数。
    """
    code = (fence_code or "").strip("\n")
    if not code.strip():
        return "", "empty", False
    need = set(run_repair.function_spans(problem["module_source"])) \
        if run_repair._parses(problem["module_source"]) else set()
    have = set(run_repair.function_spans(code)) if run_repair._parses(code) else set()
    if need.issubset(have):
        return code, "cloud_fence", False
    spliced = run_repair.try_splice(problem["module_source"], code,
                                    problem["target_function"])
    if spliced is None:
        return code, "cloud_fence", False
    return spliced, "cloud_fence_spliced", True


def legacy_location_block(reply_raw):
    """把网页版的 `LOCATION: ...` 行转成判分器认识的哨兵块，复用它的解析。"""
    m = LOC_LINE_RE.search(reply_raw or "")
    if not m:
        return reply_raw or ""
    return ("<<<LOCATION>>>\nfunction: %s\nline: %s\ntype: %s\n<<<END>>>\n\n%s"
            % (m.group(1), m.group(2), m.group(3).strip(".,;"), reply_raw or ""))


class Intern:
    """本地闭环实习生。模型只出文本，所有执行（写文件 / 跑测试）在 harness 侧。"""

    def __init__(self, base_url, contract="module", max_tokens=1024,
                 timeout=600.0, regime="nothink", cloud_site="deepseek",
                 observe=False):
        self.base_url = base_url.rstrip("/")
        self.contract = contract
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.regime = regime  # 本地小模型一律 nothink（SPEC §2 已钉死）
        self.cloud_site = cloud_site  # sites.json 站点键（默认 deepseek，备选 kimi）
        # 追加「第 N 条断言失败，实际/期望」——v4 语料里的读报错形态。
        # "off"/"on"/"auto" 归一化成 False/True/"auto"（build_prompt 只认这三种）。
        self.observe = {"off": False, "on": True}.get(observe, observe)

    # ---- 3 类 I/O 契约（模型侧只出文本，harness 执行） ----

    def browse(self, path):
        """浏览文档契约：读磁盘文件全文当上下文。harness 读，模型不出手。"""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def generate(self, prompt):
        """生成文档契约（本地后端）：prompt → 本地小模型 → 原始回复文本。"""
        r = run_repair.chat(self.base_url, prompt, self.regime,
                            self.max_tokens, self.timeout)
        return r

    def edit(self, problem, content, contract=None):
        """编辑文档契约：解析模型输出里的 <<<FIXED>>> 块，拼接为候选源码。
        返回 (code, how, spliced)。模型不直接碰文件，harness 解析后落盘。"""
        return run_repair.prepare(problem, content, contract or self.contract)

    def verify(self, problem, cand_raw, cand_src, spliced, meta):
        """验证契约（harness 自动化，不是模型工具）：跑 F2P/P2P 判定候选源码。"""
        return run_repair.grade_one(problem, cand_raw, cand_src, spliced, meta)

    # ---- 编排：本地首写 → 编辑 → 验证 ----

    def run_local(self, problem):
        problem, contract = self._resolve(problem)
        prompt = run_repair.build_prompt(problem, contract, observed=self.observe)
        # 记下"这题到底加没加观察值"：auto 路由下逐题不同，不记就没法核对路由是否真生效。
        observed = "条断言失败" in prompt
        r = self.generate(prompt)
        code, how, spliced = self.edit(problem, r["content"], contract)
        meta = self._meta(problem, r, how)
        meta["contract"] = contract
        meta["observed_applied"] = observed
        rec = self.verify(problem, r["content"], code, spliced, meta)
        rec["contract_used"] = contract
        rec["observed_applied"] = observed
        rec["via_cloud"] = False
        rec["escalated"] = False
        rec["candidate"] = code[:4000]
        return rec, r, code, how

    def _resolve(self, problem):
        """契约选择。`auto` = 先用测试静态推出目标函数，推得出就发 func 契约。

        关键：推出来的名字**直接替换** target_function，不去和 ground-truth 比对
        ——真仓库里没有 ground-truth，比对就成了作弊。推错就让它在拼接时暴露。
        """
        if self.contract != "auto":
            return problem, self.contract
        name = run_repair.infer_target(problem)
        if not name:
            return problem, "module"
        p2 = dict(problem)
        p2["target_function"] = name
        p2["target_inferred"] = True
        return p2, "func"

    def _meta(self, problem, r, how):
        content = r.get("content") or ""
        return {
            "id": problem["id"], "seed": problem.get("seed"),
            "level": problem["level"], "variant": problem["variant"],
            "axis": problem.get("axis"), "mtype": problem["mutation"]["mtype"],
            "mdesc": problem["mutation"]["desc"],
            "format": how, "fenced": "```" in content,
            "empty_reply": not content.strip(),
            "latency_s": round(r.get("wall_s", 0.0), 1),
            "completion_tokens": r.get("completion_tokens", -1),
            "prompt_tokens": r.get("prompt_tokens", -1),
            "prompt_ms": r.get("prompt_ms"), "decode_ms": r.get("decode_ms"),
            "decode_tps": r.get("decode_tps"),
            "finish_reason": r.get("finish_reason"),
            "truncated": r.get("finish_reason") == "length",
            "raw": content[:6000],
        }

    # ---- 云端分支（复用 cdp_bridge.py；需 Edge CDP 在运行） ----

    def cloud_solve(self, problem, feedback=""):
        """把（可选带反馈的）原题发给云端满血大脑，取回修复并判分。

        - feedback 为空 → 直发云端（--brain cloud 路径，跳过本地）；
        - feedback 非空 → 本地失败后的升级（带报错 + 失败代码，治 `loc=miss`）。
        复用 cdp_bridge.py CLI（已测通）：写 prompt 文件 → 调 CDP → 读回复 → 解析 → 判分。
        """
        prompt = build_cloud_prompt(problem)
        full = prompt + feedback
        tmp = tempfile.mkdtemp(prefix="intern_cloud_")
        pf = os.path.join(tmp, "prompt.txt")
        of = os.path.join(tmp, "reply.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(full)
        cdp = os.path.join(REPAIR_DIR, "cdp_bridge.py")
        try:
            proc = subprocess.run(
                [sys.executable, cdp, "--site", self.cloud_site,
                 "--ask", "--text-file", pf, "--out", of,
                 "--new-chat", "--prefer", "pre"],
                timeout=600, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return None, "cdp_failed: %s" % e
        if proc.returncode != 0:
            # 别把 stderr 吞掉：桥接失败的原因（选择器/超时/站点）全在这里
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            return None, "cdp exit=%s: %s" % (proc.returncode, tail)
        if not os.path.exists(of):
            return None, "no reply file"
        reply = open(of, encoding="utf-8", errors="replace").read()
        # 留档：云端往返原文是排查云端失败的唯一证据（解析出错时尤其需要）
        dbg = os.path.join(REPAIR_DIR, "results")
        os.makedirs(dbg, exist_ok=True)
        with open(os.path.join(dbg, "cloud_last_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(full)
        with open(os.path.join(dbg, "cloud_last_reply.txt"), "w", encoding="utf-8") as f:
            f.write(reply)
        print("  [云端原文] %d chars -> repair_eval/results/cloud_last_reply.txt"
              % len(reply))
        print("  [云端开头] %s" % reply.strip()[:160].replace("\n", " | "))
        try:  # cdp_bridge --out 写的是原始回复文本；若写成 json 则取 content
            j = json.loads(reply)
            reply = j.get("content") or j.get("text") or reply
        except ValueError:
            pass
        # 云端回的是围栏代码块（--prefer pre 已只留下最后一个块），
        # 不靠哨兵解析；定位信息另用 LOCATION 行还原，喂给判分器的诊断格子。
        code, how, spliced = prepare_cloud(problem, reply, reply)
        raw_for_grade = legacy_location_block(reply)
        meta = self._meta(
            problem,
            {"content": reply, "wall_s": 0.0, "completion_tokens": -1,
             "prompt_tokens": -1, "prompt_ms": None, "decode_ms": None,
             "decode_tps": None, "finish_reason": None},
            how)
        rec = self.verify(problem, raw_for_grade, code, spliced, meta)
        rec["via_cloud"] = True          # 这份结果是云端产出的
        rec["escalated"] = bool(feedback)  # 是否是「本地失败后升级」来的
        rec["candidate"] = code[:4000]
        return rec, None

    def escalate_to_cloud(self, problem, local_rec):
        """升级到云端。

        ⚠️ 触发条件不只是"测试没过"。最危险的形态是**测试全绿但改了别的函数**
        （collateral），它会被"通过"掩盖掉，所以也必须升级重做。
        """
        solved = local_rec.get("solved")
        collateral = local_rec.get("collateral")
        rewrote = local_rec.get("rewrote_function")

        if solved and (collateral or rewrote):
            feedback = "\n\n---\n前面那次修改虽然让测试通过了，但改动范围不合格：\n"
            if collateral:
                feedback += ("你把 bug 函数之外的代码也改了（炸邻居），这在真实仓库里"
                             "是不可接受的——测试绿了不代表没改坏别处。\n")
            if rewrote:
                feedback += "你整函数重写了，没有做最小修复。\n"
            feedback += ("要求：只改真正有 bug 的那一个函数内部，同级其它函数一个字都不要动；"
                         "做最小改动。仍然先写 LOCATION 行，再输出一个 ```python 围栏代码块"
                         "（完整模块源码）。\n")
        else:
            feedback = "\n\n---\n前面那次修改没有通过测试，反馈如下：\n"
            feedback += "失败类别：%s\n" % (local_rec.get("error_class") or "unknown")
            if local_rec.get("load_error"):
                feedback += "加载/执行错误：%s\n" % local_rec["load_error"]
            feedback += "你上一次给出的代码（未通过）：\n```python\n%s\n```\n" % (
                (local_rec.get("candidate") or "")[:2000])
            feedback += "请根据上面的失败信息重新给出修复：仍然先写 LOCATION 行，" \
                        "再输出一个 ```python 围栏代码块（完整模块源码）。\n"
        return self.cloud_solve(problem, feedback)

    def run_cloud(self, problem):
        """直发云端路径（--brain cloud）：跳过本地，云端 one-shot。"""
        return self.cloud_solve(problem, feedback="")


def load_problems(path):
    if not os.path.exists(path):
        sys.exit(
            "找不到题池：%s\n"
            "题池不随仓库发布（它是内部量具，掺了出题偏好）。\n"
            "用 --problems 指向你自己的题文件，格式见 README「跑起来」一节：\n"
            '  顶层 {"problems": [...]}，每题给 module_source / target_function /\n'
            "  visible（失败测试）/ regression（不能回归的测试）等字段。\n" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)["problems"]


REC_FIELDS = ("solved", "error_class", "loc_class", "f2p_ok", "f2p_n",
              "p2p_ok", "p2p_n", "collateral", "hits_bug", "one_shot_usable",
              "rewrote_function", "observed_applied")


def slim(rec):
    """只留判定字段 —— 原始 rec 里还塞着 4000 字候选源码，不适合进统计。"""
    return {k: rec.get(k) for k in REC_FIELDS}


def solve_one(intern, p, cloud_enabled, brain="local"):
    """一道题的完整链路：本地首写 → 验证 → （失败且启用）升级云端 → 复验。

    云端出错**不抛出**：整批跑不能因为一次 CDP 超时就全废，错误记进
    `cloud_error` 后继续下一题。这正是"自动链是否可用"要量化的东西。
    brain="cloud" 时跳过本地，直接云端 one-shot（命令式覆盖，SPEC §4.1）。
    """
    t0 = time.time()
    rec = {
        "id": p["id"], "level": p["level"], "variant": p["variant"],
        "mtype": p["mutation"]["mtype"], "mdesc": p["mutation"]["desc"],
        "escalated": False, "cloud": None, "cloud_error": None,
    }

    if brain == "cloud":
        if not cloud_enabled:
            raise RuntimeError("brain=cloud 需要启用云端")
        rec["local"] = {"solved": None, "error_class": "skipped", "loc_class": "-"}
        crec, err = intern.run_cloud(p)
        final, src = None, "none"
        if err:
            rec["cloud_error"] = err
            print("      云端失败：%s" % (err or "")[:200], flush=True)
        elif crec is not None:
            rec["cloud"] = slim(crec)
            final, src = crec, "cloud"
            print("      云端: %s class=%s loc=%s"
                  % ("✅" if crec.get("solved") else "❌", crec.get("error_class"),
                     crec.get("loc_class")), flush=True)
        rec["final_solved"] = bool(final and final.get("solved"))
        rec["final_source"] = src if rec["final_solved"] else "none"
        rec["wall_total_s"] = round(time.time() - t0, 1)
        return rec

    lrec, r, code, how = intern.run_local(p)
    rec["local"] = slim(lrec)
    rec["local"]["wall_s"] = round(r.get("wall_s", 0.0), 1)
    rec["local"]["decode_tps"] = r.get("decode_tps")
    rec["local"]["truncated"] = r.get("finish_reason") == "length"
    rec["local"]["how"] = how
    print("      本地: %s class=%s loc=%s f2p=%s/%s p2p=%s/%s %.0fs"
          % ("✅" if lrec.get("solved") else "❌", lrec.get("error_class"),
             lrec.get("loc_class"), lrec.get("f2p_ok"), lrec.get("f2p_n"),
             lrec.get("p2p_ok"), lrec.get("p2p_n"), r.get("wall_s") or 0.0), flush=True)
    if r.get("finish_reason") == "length":
        print("      ⚠️ 输出被 max_tokens 截断（可能假失败）", flush=True)
    # 升级触发条件：不只是"测试没过"。
    #   collateral      = 测试全绿但改了别的函数（最危险，会被"通过"掩盖）
    #   rewrote_function= 整函数重写，不是最小修复（评审成本高）
    reason = None
    if not lrec.get("solved"):
        reason = "fail"
    elif lrec.get("collateral"):
        reason = "collateral"
    elif lrec.get("rewrote_function"):
        reason = "rewrote"
    rec["escalate_reason"] = reason
    rec["escalated"] = bool(reason and cloud_enabled)

    final, src = lrec, "local"
    if rec["escalated"]:
        print("      -> 触发升级(%s: %s/%s)，送云端…"
              % (reason, lrec.get("error_class"), lrec.get("loc_class")), flush=True)
        crec, err = intern.escalate_to_cloud(p, lrec)
        if err:
            rec["cloud_error"] = err
            print("      -> 云端失败：%s" % (err or "")[:200], flush=True)
        elif crec is not None:
            rec["cloud"] = slim(crec)
            final, src = crec, "cloud"
            print("      云端: %s class=%s loc=%s f2p=%s/%s p2p=%s/%s"
                  % ("✅" if crec.get("solved") else "❌", crec.get("error_class"),
                     crec.get("loc_class"), crec.get("f2p_ok"), crec.get("f2p_n"),
                     crec.get("p2p_ok"), crec.get("p2p_n")), flush=True)

    rec["final_solved"] = bool(final.get("solved"))
    rec["final_source"] = src if rec["final_solved"] else "none"
    rec["wall_total_s"] = round(time.time() - t0, 1)
    return rec


def fmt_rec(rec):
    l = rec["local"]
    s = "local:%s(%s/%s)" % ("OK " if l["solved"] else "FAIL",
                             l.get("error_class"), l.get("loc_class"))
    if rec["escalated"]:
        c = rec["cloud"]
        if rec["cloud_error"]:
            s += " | cloud:ERR"
        elif c:
            s += " | cloud:%s(%s/%s)" % ("OK " if c["solved"] else "FAIL",
                                        c.get("error_class"), c.get("loc_class"))
        else:
            s += " | cloud:NONE"
    return s


def aggregate(recs):
    n = len(recs)
    local_ok = sum(1 for r in recs if r["local"].get("solved") is True)
    esc = [r for r in recs if r["escalated"]]
    esc_ok = sum(1 for r in esc if r["cloud"] and r["cloud"]["solved"])
    err = sum(1 for r in esc if r["cloud_error"])
    fin = sum(1 for r in recs if r["final_solved"])
    coll = sum(1 for r in recs
               if (r["cloud"] or {}).get("collateral")
               or (r["final_source"] == "local" and r["local"].get("collateral")))
    wall = sum(r["wall_total_s"] for r in recs)
    return {
        "n": n, "local_solved": local_ok,
        "local_rate": round(100.0 * local_ok / n, 1) if n else 0.0,
        "escalated": len(esc),
        "escalate_reasons": {
            k: sum(1 for r in esc if r.get("escalate_reason") == k)
            for k in ("fail", "collateral", "rewrote")},
        "cloud_ok": esc_ok,
        "cloud_rescue_rate": round(100.0 * esc_ok / len(esc), 1) if esc else None,
        "cloud_errors": err,
        "final_solved": fin,
        "final_rate": round(100.0 * fin / n, 1) if n else 0.0,
        "gain_pp": round(100.0 * (fin - local_ok) / n, 1) if n else 0.0,
        "collateral": coll,
        "wall_s": round(wall, 1),
        "wall_avg_s": round(wall / n, 1) if n else 0.0,
    }


def write_report(recs, tag, out_dir, args):
    os.makedirs(out_dir, exist_ok=True)
    agg = aggregate(recs)
    js = os.path.join(out_dir, "batch-%s.json" % tag)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"meta": {
            "tag": tag, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": args.base_url, "brain": args.brain,
            "cloud_site": args.cloud_site, "cloud_enabled": args.cloud or args.brain == "cloud",
            "contract": args.contract, "problems": args.problems,
            "local_model_note": "本地小模型 nothink（SPEC §2）",
        }, "aggregate": agg, "records": recs}, f, ensure_ascii=False, indent=1)

    md = os.path.join(out_dir, "batch-%s.md" % tag)
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 批量实测 — %s\n\n" % tag)
        f.write("- 时间：%s\n- 本地：`%s` · 云端站点：`%s`（%s）\n- 题数：%d\n\n"
                % (time.strftime("%Y-%m-%d %H:%M"),
                   args.base_url, args.cloud_site,
                   "启用" if (args.cloud or args.brain == "cloud") else "未启用", agg["n"]))
        f.write("| 指标 | 值 |\n|---|---|\n")
        f.write("| 本地通过 | %d/%d（%.1f%%） |\n"
                % (agg["local_solved"], agg["n"], agg["local_rate"]))
        f.write("| 升级云端 | %d |\n" % agg["escalated"])
        _rs = agg.get("escalate_reasons") or {}
        f.write("| ↳ 触发原因 | 测试失败 %d / 炸邻居 %d / 整函数重写 %d |\n"
                % (_rs.get("fail", 0), _rs.get("collateral", 0), _rs.get("rewrote", 0)))
        f.write("| 云端救回 | %s |\n"
                % ("—（无需升级）" if not agg["escalated"]
                   else "%d/%d（%.1f%%）" % (agg["cloud_ok"], agg["escalated"],
                                            agg["cloud_rescue_rate"])))
        f.write("| 云端调用失败 | %d |\n" % agg["cloud_errors"])
        f.write("| **最终通过** | **%d/%d（%.1f%%）** 比纯本地 +%.1fpp |\n"
                % (agg["final_solved"], agg["n"], agg["final_rate"], agg["gain_pp"]))
        f.write("| 炸邻居(最终) | %d |\n" % agg["collateral"])
        f.write("| 总墙钟 / 均摊 | %.0fs / %.0fs |\n\n" % (agg["wall_s"], agg["wall_avg_s"]))
        f.write("## 逐题\n\n| # | 题 | 本地 | 云端 | 最终 | 耗时 |\n|---|---|---|---|---|---|\n")
        for i, r in enumerate(recs, 1):
            l = r["local"]
            lc = "%s `%s/%s`" % ("✅" if l["solved"] else "❌",
                               l.get("error_class"), l.get("loc_class"))
            if not r["escalated"]:
                cc = "—"
            elif r["cloud_error"]:
                cc = "⚠️ 调用失败"
            else:
                c = r["cloud"] or {}
                cc = "%s `%s/%s`" % ("✅" if c.get("solved") else "❌",
                                   c.get("error_class"), c.get("loc_class"))
            f.write("| %d | `%s` | %s | %s | %s | %.0fs |\n"
                    % (i, r["id"], lc, cc,
                       "✅" if r["final_solved"] else "❌", r["wall_total_s"]))
        bad = [r for r in recs if r["cloud_error"]]
        if bad:
            f.write("\n## 云端调用失败明细\n\n")
            for r in bad:
                f.write("- `%s`: %s\n" % (r["id"], (r["cloud_error"] or "")[:300]))
    return agg, js, md


def pick_problem(problems, problem_id=None, smoke=False):
    if problem_id:
        for p in problems:
            if p["id"] == problem_id:
                return p
        raise SystemExit("no such problem id: %s" % problem_id)
    if smoke:
        mixed = [p for p in problems if p["variant"] == "mixed"]
        return mixed[0]
    raise SystemExit("need --id or --smoke")


def main():
    ap = argparse.ArgumentParser(description="实习生助手：本地小模型先写，失败的升级网页大模型")
    ap.add_argument("--base-url", default="http://127.0.0.1:8299",
                    help="本地 llama-server 地址")
    ap.add_argument("--problems", default=PROBLEMS_DEFAULT)
    ap.add_argument("--id", default="", help="指定题 id")
    ap.add_argument("--smoke", action="store_true", help="跑第 1 道 mixed")
    ap.add_argument("--contract", default="module", choices=["module", "func", "auto"],
                    help="auto = 从 f2p 测试静态推出目标函数（唯一命中才发 func 契约，否则回落 module）")
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--brain", default="local", choices=["local", "cloud"],
                    help="大脑分配：local=默认本地首写+失败自动升级云端；"
                         "cloud=直发云端跳过本地（命令式覆盖，SPEC §4.1）")
    ap.add_argument("--cloud", action="store_true",
                    help="启用云端路径（本地失败自动升级 / 或 --brain cloud 直发）。"
                         "需 Edge CDP 在运行；未启用时本地失败只报结果不升级")
    ap.add_argument("--cloud-site", default="deepseek",
                    help="云端大脑用哪个网页（sites.json 的键，默认 deepseek，备选 kimi）")
    ap.add_argument("--all", action="store_true",
                    help="跑整批题（配合 --limit 抽前 N 道），产出统计报告")
    ap.add_argument("--limit", type=int, default=0, help="--all 时只跑前 N 道（0=全部）")
    ap.add_argument("--tag", default="", help="报告文件名后缀（默认时间戳）")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "results"),
                    help="批跑报告输出目录")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="每题之间的间隔秒（给云端站点喘息，避免触发风控）")
    ap.add_argument("--observe", nargs="?", const="on", default="auto",
                    choices=["off", "on", "auto"],
                    help="func 契约下追加「第 N 条断言失败，实际得到 X，期望 Y」"
                         "（v4 语料 42%% 的 fix 样本带这句，属其训练过的读报错形态）。"
                         " 不写=auto（默认，按路由：模块内顶层函数 >1 才加）；"
                         " --observe 等价 on（无条件全加，实测 solo 组会掉分，仅作对照）；"
                         " --observe off 关闭。module 契约下此开关不生效")
    args = ap.parse_args()

    problems = load_problems(args.problems)
    cloud_enabled = args.cloud or args.brain == "cloud"

    intern = Intern(args.base_url, contract=args.contract,
                    max_tokens=args.max_tokens, timeout=args.timeout,
                    cloud_site=args.cloud_site, observe=args.observe)

    # ---------------- 批量模式 ----------------
    if args.all:
        todo = problems[:args.limit] if args.limit > 0 else problems
        tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(args.out_dir, exist_ok=True)
        jsonl = os.path.join(args.out_dir, "batch-%s.jsonl" % tag)
        print("=== 批跑 %d 道 | 本地=%s | 云端=%s(%s) | brain=%s ==="
              % (len(todo), args.base_url, args.cloud_site,
                 "启用" if cloud_enabled else "未启用", args.brain), flush=True)
        recs = []
        t_all = time.time()
        with open(jsonl, "w", encoding="utf-8") as fl:
            for i, p in enumerate(todo, 1):
                print("[%2d/%d] %s" % (i, len(todo), p["id"]), flush=True)
                try:
                    rec = solve_one(intern, p, cloud_enabled, args.brain)
                except Exception as e:  # noqa: BLE001  单题异常不拖垮整批
                    rec = {"id": p["id"], "level": p.get("level"),
                           "variant": p.get("variant"), "error": repr(e),
                           "local": {"solved": False, "error_class": "harness_error",
                                     "loc_class": "miss", "collateral": False},
                           "escalated": False, "cloud": None,
                           "cloud_error": None, "final_solved": False,
                           "final_source": "none", "wall_total_s": 0.0}
                recs.append(rec)
                fl.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fl.flush()  # 崩溃也能保住已完成的部分
                print("      %s  最终 %s  %.0fs"
                      % (fmt_rec(rec), "✅" if rec["final_solved"] else "❌",
                         rec["wall_total_s"]), flush=True)
                if args.pause and i < len(todo):
                    time.sleep(args.pause)

        agg, js, md = write_report(recs, tag, args.out_dir, args)
        print("\n=== 汇总（%.0fs）===" % (time.time() - t_all))
        print("  本地通过   %d/%d (%.1f%%)"
              % (agg["local_solved"], agg["n"], agg["local_rate"]))
        print("  升级云端   %d 次，救回 %s，调用失败 %d"
              % (agg["escalated"],
                 ("%d/%d" % (agg["cloud_ok"], agg["escalated"])) if agg["escalated"] else "—",
                 agg["cloud_errors"]))
        print("  最终通过   %d/%d (%.1f%%)  比纯本地 +%.1fpp"
              % (agg["final_solved"], agg["n"], agg["final_rate"], agg["gain_pp"]))
        print("  炸邻居     %d" % agg["collateral"])
        print("  报告       %s" % js)
        print("             %s" % md)
        return 0

    # ---------------- 单题模式 ----------------
    p = pick_problem(problems, args.id or None, args.smoke)
    rec = solve_one(intern, p, cloud_enabled, args.brain)
    print("\n=== 最终 ===")
    print("  id=%s solved=%s via_cloud=%s escalated=%s one_shot_usable=%s "
          "collateral=%s brain=%s"
          % (p["id"], rec.get("final_solved"), rec.get("final_source") == "cloud",
             rec.get("escalated"),
             (rec.get("cloud") or rec.get("local") or {}).get("one_shot_usable"),
             (rec.get("cloud") or rec.get("local") or {}).get("collateral"),
             args.brain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
