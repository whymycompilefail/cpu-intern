"""修 bug 题集的评测器 —— 喂题 → 取输出 → 机械判分 → 四轴报告。

被评的是**模型**，判分的是**机器**：全程只跑测试、比 AST、算行号，没有任何人工品味。

## 判分轴（分开报，不合成一个总分）

| 轴 | 含义 | 反作弊作用 |
|---|---|---|
| `syntax_ok` / `error_class` | 语法/加载/崩溃/结果错/超时 | 把「响亮失败」和「静默失败」分开 |
| `f2p` | 原本失败的测试转绿 | 能力 |
| `p2p` | 原本通过的测试没破 | **反装懂**（含干扰函数 ⇒ 反「整个文件重写」） |
| `loc_class` | 改动落在哪个函数：exact / in_func_not_bug / collateral / outside_only / miss | 定位能力 + 爆炸半径 |
| `footprint` / `rewrote_function` | 改了几行 / 是否整函数重写 | 直接映射我的评审时间 |
| `diag_*` | 自报的 bug 类别/函数/行号 vs 构造真值 | 诊断的 2×2 |
| `sim_ast` / `sim_tok` | 与参考解的结构相似度（**已排除注释**） | 只作诊断，不进结论 |

## 输入契约

只喂 `spec + module_source + visible`（1 条失败测试作为症状）。
**不给文件工具、不给参考解、不给全部失败用例** ⇒ 模型无法偷看答案。
判分用的是藏起来的 `f2p`（含隐藏用例）+ `p2p`。

## 用法

    python repair_eval/run_repair.py --base-url http://127.0.0.1:8299 --tag official-f16
    python repair_eval/run_repair.py --base-url ... --tag x --smoke
    python repair_eval/run_repair.py --base-url ... --tag x --variants all --levels 1,2
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import tokenize
import urllib.error
import urllib.request
from collections import Counter
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from inject import changed_lines, function_spans  # noqa: E402

RESULTS = os.path.join(HERE, "results")

# 后 4 类是难题集（seeds_hard.py）引进的：它们描述的是「修复所需的认知操作」，
# 不是「被改坏的语法位置」。⚠️ 闭集必须覆盖题集实际出现的所有 mtype，否则
# 那批题的「诊断正确率」恒为 0——等于先判死刑。列表由题集驱动，别再手写死。
MTYPES = ["boundary", "arith", "relational", "logic", "cond_swap", "init",
          "index", "off_by_one", "drop_guard", "api_misuse", "return_var",
          "contract", "state_trace", "control_flow", "python_semantics"]

N_MTYPES = len(MTYPES)
MTYPES_CSV = ", ".join(MTYPES)
MTYPES_SLASH = " / ".join(MTYPES)


# ===================================================================== 提示词
#
# 🔴 两个契约，这是**消融实验**，不是备份方案：
#   `module` —— 要求输出「修好后的完整模块」。好处：能测「有没有炸邻居」。
#               实测坏处：1B 把它当成**抄写任务**，67% 的题把 buggy 函数原样抄回来
#               （`sim_buggy_ast=1.0`、footprint=0）——它根本没尝试改。
#   `func`   —— 只要求输出「修好后的那一个函数」，且**去掉自报定位块**
#               （定位本来就由 diff 机械算出，不需要它自报）。
#               输出短、无抄写负担，形态最接近 `code_eval`（同模型那儿 88% 从零写对）。
#   判定用哪个当主口径：先看 `func` 的通过率有没有跳出地板。
#   代价：`func` 契约下「炸邻居」在结构上不可能发生（collateral 恒 0），安全轴失效。

PROMPT_MODULE = """你是修 bug 的工具。下面这个 Python 模块里**有一个函数写错了**，请修好它。

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

## 输出要求
只输出下面两段。不要解释、不要总结、不要重复题目。

第一段，定位：
<<<LOCATION>>>
function: 有 bug 的函数名
line: 出错那一行在模块源码里的行号，只写数字
type: 只写一个，从这 {n} 个里选：{csv}

第二段，修好后的**完整模块源码**：
<<<FIXED>>>
# 这行注释下面写修好后的完整模块源码，其它函数保持原样，不要加 markdown 围栏
<<<END>>>
"""

PROMPT_FUNC = """{spec}

```python
{module}
```

上面这个模块的 `{target}` 函数写错了。失败的测试：
```python
{visible}
```

修好 `{target}`。输出规则（严格遵守）：
1. 只输出 `{target}` 这一个函数，从 `def {target}(` 开始，到函数体结束。
2. 不输出其它函数，不解释，不加 markdown 围栏。
3. 第一行是注释 `# BUG_TYPE: <类别>`，第二行起是代码。

类别从这 {n} 个里选一个：{slash}
"""


def render_visible(vis):
    """把「症状」渲染成可直接读的测试函数（名字去掉复合前缀）。"""
    lines = []
    for name, body in vis:
        lines.append("def %s():" % name.split("::")[-1])
        lines.append(textwrap.indent(body.strip(), "    "))
    return "\n".join(lines)


def infer_target(p):
    """从 F2P 测试静态推出该修哪个函数：被调用 ∩ 模块内定义 − 测试内定义。

    2026-11-03 实测：problems_hard.json 54 题**全部唯一命中**（无多解/空/错）。
    现实依据：真实仓库里失败测试的调用栈本来就点明了函数，不需要模型猜。
    不唯一时返回 None，交回模型自己定位（宁可不给，也不给错）。
    """
    try:
        tree = ast.parse(p["module_source"])
    except SyntaxError:
        return None
    mod_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    calls, test_defs = set(), set()
    for _kind, src in p.get("f2p") or []:
        try:
            tt = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tt):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                calls.add(n.func.id)
            elif isinstance(n, ast.FunctionDef):
                test_defs.add(n.name)
    cand = (calls & mod_funcs) - test_defs
    return next(iter(cand)) if len(cand) == 1 else None


_MISSING = object()


def measure_observed(p, limit=1):
    """跑一遍 buggy 模块，算出「第几条断言、实际得到什么、期望什么」。

    只处理**单运算符比较**断言（`==` / `is` / `is not`），其它形状跳过 ——
    宁可少给信息，也不要给错信息（`<` / `in` 的"期望"不是单值，这句话表述不清）。
    只返回前 `limit` 条**不成立**的断言，因为训练语料里这句话就是单数一条
    （多写会和训练分布错开）。

    返回 [(序号, 表达式, 实际, 期望), ...]

    🔴 两个曾经漏掉的形状（2026-11-18 修，都在 `observed_applied` 上暴露成
    "路由说该加、实际没加上"）：

    1. **`is` / `is not` 断言**（`in_window__closed_interval__mixed`）：
       旧版只认 `ast.Eq`，`assert in_window(10, 0, 10) is False` 直接被跳过。
    2. **测试函数内的非断言语句必须照样执行**（`add_tag__mutable_default__mixed`）：
       旧版只对断言求值、跳过其余语句，于是 `t_first` 里 `add_tag('a')`
       制造的 mutable-default 污染丢失，`t_again` 的 `add_tag('b')` 单独求值
       返回 `['b']`、看起来"断言成立"，观察值算不出来。
       而真实失败恰恰来自那个跨测试的状态。
    """
    g = {}
    try:
        exec(compile(p["module_source"], "<module>", "exec"), g)  # noqa: S102
    except Exception:  # noqa: BLE001
        return []
    out = []
    for _name, body in p.get("visible") or []:
        try:
            tree = ast.parse(textwrap.dedent(body.strip()))
        except SyntaxError:
            continue
        idx = 0
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assert):
                # 保留测试内部的副作用，否则跨测试的状态依赖会丢（见上面第 2 条）
                try:
                    exec(compile(ast.Module(body=[stmt], type_ignores=[]),
                                 "<visible>", "exec"), g)  # noqa: S102
                except Exception:  # noqa: BLE001
                    pass
                continue
            idx += 1
            t = stmt.test
            if not (isinstance(t, ast.Compare) and len(t.ops) == 1
                    and len(t.comparators) == 1):
                continue
            op = t.ops[0]
            if not isinstance(op, (ast.Eq, ast.Is, ast.IsNot)):
                continue
            left = ast.unparse(t.left)
            right_src = ast.unparse(t.comparators[0])
            try:
                lv = eval(left, g)  # noqa: S307
                got_s = repr(lv)
            except Exception as e:  # noqa: BLE001
                lv, got_s = _MISSING, "抛了 %s: %s" % (type(e).__name__, e)
            try:
                rv = eval(right_src, g)  # noqa: S307
                want_s = repr(rv)
            except Exception:  # noqa: BLE001
                rv, want_s = _MISSING, right_src
            if lv is _MISSING or rv is _MISSING:
                same = False
            elif isinstance(op, ast.Eq):
                try:
                    same = bool(lv == rv)
                except Exception:  # noqa: BLE001
                    same = False
            elif isinstance(op, ast.Is):
                same = lv is rv
            else:
                same = lv is not rv
            if not same:
                out.append((idx, left, got_s, want_s))
                if len(out) >= limit:
                    return out
    return out


def n_top_functions(module_src):
    """模块顶层定义的函数个数。解析失败按 1 处理（当作 solo，宁可不加观察值）。"""
    try:
        tree = ast.parse(module_src)
    except SyntaxError:
        return 1
    return sum(1 for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


def should_observe(p):
    """func 契约下要不要追加「第 N 条断言失败，实际/期望」。

    2026-11-18 实测（54 题，mcp2b_v4，func 契约，temp=0）：

      mixed（模块内 4~5 个函数）17 → 20（+3）
        模型在这一档的失败形态是**原样抄回错误代码**（footprint=0，loc=miss）——
        它看得到代码，但看不出这行错在哪。给「实际 3、期望 1」之后才把 `<=` 改成 `<`。
        关键：这条信息**无法静态推导**，必须跑一遍才知道，所以它是新增信息。

      solo（模块内只有 1 个函数）21 → 19（−2）
        bug 静态可见（切片方向、比较符、变量没更新），观察值的信息量趋近于零；
        副作用却是真的：模型被锚死在**那一条**断言上，为它做局部适配。
        实测 `rotate`：base 版修对切片并保留 `if k == 0` 守卫，obs 版把守卫**删了**，
        因为观察值只给了 k=1 这一例。

    ⇒ 判据 = 模块内顶层函数数 > 1（不依赖题目的 variant 标签：真实仓库没有这种标签）。
    """
    return n_top_functions(p["module_source"]) > 1


def build_prompt(p, contract="module", observed=False):
    """observed: False/True 强制两档；"auto" 走 should_observe 路由。"""
    if contract == "func":
        if observed == "auto":
            observed = should_observe(p)
        prompt = PROMPT_FUNC.format(spec=p["spec"], module=p["module_source"].rstrip(),
                                    visible=render_visible(p["visible"]),
                                    target=p["target_function"],
                                    n=N_MTYPES, slash=MTYPES_SLASH)
        if observed:
            rows = measure_observed(p)
            if rows:
                idx, _expr, got, want = rows[0]
                # 与语料 v4 的 fix 样本同形（42% 的样本带这一句）
                line = "第 %d 条断言失败，实际得到 `%s`，期望 `%s`。\n\n" % (idx, got, want)
                anchor = "修好 `%s`。" % p["target_function"]
                if anchor in prompt:
                    prompt = prompt.replace(anchor, line + anchor, 1)
                else:
                    prompt += "\n" + line
        return prompt
    return PROMPT_MODULE.format(spec=p["spec"], module=p["module_source"].rstrip(),
                                visible=render_visible(p["visible"]),
                                n=N_MTYPES, csv=MTYPES_CSV)


# ===================================================================== 取输出

def chat(base_url, prompt, regime, max_tokens, timeout):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if regime in ("think", "nothink"):
        body["chat_template_kwargs"] = {"enable_thinking": regime == "think"}

    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    wall = time.time() - t0

    ch = payload.get("choices", [{}])[0]
    msg = ch.get("message", {}) or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    # 有些构建把思维链放在独立字段；content 恒空的假阳性坑（Qwen3.5 踩过）必须先兜住
    if not content and reasoning:
        content = reasoning
    usage = payload.get("usage") or {}
    tim = payload.get("timings") or {}
    return {
        "content": content,
        "completion_tokens": usage.get("completion_tokens", -1),
        "prompt_tokens": usage.get("prompt_tokens", -1),
        "wall_s": wall,
        "prompt_ms": tim.get("prompt_ms"),
        "decode_ms": tim.get("predicted_ms"),
        "decode_tps": tim.get("predicted_per_second"),
        "finish_reason": ch.get("finish_reason"),
    }


# ===================================================================== 提取

def _decap(text):
    """清洗候选代码块。

    两个模型侧的坑（冒烟实测踩到，不是假想）：
      ① 1B 会在 <<<FIXED>>> 里**再包一层 markdown 围栏** —— 不剥就 load_error。
      ② 1B 会**照抄提示词里的 <...> 占位符** —— 只丢含非 ASCII 的整行 <...>，
         避免误删合法的比较表达式。
    """
    text = text.strip()
    text = re.sub(r"<<<END>>>\s*\Z", "", text).strip()

    # ① 围栏优先：块内还有围栏时取最长那段
    blocks = re.findall(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", text, re.S)
    if blocks:
        text = max(blocks, key=len).strip()
    else:
        text = re.sub(r"\A```[A-Za-z]*[ \t]*\r?\n", "", text)
        text = re.sub(r"\r?\n```\s*\Z", "", text)

    # ② 丢弃照抄的模板占位行（整行 <...> 且含中文）
    kept = [l for l in text.splitlines()
            if not (re.fullmatch(r"\s*<[^<>]*>\s*", l) and re.search(r"[^\x00-\x7f]", l))]
    text = "\n".join(kept).strip()

    # ③ LOCATION 残渣：只在整段以定位标记开头时才砍（避免误伤真代码）
    if text.startswith("<<<LOCATION>>>"):
        m = re.search(r"(?m)^\s*(?=def\s|import\s|from\s|class\s)", text)
        if m:
            text = text[m.start():]

    if re.search(r"^[ \t]+(def|import|from|class)\s", text, re.M) and \
       not re.search(r"^(def|import|from|class)\s", text, re.M):
        text = textwrap.dedent(text)
    return text.strip()


def extract(content):
    """返回 (候选模块源码, how)。how ∈ sentinel|fence|raw|empty。"""
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[-1]
    m = re.search(r"<<<FIXED>>>(.*?)(?:<<<END>>>|\Z)", content, re.S)
    if m and m.group(1).strip():
        return _decap(m.group(1)), "sentinel"
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, re.S)
    if blocks:
        return _decap(max(blocks, key=len)), "fence"
    if re.search(r"(?m)^\s*(def|import|from)\s", content):
        return _decap(content), "raw"
    return "", "empty"


def extract_function(src, name):
    """从任意文本里抠出顶层函数 name 的源码（AST 优先，正则兜底）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                seg = ast.get_source_segment(src, node)
                if seg:
                    return seg.strip()
    m = re.search(r"(?m)^def\s+%s\s*\(" % re.escape(name), src)
    if not m:
        return None
    rest = src[m.end():]
    nxt = re.search(r"(?m)^def\s+\w+\s*\(", rest)
    end = m.end() + (nxt.start() if nxt else len(rest))
    seg = src[m.start():end]
    # 在围栏/标记/占位符处截断（否则拼回去的模块会多出 ``` 而语法报错）
    return re.split(r"(?m)^\s*(?:```|<<<|<[^<>]*[^\x00-\x7f][^<>]*>\s*$)", seg)[0].rstrip()


def extract_plain(content):
    """只要代码、不要标记（`func` 契约用）：围栏优先，否则整段当代码。"""
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[-1]
    blocks = re.findall(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", content, re.S)
    if blocks:
        return _decap(max(blocks, key=len)), "fence"
    if re.search(r"(?m)^\s*(def|import|from)\s", content):
        return _decap(content), "raw"
    return "", "empty"


def splice_function(original_module, target, fn_src):
    try:
        s, e = function_spans(original_module)[target]
    except (SyntaxError, KeyError):
        return None
    lines = original_module.splitlines()
    return "\n".join(lines[:s - 1] + [fn_src.rstrip()] + lines[e:]) + "\n"


def try_splice(original_module, cand, target):
    """候选不是完整模块时，只把它里面的目标函数替换回原模块。

    ⚠️ 这一步会把「格式没听话」和「修错了」分开 —— 否则一个 1B 模型
    格式一崩就全判 0，测出来的就不是修 bug 能力了。返回 None 表示救不回来。
    """
    fn = extract_function(cand, target)
    if not fn:
        return None
    return splice_function(original_module, target, fn)


# ===================================================================== 判分

GRADER = r'''
import json, sys, textwrap
payload = json.loads(sys.stdin.read())
src, tests = payload["src"], payload["tests"]
out = {"load": None, "tests": {}}
ns = {}
try:
    exec(compile(src, "<cand>", "exec"), ns)
except BaseException as e:
    out["load"] = "%s: %s" % (type(e).__name__, e)
for name, body in tests:
    prog = "def __t():\n" + textwrap.indent(body, "    ") + "\n__t()"
    loc = dict(ns)
    try:
        exec(compile(prog, "<t>", "exec"), loc)
        out["tests"][name] = "PASS"
    except BaseException as e:
        out["tests"][name] = type(e).__name__
sys.stdout.write("<<<JSON>>>" + json.dumps(out))
'''

_GRADER_PATH = None


def _grader_path():
    global _GRADER_PATH
    if _GRADER_PATH is None:
        fd, path = tempfile.mkstemp(suffix="_repair_grader.py", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(GRADER)
        _GRADER_PATH = path
    return _GRADER_PATH


def run_suite(cand_src, tests, timeout=45.0, retries=1):
    """独立子进程跑整份测试套件，逐个测试捕获异常类型。超时全判 TIMEOUT。

    ⚠️ **超时要重试一次**：实测踩到过一次性进程启动超时（参考解被判 timeout，
    重跑就过）—— 那会把**正确的解静默判成失败**，是比崩溃更坏的 bug。
    真·死循环会再超时一次，所以重试不会掩盖真问题；重试过会在结果里标 `grade_retry`。
    返回 (结果 dict, 是否重试过)。
    """
    names = [n for n, _ in tests]
    tried = 0
    while True:
        try:
            p = subprocess.run(
                [sys.executable, _grader_path()],
                input=json.dumps({"src": cand_src, "tests": tests}),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
            out = p.stdout or ""
            if "<<<JSON>>>" not in out:
                return ({"load": "NO_OUTPUT", "tests": {n: "ERROR" for n in names}}, tried > 0)
            return json.loads(out.split("<<<JSON>>>", 1)[1]), tried > 0
        except subprocess.TimeoutExpired:
            if tried >= retries:
                return ({"load": "TIMEOUT", "tests": {n: "TIMEOUT" for n in names}}, tried > 0)
            tried += 1
        except Exception as e:                                # noqa: BLE001
            return ({"load": "HARNESS: %s" % e, "tests": {n: "ERROR" for n in names}}, tried > 0)


def classify_error(exc_map, load_err):
    """把结果压成单一类别 —— 响亮失败（崩/超时）与静默失败（跑通但结果错）必须分开。"""
    if load_err == "TIMEOUT" or any(v == "TIMEOUT" for v in exc_map.values()):
        return "timeout"
    if load_err:
        return "load_error"
    bad = {k: v for k, v in exc_map.items() if v != "PASS"}
    if not bad:
        return "ok"
    if any(v not in ("AssertionError",) for v in bad.values()):
        return "runtime_error"          # 响亮失败：抛异常/NameError/TypeError…
    return "wrong_result"               # 静默失败：跑得通，但结果不对


# ===================================================================== 定位

def locality(buggy, cand, bug_lines, spans, target):
    """改动落在哪里 —— 坐标统一用 **buggy 侧**，避免候选增删行导致漂移。"""
    touched = set(changed_lines(buggy, cand))
    tspan = spans.get(target)
    tlines = set(range(tspan[0], tspan[1] + 1)) if tspan else set()
    other_lines = set()
    for n, (s, e) in spans.items():
        if n != target:
            other_lines |= set(range(s, e + 1))
    all_span = tlines | other_lines
    # ⚠️ 炸邻居 = 动了「别的函数里、且不是 bug 行」的代码。
    # 不能简单用 touched & other_lines：有 2/54 道题的 bug_lines 落在辅助函数里
    # （如 normalize_scores 的 bug 在 _pct 内），正确修法必然改到非 target 函数，
    # 那样会把「照参考解修好」误判成炸邻居（实测踩过：云端给出与 reference_source
    # 逐字相同的修复，仍被判 collateral）。
    collateral = (touched & other_lines) - set(bug_lines)
    module_level = touched - all_span
    hits = bool(touched & set(bug_lines))

    if not touched:
        klass = "miss"
    elif collateral:
        klass = "collateral"                       # 动了别的函数：对我的仓库最危险
    elif hits:
        klass = "exact"                            # 命中 bug 行即精确（可能在辅助函数里）
    elif touched & tlines:
        klass = "in_func_not_bug"                  # 改对了函数，没改到点上
    elif module_level:
        klass = "outside_only"
    else:
        klass = "miss"

    rewrote = bool(tspan) and len(touched & tlines) >= max(3, 0.8 * len(tlines))
    return {
        "loc_class": klass,
        "hits_bug": hits,
        "touched_lines": len(touched),
        "footprint": len(touched),
        "rewrote_function": rewrote,
        "collateral": bool(collateral),
    }


# ===================================================================== 相似度

def norm_ast(src):
    try:
        return ast.dump(ast.parse(src))
    except SyntaxError:
        return None


def strip_comments(src):
    """tokenize 丢掉注释/缩进/换行 —— 「测相似度排除注释」要求的落地。"""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING):
                continue
            if tok.string:
                out.append(tok.string)
    except Exception:                                          # noqa: BLE001
        return src
    return " ".join(out)


def ratio(a, b):
    if a is None or b is None:
        return None
    return round(SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def similarity(cand_src, ref_src, buggy_src, target):
    """只比**目标函数**（整模块比会把干扰函数的相同部分算成高分，掩盖差异）。"""
    cf = extract_function(cand_src, target)
    rf = extract_function(ref_src, target)
    bf = extract_function(buggy_src, target)
    ca, ra, ba = norm_ast(cf or ""), norm_ast(rf or ""), norm_ast(bf or "")
    return {
        "sim_ast": ratio(ca, ra),
        "sim_tok": ratio(strip_comments(cf or ""), strip_comments(rf or "")),
        "sim_buggy_ast": ratio(ca, ba),
        "ast_identical": bool(ca and ra and ca == ra),
    }


# ===================================================================== 自报定位

LOC_RE = re.compile(r"<<<LOCATION>>>(.*?)(?=<<<FIXED>>>|\Z)", re.S)
LINE_RE = re.compile(r"(?im)^\s*(?:行号|line)\s*[:：]\s*(\d+)")
FUNC_RE = re.compile(r"(?im)^\s*(?:函数名|function|func)\s*[:：]\s*([A-Za-z_][\w]*)")
TYPE_RE = re.compile(r"(?im)^\s*(?:类别|type)\s*[:：]\s*[`\"']?([A-Za-z_]+)")
BT_RE = re.compile(r"(?i)BUG_TYPE\s*[:：]\s*[`\"']?([A-Za-z_]+)")


def parse_location(text):
    """自报定位。`func` 契约只有一行 `# BUG_TYPE:`；`module` 契约看 LOCATION 块。

    ⚠️ 不管它报不报，`loc_class` / `hits_bug` 都**不依赖**这里 —— 那两个是从
    diff 机械算出来的。这个函数只为「诊断 2×2」服务，缺失不算失败。
    """
    m = LOC_RE.search(text)
    blk = m.group(1) if m else ""
    f = FUNC_RE.search(blk)
    l = LINE_RE.search(blk)
    t = TYPE_RE.search(blk)
    bt = BT_RE.search(text)
    return {"diag_func": f.group(1) if f else None,
            "diag_line": int(l.group(1)) if l else None,
            "diag_type": ((bt or t).group(1).lower() if (bt or t) else None)}


# ===================================================================== 主流程

def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, (c - h) / d), 4), round(min(1.0, (c + h) / d), 4))


def grade_one(p, cand_raw, cand_src, spliced, meta):
    spans = {k: tuple(v) for k, v in p["func_spans"].items()}
    target = p["target_function"]

    rec = {}
    rec.update(meta)

    if not cand_src.strip():
        # 「回空」和「回了一段散文/解释但一行代码都没有」是两种不同行为，必须分开记：
        # 前者是没答，后者是**答错了模态**（1B 在 func 契约下就会这样）。
        cls = "no_code" if cand_raw.strip() else "empty"
        rec.update(syntax_ok=False, error_class=cls,
                   f2p_ok=0, f2p_n=len(p["f2p"]), p2p_ok=0, p2p_n=len(p["p2p"]),
                   solved=False, loc_class="miss", hits_bug=False, footprint=0,
                   rewrote_function=False, collateral=False, one_shot_usable=False,
                   sim_ast=None, sim_tok=None, sim_buggy_ast=None, ast_identical=False)
        rec.update(parse_location(cand_raw))
        rec["diag_correct"] = None
        return rec

    try:
        ast.parse(cand_src)
        syntax_ok = True
    except SyntaxError:
        syntax_ok = False
    rec["syntax_ok"] = syntax_ok

    res, retried = run_suite(cand_src, p["f2p"] + p["p2p"])
    exc_map = res.get("tests") or {}
    cls = classify_error(exc_map, res.get("load"))
    rec["error_class"] = cls
    rec["load_error"] = res.get("load")
    rec["grade_retry"] = retried

    f2p_ok = sum(1 for n, _ in p["f2p"] if exc_map.get(n) == "PASS")
    p2p_ok = sum(1 for n, _ in p["p2p"] if exc_map.get(n) == "PASS")
    rec.update(f2p_ok=f2p_ok, f2p_n=len(p["f2p"]), p2p_ok=p2p_ok, p2p_n=len(p["p2p"]))
    rec["solved"] = (f2p_ok == rec["f2p_n"] and p2p_ok == rec["p2p_n"])

    loc = locality(p["module_source"], cand_src, p["bug_lines"], spans, target)
    rec.update(loc)

    if syntax_ok:
        rec.update(similarity(cand_src, p["reference_source"], p["module_source"], target))
    else:
        rec.update(sim_ast=None, sim_tok=None, sim_buggy_ast=None, ast_identical=False)

    self_loc = parse_location(cand_raw)
    rec.update(self_loc)
    rec["diag_correct"] = (None if not self_loc["diag_type"] or self_loc["diag_type"] == "unknown"
                           else self_loc["diag_type"] == p["mutation"]["mtype"])
    rec["diag_func_ok"] = (None if not self_loc["diag_func"]
                           else self_loc["diag_func"] == target)
    if self_loc["diag_line"] is None:
        rec["diag_line_err"] = None
    else:
        rec["diag_line_err"] = min(abs(self_loc["diag_line"] - b) for b in p["bug_lines"])

    # 「一次性可用」= 连词，不是平均：整题过 ∧ 格式拿到 ∧ 没炸邻居 ∧ 没整函数重写
    rec["one_shot_usable"] = bool(rec["solved"] and meta["format"] != "empty"
                                  and not rec["collateral"] and not rec["rewrote_function"])
    rec["spliced"] = spliced
    return rec


_META_LINE = re.compile(
    r"^\s*(?:"
    r"#\s*(?:定位|修复|修好|改动|位置|修复后|定位结果|[Bb][Uu][Gg]\w*)"
    r"|(?:function|line|type|类别|函数名?|行号)\s*[:：]"
    r"|<<<[A-Z_]+>>>"
    r")"
)


def strip_meta_header(code):
    """剥掉模型塞进代码块里的「定位元数据」头。

    某些模型（实测 Qwen2.5-Coder-7B）会把本该在围栏外的定位段改写成
    `# 定位` + `function:/line:/type:` 并塞进同一个 ```python 块里。
    这样拼回模块后，模块顶层就多出 `function: count_runs` 这种非法语句，
    判分器只能报 load_error —— 而它其实定位对了（hits_bug=true），
    等于用工程 bug 冒充了能力缺陷。

    **只在剥完能够 ast.parse 时才采纳**，所以对合规输出零影响：
    合规代码要么本来就能解析（不剥），要么剥了反而不能解析（不剥）。
    """
    if not code.strip():
        return code
    lines = code.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or _META_LINE.match(lines[i])):
        i += 1
    # 要求至少剥掉 2 行：单行 `# bug fix` 之类的正常注释不该被动
    if i < 2 or i >= len(lines):
        return code
    cand = "\n".join(lines[i:])
    return cand if _parses(cand) else code


def prepare(p, content, contract="module"):
    """提取候选模块；不是完整模块时降级为「只把目标函数替换回去」。

    返回 (code, how, spliced)。spliced=True 说明模型只给了那一个函数
    —— 这会让「炸邻居」在结构上不可能发生，所以必须单独统计、看两臂是否有差异。
    """
    if contract == "func":
        text, how = extract_plain(content)
        if not text.strip():
            return "", how, False
        fn = extract_function(text, p["target_function"])
        if not fn:
            return "", how, False
        s2 = splice_function(p["module_source"], p["target_function"], fn)
        return (s2, how, True) if s2 else ("", how, False)

    code, how = extract(content)
    code = strip_meta_header(code)
    if not code.strip():
        return "", how, False
    need = set(function_spans(p["module_source"])) if _parses(p["module_source"]) else set()
    have = set(function_spans(code)) if _parses(code) else set()
    if need.issubset(have):
        return code, how, False
    s2 = try_splice(p["module_source"], code, p["target_function"])
    if s2 is None:
        return code, how, False
    return s2, how, True


def _parses(src):
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _replace_function(module_src, name, new_src):
    s, e = function_spans(module_src)[name]
    lines = module_src.splitlines()
    return "\n".join(lines[:s - 1] + [new_src.rstrip()] + lines[e:]) + "\n"


def selftest(problems_path=None):
    """离线验证**判分器本身**：不接模型，用已知答案喂进去看分类对不对。

    A) 参考解            -> 满分、无炸邻居、命中 bug 行
    B) 原 buggy 模块原样  -> 不通过、loc=miss、footprint=0
    C) 空字符串          -> error_class=empty
    D) 目标函数换成桩     -> 不通过
    E) 参考解 + 在干扰函数里插一行注释 -> 通过但 collateral=True（**证明爆炸半径检测有牙齿**，
       同时说明这是 p2p 抓不到的那类改动 —— 所以足迹/炸邻居必须单独当一轴）
    """
    if problems_path is None:
        problems_path = os.path.join(HERE, "problems.json")
    with open(problems_path, encoding="utf-8") as f:
        allp = json.load(f)["problems"]
    mixed = [p for p in allp if p["variant"] == "mixed"]
    META = {"format": "sentinel", "fenced": False, "empty_reply": False,
            "latency_s": 0.0, "completion_tokens": 0, "prompt_tokens": 0,
            "prompt_ms": None, "decode_ms": None, "decode_tps": None}

    def g(p, src):
        return grade_one(p, "", src, False, dict(META, id=p["id"], seed=p["seed"],
                                                 level=p["level"], variant="mixed",
                                                 axis=p["axis"],
                                                 mtype=p["mutation"]["mtype"],
                                                 mdesc=p["mutation"]["desc"]))

    fails = []
    rewrote_ref = []
    for p in mixed:
        tag = p["id"]

        a = g(p, p["reference_source"])
        if not (a["solved"] and a["hits_bug"] and not a["collateral"]):
            fails.append(("A 参考解", tag, a))
        if a["rewrote_function"]:
            rewrote_ref.append(tag)

        b = g(p, p["module_source"])
        if b["solved"] or b["loc_class"] != "miss" or b["footprint"] != 0:
            fails.append(("B 原样", tag, b))

        c = g(p, "")
        if c["error_class"] != "empty" or c["solved"]:
            fails.append(("C 空", tag, c))

        d = g(p, _replace_function(p["module_source"], p["target_function"],
                                   "def %s(*a, **k):\n    return None" % p["target_function"]))
        if d["solved"]:
            fails.append(("D 桩", tag, d))

        spans = function_spans(p["reference_source"])
        other = [n for n in spans if n != p["target_function"]]
        if other:
            s, _ = spans[other[0]]
            lines = p["reference_source"].splitlines()
            lines.insert(s, "    # 无关的一行")
            e_src = "\n".join(lines) + "\n"
            e = g(p, e_src)
            if not e["solved"] or not e["collateral"] or e["one_shot_usable"]:
                fails.append(("E 炸邻居", tag, e))

    print("=" * 78)
    print("判分器自测 —— %d 道 mixed 题 × 5 项" % len(mixed))
    print("=" * 78)
    if fails:
        print("❌ %d 项不符预期：" % len(fails))
        for kind, tag, rec in fails[:12]:
            print("   %-10s %-40s solved=%s loc=%s fp=%s cls=%s coll=%s"
                  % (kind, tag, rec.get("solved"), rec.get("loc_class"),
                     rec.get("footprint"), rec.get("error_class"), rec.get("collateral")))
    else:
        print("✅ 五项全部符合预期（A 满分 / B miss / C empty / D 不过 / E 炸邻居被抓）")
    print("\n参考解被判「整函数重写」的题：%d 道 %s"
          % (len(rewrote_ref), rewrote_ref[:6] if rewrote_ref else ""))
    print("  （>0 说明该题的 bug 本身覆盖了大半函数，那条题用 rewrote_function 当负面信号会误伤正解）")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="", help="llama-server 地址（--selftest 不需要）")
    ap.add_argument("--tag", default="untagged")
    ap.add_argument("--regime", default="nothink", choices=["nothink", "think", "default"])
    ap.add_argument("--variants", default="mixed", choices=["mixed", "solo", "all"])
    ap.add_argument("--levels", default="", help="逗号分隔，如 1,2；空=全部")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 道（冒烟用）")
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out-dir", default=RESULTS)
    ap.add_argument("--problems", default=os.path.join(HERE, "problems.json"),
                    help="题集路径（标定后可用筛选出的子集）")
    ap.add_argument("--contract", default="module", choices=["module", "func"],
                    help="输出契约：module=完整模块+定位块 / func=只输出目标函数")
    ap.add_argument("--smoke", action="store_true", help="只跑 1 道，打印 prompt/原始输出/逐项判分")
    ap.add_argument("--selftest", action="store_true", help="离线验判分器（不需要模型）")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.problems)

    with open(args.problems, encoding="utf-8") as f:
        allp = json.load(f)["problems"]

    if args.smoke:
        p = [x for x in allp if x["variant"] == "mixed"][0]
        pr = build_prompt(p, args.contract)
        print("=== prompt (%d chars, contract=%s) ===" % (len(pr), args.contract))
        print(pr)
        r = chat(args.base_url, pr, args.regime, args.max_tokens, args.timeout)
        print("\n=== meta ===")
        print("prompt_tokens=%s completion_tokens=%s wall=%.1fs decode_tps=%s finish=%s"
              % (r["prompt_tokens"], r["completion_tokens"], r["wall_s"],
                 r["decode_tps"], r["finish_reason"]))
        if r["finish_reason"] == "length":
            print("🔴 输出被 max_tokens 截断 —— 提上去再跑，否则全是假失败")
        print("\n=== content（前 1500 字）===")
        print(r["content"][:1500] if r["content"] else "(EMPTY!)")
        code, how, spliced = prepare(p, r["content"], args.contract)
        print("\n=== 提取后 (%s, spliced=%s, %d chars) ===" % (how, spliced, len(code)))
        print(code[:1200] if code else "(EMPTY!)")
        rec = grade_one(p, r["content"], code, spliced, {
            "id": p["id"], "seed": p["seed"], "level": p["level"],
            "variant": p["variant"], "axis": p["axis"],
            "mtype": p["mutation"]["mtype"], "mdesc": p["mutation"]["desc"],
            "format": how, "fenced": "```" in r["content"],
            "empty_reply": not r["content"].strip(),
            "latency_s": round(r["wall_s"], 1), "completion_tokens": r["completion_tokens"],
            "prompt_tokens": r["prompt_tokens"], "prompt_ms": r["prompt_ms"],
            "decode_ms": r["decode_ms"], "decode_tps": r["decode_tps"]})
        print("\n=== 判分 ===")
        print("  真值: 目标函数=%s  注入行=%s  类别=%s"
              % (p["target_function"], p["bug_lines"], p["mutation"]["mtype"]))
        for k in ("error_class", "load_error", "syntax_ok", "f2p_ok", "f2p_n", "p2p_ok",
                  "p2p_n", "solved", "one_shot_usable", "loc_class", "hits_bug",
                  "footprint", "rewrote_function", "collateral", "diag_func", "diag_line",
                  "diag_type", "diag_correct", "sim_ast", "sim_tok", "sim_buggy_ast"):
            print("  %-18s %s" % (k, rec.get(k)))
        return 0

    sel = [p for p in allp if args.variants == "all" or p["variant"] == args.variants]
    if args.levels:
        want = {int(x) for x in args.levels.split(",")}
        sel = [p for p in sel if p["level"] in want]
    if args.limit:
        sel = sel[:args.limit]

    print("开始评测 %d 道（contract=%s variants=%s levels=%s regime=%s）"
          % (len(sel), args.contract, args.variants, args.levels or "all", args.regime))
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, "%s.json" % args.tag)

    # 续跑：输出文件已存在则加载已完成 id，跳过它们（避免两个慢 server 互相拖时重跑）。
    # 但「被 max_tokens 截断」和「请求失败」的是假结果，必须重跑。
    rows = []
    if os.path.exists(out):
        try:
            prev = json.load(open(out, encoding="utf-8"))
            prev_rows = prev.get("rows", []) if isinstance(prev, dict) else prev
            # 🔴 续跑修正：tune 在 nothink 下会退化复读、把上下文塞满才停
            # （finish=length），但修复代码在循环前已完整输出、已抽到 candidate
            # —— 这种「截断」评分有效，不该当假失败重跑。只有「截断且没抽到任何
            # 代码」才是真假失败，需重跑。否则 tune 全套跑完会被无限重跑。
            done_ids = {r["id"] for r in prev_rows
                        if "id" in r
                        and r.get("error_class") != "request_failed"
                        and not r.get("empty_reply")
                        and (not r.get("truncated")
                             or (r.get("candidate") or "").strip())}
            if done_ids:
                rows = [r for r in prev_rows if r["id"] in done_ids]
                before = len(sel)
                sel = [p for p in sel if p["id"] not in done_ids]
                redone = before - len(sel)
                print("♻️ 续跑：已完成 %d，跳过 %d，重跑 %d（截断/失败）"
                      % (len(done_ids), len(done_ids), redone))
        except Exception as e:  # noqa: BLE001
            print("⚠️ 读取旧结果失败（%s），从头跑" % e)

    def flush(rows):
        """逐题落盘：后面任何一步崩掉，已跑完的数据都还在。"""
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"partial": True, "rows": rows}, f, ensure_ascii=False, indent=1)

    for i, p in enumerate(sel, 1):
        pr = build_prompt(p, args.contract)
        try:
            r = chat(args.base_url, pr, args.regime, args.max_tokens, args.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print("[%3d/%d] %-40s REQUEST FAILED %s" % (i, len(sel), p["id"], e))
            rows.append({"id": p["id"], "seed": p["seed"], "level": p["level"],
                         "variant": p["variant"], "mtype": p["mutation"]["mtype"],
                         "request_failed": str(e), "solved": False, "one_shot_usable": False,
                         "format": "empty", "error_class": "request_failed",
                         "empty_reply": True, "loc_class": "miss", "hits_bug": False,
                         "footprint": 0, "rewrote_function": False, "collateral": False,
                         "f2p_ok": 0, "f2p_n": len(p["f2p"]), "p2p_ok": 0,
                         "p2p_n": len(p["p2p"]), "syntax_ok": False})
            continue

        code, how, spliced = prepare(p, r["content"], args.contract)

        meta = {"id": p["id"], "seed": p["seed"], "level": p["level"],
                "variant": p["variant"], "axis": p["axis"],
                "mtype": p["mutation"]["mtype"], "mdesc": p["mutation"]["desc"],
                "format": how, "fenced": "```" in r["content"],
                "empty_reply": not r["content"].strip(),
                "latency_s": round(r["wall_s"], 1),
                "completion_tokens": r["completion_tokens"],
                "prompt_tokens": r["prompt_tokens"],
                "prompt_ms": r["prompt_ms"], "decode_ms": r["decode_ms"],
                "decode_tps": r["decode_tps"],
                "finish_reason": r["finish_reason"],
                "truncated": r["finish_reason"] == "length",
                "raw": r["content"][:6000]}
        rec = grade_one(p, r["content"], code, spliced, meta)
        rec["candidate"] = code[:4000]
        rows.append(rec)
        flush(rows)

        flag = "OK " if rec["solved"] else "   "
        print("[%3d/%d] %-40s %s f2p %d/%d p2p %d/%d  %-16s loc=%-15s fp=%-3d %4.1fs%s"
              % (i, len(sel), p["id"], flag, rec["f2p_ok"], rec["f2p_n"],
                 rec["p2p_ok"], rec["p2p_n"], rec["error_class"], rec["loc_class"],
                 rec["footprint"], rec["latency_s"],
                 "  SPLICED" if spliced else ("  " + rec["format"] if rec["format"] != "sentinel" else "")))

    try:
        summary = summarize(rows, args)
    except Exception as e:                                   # noqa: BLE001
        # 汇总崩了不该毁掉 20 分钟的原始数据（已逐题落盘，见 flush）
        print("\n🔴 summarize 失败（%s: %s），但逐题数据已在 %s" % (type(e).__name__, e, out))
        return 0
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=1)
    print_summary(summary)
    print("-> %s" % out)
    return 0


def summarize(rows, args):
    n = len(rows)
    def rr(f):
        return round(sum(1 for r in rows if f(r)) / n, 3) if n else 0.0

    empty = sum(1 for r in rows if r.get("empty_reply"))
    s = {
        "tag": args.tag, "regime": args.regime, "contract": getattr(args, "contract", "module"),
        "variants": args.variants,
        "levels": args.levels or "all", "n": n,
        "empty_reply": empty,
        "valid": empty < 0.5 * n if n else False,
        "solved": sum(1 for r in rows if r.get("solved")),
        "solved_rate": rr(lambda r: r.get("solved")),
        "one_shot_usable": sum(1 for r in rows if r.get("one_shot_usable")),
        "one_shot_usable_rate": rr(lambda r: r.get("one_shot_usable")),
        "hits_bug_rate": rr(lambda r: r.get("hits_bug")),
        "no_change_rate": rr(lambda r: r.get("footprint") == 0),
        "collateral_rate": rr(lambda r: r.get("collateral")),
        "rewrote_function_rate": rr(lambda r: r.get("rewrote_function")),
        "format_ok_rate": rr(lambda r: r.get("format") in ("sentinel", "fence")),
        "sentinel_rate": rr(lambda r: r.get("format") == "sentinel"),
        "spliced_rate": rr(lambda r: r.get("spliced")),
        "fenced_rate": rr(lambda r: r.get("fenced")),
        "diag_correct_rate": rr(lambda r: r.get("diag_correct") is True),
        "diag_reported_rate": rr(lambda r: r.get("diag_type") not in (None, "unknown")),
        "truncated": sum(1 for r in rows if r.get("truncated")),
        "truncated_rate": rr(lambda r: r.get("truncated")),
        "truncated_unscored": sum(1 for r in rows
                                 if r.get("truncated") and not (r.get("candidate") or "").strip()),
        "grade_retry": sum(1 for r in rows if r.get("grade_retry")),
        "sim_ast_mean": None,
    }
    bycls = {}
    for r in rows:
        k = r.get("error_class")
        bycls[k] = bycls.get(k, 0) + 1
    s["error_class"] = bycls
    fails = [r for r in rows if not r.get("solved")]
    s["fail_silent"] = sum(1 for r in fails if r.get("error_class") == "wrong_result")
    s["fail_loud"] = sum(1 for r in fails if r.get("error_class") in
                         ("syntax", "load_error", "runtime_error", "timeout", "empty", "no_code"))
    s["fail_silent_rate"] = round(s["fail_silent"] / len(fails), 3) if fails else 0.0

    locs = {}
    for r in rows:
        k = r.get("loc_class")
        locs[k] = locs.get(k, 0) + 1
    s["loc_class"] = locs

    sims = [r["sim_ast"] for r in rows if r.get("sim_ast") is not None]
    if sims:
        s["sim_ast_mean"] = round(sum(sims) / len(sims), 3)
        s["sim_ast_identical_rate"] = round(
            sum(1 for r in rows if r.get("ast_identical")) / len(sims), 3)

    fps = sorted(r.get("footprint", 0) for r in rows)
    if fps:
        s["footprint_p50"] = fps[len(fps) // 2]
        s["footprint_p90"] = fps[int(len(fps) * 0.9)]

    for key, cnt in (("solved", s["solved"]), ("one_shot_usable", s["one_shot_usable"]),
                     ("hits_bug", sum(1 for r in rows if r.get("hits_bug")))):
        s["ci_" + key] = wilson(cnt, n)

    # 自报类别的**退化检测**：如果同一个标签占了多数，那不是"诊断错了"，
    # 而是它根本没在诊断（在输出常量）。这两件事必须分开报，否则 8% 会被误读成"接近随机"。
    tp = [r.get("diag_type") for r in rows if r.get("diag_type") not in (None, "unknown")]
    if tp:
        mode, cnt = Counter(tp).most_common(1)[0]
        s["diag_mode"] = mode
        s["diag_mode_share"] = round(cnt / len(tp), 3)
        s["diag_degenerate"] = cnt / len(tp) > 0.5
    else:
        s["diag_mode"] = None
        s["diag_mode_share"] = 0.0
        s["diag_degenerate"] = False

    # 分难度档（只看判别力：≈100% 或 ≈0% 都是死档）
    s["by_level"] = {}
    for lv in sorted({r["level"] for r in rows}):
        g = [r for r in rows if r["level"] == lv]
        s["by_level"]["L%d" % lv] = {
            "n": len(g),
            "solved_rate": round(sum(1 for r in g if r.get("solved")) / len(g), 3),
            "ci": wilson(sum(1 for r in g if r.get("solved")), len(g)),
        }
    return s


def print_summary(s):
    print()
    print("=" * 78)
    print("=== %s (%s, contract=%s, variants=%s, levels=%s) ==="
          % (s["tag"], s["regime"], s.get("contract"), s["variants"], s["levels"]))
    print("=" * 78)
    if not s["valid"]:
        print("🔴 空回复 %d/%d —— 疑似 enable_thinking 没生效（content 恒空），本次结果无效"
              % (s["empty_reply"], s["n"]))
    print("n=%-4d 整题通过     %3d (%.0f%%)   Wilson95 %s"
          % (s["n"], s["solved"], s["solved_rate"] * 100, s["ci_solved"]))
    print("      一次性可用   %3d (%.0f%%)   Wilson95 %s"
          % (s["one_shot_usable"], s["one_shot_usable_rate"] * 100, s["ci_one_shot_usable"]))
    print("      改到 bug 行  %3d (%.0f%%)   炸邻居 %.0f%%   整函数重写 %.0f%%"
          % (s["loc_class"].get("exact", 0),
             s["hits_bug_rate"] * 100, s["collateral_rate"] * 100, s["rewrote_function_rate"] * 100))
    print("      **一行没改** %.0f%%   ← 抄回原样（不是「修错了」，是根本没动）"
          % (s["no_change_rate"] * 100))
    print("      沉默失败占失败 %.0f%%  (失败 %d 道)" % (s["fail_silent_rate"] * 100,
                                                       s["fail_silent"] + s["fail_loud"]))
    print("格式: sentinel %.0f%% | 可用(含围栏) %.0f%% | 加了围栏 %.0f%% | 靠拼接救回 %.0f%%"
          % (s["sentinel_rate"] * 100, s["format_ok_rate"] * 100,
             s["fenced_rate"] * 100, s["spliced_rate"] * 100))
    print("自报类别正确 %.0f%%（报了 %.0f%%）| AST 相似度 %s"
          % (s["diag_correct_rate"] * 100, s["diag_reported_rate"] * 100, s["sim_ast_mean"]))
    if s.get("diag_degenerate"):
        print("  ⚠️ 自报类别退化：'%s' 占 %.0f%% ⇒ 它不是诊断错了，是**没在诊断**（在输出常量）"
              % (s["diag_mode"], s["diag_mode_share"] * 100))
    # 🔴 截断语义修正：区分「真假失败（截断且没抽到代码）」与
    # 「退化复读型截断（已抽到代码、评分有效）」。后者（如 tune nothink 复读到 ctx 满）
    # 不应误报成假失败，否则会误导人去无谓地提 max_tokens。
    unscored_trunc = s.get("truncated_unscored", 0)
    if unscored_trunc:
        print("⚠️ 有 %d 道被截断且没抽到代码（%.0f%%）⇒ 这些是假失败，先提 max_tokens/ctx 重跑"
              % (unscored_trunc, unscored_trunc / s["n"] * 100))
    elif s.get("truncated"):
        print("ℹ️ 有 %d 道被截断（%.0f%%），但都已抽到修复代码、评分有效"
              "（退化复读型截断，非假失败；续跑已识别为已完成）"
              % (s["truncated"], s["truncated_rate"] * 100))
    print("error_class: %s" % s["error_class"])
    print("loc_class  : %s" % s["loc_class"])
    print("改动足迹   : p50=%s p90=%s 行" % (s.get("footprint_p50"), s.get("footprint_p90")))
    if s.get("grade_retry"):
        print("（判分超时重试过 %d 道 —— 一次性进程启动超时，重试后正常；不是真死循环）"
              % s["grade_retry"])
    for lv, d in s["by_level"].items():
        mark = "  ← 有判别力" if 0.25 <= d["solved_rate"] <= 0.75 else ""
        print("  %s n=%-3d 通过 %.0f%%  Wilson95 %s%s"
              % (lv, d["n"], d["solved_rate"] * 100, d["ci"], mark))


if __name__ == "__main__":
    sys.exit(main())
