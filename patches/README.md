# llama.cpp 改动：改了什么，为什么

这个仓库不 vendored llama.cpp。`llama-cpp-cpu-tuning.patch` 是一份针对上游快照的 diff，`scripts/setup_llama.sh` 负责 clone、退基线、打 patch、编译。

## 上游基线

| 项 | 值 |
|---|---|
| 上游 | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT） |
| 基线 commit | `5096f2a42468daff7ff38d97b48bd17432196e98` |
| 改动规模 | **16 个文件，+1239 / −130 行** |
| patch 路径前缀 | 相对 llama.cpp 根目录，所以要在该目录里 `git apply` |

```bash
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
git checkout 5096f2a42468daff7ff38d97b48bd17432196e98
git apply ../patches/llama-cpp-cpu-tuning.patch
```

## 16 个文件，各自属于哪一项

| 文件 | 改动 | 对应项 |
|---|---|---|
| `ggml/src/ggml-cpu/repack.cpp` | +129/−? | **MUL_MAT_ID 抢块**（`MMID_DYNAMIC`） |
| `ggml/src/ggml-cpu/ggml-cpu.c` | +153 | **op profiler**（`GGML_OPPROF`）+ 量化算子分派 |
| `ggml/src/ggml-cpu/ops.cpp` | +741 | KV 头分组重排、量化 V 算子（GQA 相关，验收部分通过 / 一项关闭） |
| `ggml/src/ggml-cpu/common.h`、`ops.h` | 小 | 上面两项的声明与常量 |
| `ggml/include/ggml.h`、`ggml/src/ggml.c` | +20 / +45 | 新算子与类型注册（`ggml_mul_mat_tq` 等） |
| `src/llama-cparams.h`、`llama-graph.cpp`、`llama-context.cpp`、`llama-model.cpp` | +80/+52/+55/+45 | `-fa auto` 按 KV 长度动态切注意力 |
| `include/llama.h`、`common/arg.cpp`、`common/common.{cpp,h}` | 小 | 上面那个开关的命令行接入 |
| `conversion/base.py` | +5 | deepseek-coder-1.3b 的 tokenizer 哈希登记（上游列表里没有，本地补的） |

逐行改动读 patch；为什么这么改、实测多少，读 `specs/` 里对应的那份。

## 这份 patch 里**没有**什么

原 fork 里还有 `tools/tts/tts.cpp`（+215）、`tools/mtmd/`（+38）和几处 Qwen3-TTS 适配。那是同一台机器上**另一个项目**（虚拟主播的语音合成）的改动，跟代码修复流水线无关，所以从这份 patch 里剔掉了。

想同时要那部分，去 `cpu-only-ai-tuber` 仓库拿全量 fork。这边保持干净：`git apply` 之后 `llama-server`、`llama-bench`、`llama-cli` 全部可用，TTS 工具是上游原版。

## 两个必须知道的前提

**1. 基线必须精确。** 上游每周几十个提交，ggml 那块重构频繁。打在别的 commit 上，`git apply` 报错会指到一个跟你改动无关的位置，很难看出真因。

**2. 策略开关必须是编译期 `#define`。** `repack.cpp` 里的 `MMID_DYNAMIC` 就是这样。试过做成运行期 `if`（else 分支为空、循环体一字未改），`gemv<>` 这个巨型模板掉 26%（19.26 → 14.32 tok/s）：MSVC 一旦见它落在分支里，就不再生成同样的代码。要验证这一点，把 `MMID_DYNAMIC` 改成从环境变量读，然后跑 `llama-bench -p 512 -n 128`。

## 回滚

两项新改动都是单点开关：

| 想要的效果 | 做法 |
|---|---|
| 关掉抢块，回到上游的静态切分 | `repack.cpp` 顶部 `MMID_DYNAMIC 0`，重编译 |
| 关掉 profiler | 不设 `GGML_OPPROF` 环境变量即可（零开销，留着无妨） |
| 关掉 `-fa auto` 自适应 | 启动时传 `-fa on` 或 `-fa off`，不走阈值分支 |

## 发布边界

- ✅ 发：patch、specs、脚本
- ❌ 不发：llama.cpp 源码本体（用 clone 复现）、`build/`、权重、题池、`results/`

`build/` 不只是体积问题——它是这类项目隐私命中的主要来源：`CMakeCache.txt` 里有 Windows 用户名，`compile_commands.json` 里全是本机绝对路径。仓库里不带它，也不会被 `git add`（`.gitignore` 已排除）。
