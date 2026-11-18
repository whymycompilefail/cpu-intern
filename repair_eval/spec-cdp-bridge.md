# Spec：网页版文本桥（CDP Bridge）

> 状态：**已实现并通过 G1–G5**（2026-10-20）；**G6（跑完难题集出分）待跑**。
> 实现：`cdp_ws.py`（零依赖 WebSocket）、`cdp_bridge.py`（桥主体）、
> `sites.json`（多站点配置）、`launch_edge_cdp.sh` + `prepare_cdp_profile.sh`。
> 实测踩到的坑见 §10 —— 每一条都是真踩的，不是预防性设计。
> 目标：让我（AI）能自己与网页版大模型对话——发纯文本、收纯文本、落盘——
> 从而把「满血大脑」接入我们已有的 repair_eval 循环（F2P/P2P + 报错回灌）。
> 上游参考项目只学思路（详见 `EAgent-调研与对我们的启示.md` §10.1）。

---

## 1. 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 连一个**已登录**的浏览器实例 | 不碰密码、不存凭据、不管登录 |
| 把一段**纯文本**送进输入框并提交 | 不执行模型返回的任何代码 |
| 等生成结束，取**回复纯文本** | 不调任何工具（无鼠标/键盘/截图/脚本执行） |
| 把回复**落盘**为本地文件 | 不做多账号、不做并发刷量 |
| 串行队列 + 礼貌间隔 | **不做任何反检测/身份伪装** |

**一句话**：自动化的是「复制粘贴」这个动作，不是「替人操作电脑」。

---

## 2. 形态（用户拍板）

```
[我 / harness]                    [本机浏览器 · 用户自己的账号]
   构造 prompt ──► 注入输入框 ──► 提交
                                  ... 等待生成 ...
   取回文本  ◄── 读回复 ◄───────── 生成结束
   落盘 → harness 跑 F2P/P2P → 报错作为下一轮回灌
```

不需要工具调用能力 ⇒ 复杂度远低于通用 CDP agent（对比 §9.4 四坑，只剩 1.5 个真难点）。

---

## 3. 技术选型：零依赖（纯标准库）

**结论：不装第三方包，用 `socket` 手写一个最小 WebSocket 客户端。**

| 方案 | 判断 |
|---|---|
| managed python + 装 `websocket-client` | 需新建 venv 并联网安装；且本机环境隔离规则要求装进指定 venv |
| 复用 `Open-LLM-VTuber/.venv`（已有 websocket） | 那是第三方项目的 venv，混用会污染其依赖 |
| **纯标准库 `socket`** ✅ | 零依赖、可随项目走。CDP 只用文本帧 + 小消息，握手与帧编解码 **~120 行**足够 |

已核实的环境事实：
- managed python（3.13.12）**无** `websocket`、**无** `requests`
- `Open-LLM-VTuber/.venv` 有 websocket（备用，不首选）
- Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- 9222 / 9223 **均空闲**（此前"OPEN"是脚本误报，已修正）

---

## 4. 五个阶段与难点对策

| # | 阶段 | 做法 | 难点 |
|---|---|---|---|
| 1 | 启动/接入 | 用独立 user-data-dir 起 Edge 并开 `--remote-debugging-port=9222`；或接已有实例 | **登录态**：首次需人工登录一次，之后复用 profile |
| 2 | 发现页面 | `GET /json/list` 找到目标标签页，取 `webSocketDebuggerUrl` | ⚠️ **必须绕过本地代理**（见 §5） |
| 3 | 输入注入 | `Runtime.evaluate`：用**原型链原生 setter** 写 `textarea.value`，再派发 `new Event('input',{bubbles:true})` | 🔴 **React 覆写了 value setter**，直接赋值框架收不到 —— 唯一真难点 |
| 4 | 提交 + 等结束 | 点击发送按钮（或派发 Enter）；轮询判「停止/重新生成」按钮状态消失 + 文本长度稳定 N 秒 | 不能用 sleep；要双条件（按钮状态 **且** 长度稳定） |
| 5 | 提取落盘 | `Runtime.evaluate` 取回复容器 innerText → 写本地文件 | 选择器需**探测式**，不硬编码（网页会改版） |

### 4.1 选择器策略
不硬编码任何 DOM 选择器。改为**配置文件 + 探测**：
- 配置里放候选列表（`textarea` / `[contenteditable]` / 发送按钮的 aria-label 候选）
- 启动时逐个探测，命中的写回配置文件，失败则**明确报错并打印候选**，不做模糊兜底

---

## 5. ⚠️ 环境坑：本地代理会拦 127.0.0.1

实测：环境注入了 `http_proxy`（本机 127.0.0.1 上的代理），
`curl http://127.0.0.1:9222/json/version` 会返回 **"upstream connect failed"**（被代理接走）。

⇒ 所有 CDP 的 HTTP 请求必须显式绕过：`no_proxy=127.0.0.1,localhost` 或临时清空 `http_proxy`。

---

## 6. 边界（我给自己划的红线）

| 类别 | 处理 |
|---|---|
| 礼貌速率（串行 + 间隔） | ✅ 做 —— 目的是**降低对方服务器负担**，属正常礼貌 |
| 隐藏自动化特征（伪装 `navigator.webdriver`、伪造 UA/指纹、绕过风控） | ❌ **不做** —— 这是主动对抗检测，不是自动化表单 |
| 多账号 / 号池 / 高频并发 | ❌ 不做 |
| 执行模型返回的任何内容 | ❌ 不做（输出只落盘，不执行） |

**风险如实告知**：自动化访问第三方网页通常违反其 ToS，最坏结果是**该账号被限制**。
用户已明确接受此风险（"灰色地带不是问题"）。本设计通过「串行 + 低频 + 不伪装」
把概率压到最低，但**不能降到零**——这一点不掩饰。

---

## 7. 验收标准（做出来立刻回答那个悬而未决的问题）

**第一个用途就是跑基准**：用网页版跑 `problems_hard.json`（27 道 mixed），
与本地基线对比：

| 模型 | 难题集通过 |
|---|---|
| Qwen2.5-Coder-3B | 59% |
| MiniCPM5-2.6B | 44% |
| Qwen3.5-2B | 37% |
| **网页版（待测）** | **?** |

**验收清单**
- [ ] G1 能连上浏览器并列出标签页
- [ ] G2 能把一段已知文本注入输入框（页面上肉眼可见）
- [ ] G3 能提交并等到生成结束，取回完整回复
- [ ] G4 回复落盘，且**与页面显示一致**（无截断、无乱码）
- [ ] G5 复用 `run_repair.py` 的 `<<<FIXED>>>` 解析 + F2P/P2P 评分跑通 1 道题
- [ ] **G6 跑完难题集 27 道，出通过率** ← 真正的验收

> G6 同时回答了「满血大脑 + 我们的循环到底值多少 pp」，
> 这是此前一直没验证的两个假设之一（另一个是循环本身的价值）。

---

## 8. 不做的事（明确排除）

- 不实现任何工具调用（那正是我们与参考项目的分界）
- 不改 `run_repair.py` 的核心评分逻辑，只加一个"文本来源"适配层
- 不把浏览器凭据/会话写进仓库；配置文件中不含任何个人标识

---

## 10. 实测记录：真踩到的坑（都已解决）

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | **默认 profile 开不了调试端口** | 用默认 user-data-dir 时 `--remote-debugging-port` 被忽略，端口永不监听、不写 `DevToolsActivePort` | 必须用自定义 `--user-data-dir`；用 `prepare_cdp_profile.sh` 复制登录态过去 |
| 2 | **登录态迁移必须带 `Local State`** | 只复制 Cookies 解不开（DPAPI 主密钥在 `Local State`） | 复制 `Local State` + `Default/Network`（Cookies）+ Local Storage / IndexedDB |
| 3 | **浏览器活不过一次工具调用** | `&` 或 PowerShell `Start-Process` 启动的 Edge 在调用返回后被杀 | 启动脚本必须**常驻前台** + 用受管后台任务跑（`launch_edge_cdp.sh` 用 `wait`） |
| 4 | **PowerShell `Start-Process` 不传参** | 用 Start-Process 传 `--remote-debugging-port` 无效（进程起来了但端口没开） | 改用 Bash 启动 |
| 5 | **本地代理吞掉 127.0.0.1** | `curl http://127.0.0.1:9222/json/version` → "upstream connect failed" | `urllib` 用 `ProxyHandler({})` 显式禁用代理 |
| 6 | **发送按钮无法识别** | DeepSeek 的按钮全是 hash class、无 aria-label、无文本 | 绕开：注入后用 **Enter** 提交，不需要找按钮 |
| 7 | 🔴 **`innerText` 吃掉 Python 缩进** | 模型不用代码围栏时，代码被当普通段落渲染，前导空格被 HTML 折叠 → 代码直接报废 | prompt 强制要求 ``` 围栏；提取时对 `<pre>` 用 `textContent` |
| 8 | **代码块工具栏污染文本** | 提取结果混入 `python`、`复制`、`下载` 等 UI 文字 | 标记放进围栏**内部**作注释，提取优先取 `preText`（工具栏在 pre 外） |
| 9 | JS 隐式字符串拼接 | 跨行写两个字符串字面量（Python 习惯）→ `SyntaxError: Unexpected string` | JS 跨行必须用 `+` |

### 10.1 端到端验证结果（value_at / falsy_sentinel）

```
CDP 注入 → 网页版生成 → 提取 → 落盘 → 正则解析 → exec → F2P/P2P
F2P idx=0（真正的 bug）  PASS
P2P idx=1                PASS
P2P miss                 PASS
```

遗留小瑕疵：`# <<<END>>>` 会让解析结果尾部多一个 `#`（合法注释，不影响编译）。
若要彻底干净，可让结束标记放在围栏外独占一行。

### 10.2 与 `run_repair.py` 的兼容点（重要）

`run_repair.py` 的本地 prompt 明写 **"不要加 markdown 围栏"**，
但**网页版必须加围栏**（否则缩进被 HTML 吃掉，见坑 7）。

二者不冲突：`run_repair.py` 的 `_decap()` 本来就负责剥离 `<<<FIXED>>>` 里多包的围栏
（代码注释里明写"1B 会在 FIXED 里再包一层 markdown 围栏"）。
⇒ 网页版走**带围栏**的 prompt 模板，`_decap` 会正常剥掉。

---

## 9. 待你确认的三个点

1. **浏览器**：用 Edge（已确认存在）还是 Chrome？用**独立 profile** 还是**接你已有的登录实例**？
   （独立 profile 更干净但需登录一次；接已有实例更省事但会挂上你正在用的浏览器）
2. **目标站点**：只做 DeepSeek，还是做成可配置多站点？
3. **风险接受**：§6 的 ToS 风险你已接受——若账号被限制，我们不追责对方、也不做规避，接受即可。
