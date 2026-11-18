#!/usr/bin/env bash
# 复现 fork：clone 上游 → 退到基线 → 打 patch → 编译。
#
#   bash scripts/setup_llama.sh            # 完整流程
#   DEST=/somewhere/llama.cpp bash scripts/setup_llama.sh
#   SKIP_BUILD=1 bash scripts/setup_llama.sh   # 只打 patch，不编译
#
# patch 不在本仓。改动的唯一真源是 cpu-afterburner：
#   https://github.com/whymycompilefail/cpu-afterburner
#
# 那份 patch 里同时带着 TTS 的改动（tools/tts、tools/mtmd），这不是多带的东西：
# MoE 推理和 TTS 走的是同一套 ggml 内核，切分方式、循环顺序、图宽度改一次两边
# 都受益。把它裁成"纯文本版"和"文本+TTS 版"两个变体等于把一件整体的事拆两半，
# 而且两份会各自漂移——裁过两次，一次记 15 文件 +1121/−173，一次记 16 文件
# +1239/−130，都跟真源（20 文件 +1428/−194）对不上。
#
# 本仓只用 llama-server，所以 TTS 那部分不编就不参与：tools/tts 是独立 target，
# mtmd 那几处只在 GEN_WAV 分支里生效。想一起编就直接编，三个二进制都在。
#
# patch 必须打在 **e351231** 上。上游每周几十个提交，ggml 那块重构频繁，
# 打不上时报错会指到一个跟你改动无关的地方（README「不要这么干」里有说明）。
# 这个 commit 是用 patch 的 20 个前像 blob 在官方历史里反查出来的：在那一点，
# 这 20 个文件与本 patch 的基线逐字节相同（2026-09-25，master 上真实存在）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN=e351231c4f4cdd89c88e696c46d0eb718c9e0ab5
DEST="${DEST:-$HERE/llama.cpp}"

AFTERBURNER_REPO="${AFTERBURNER_REPO:-https://github.com/whymycompilefail/cpu-afterburner}"
AFTERBURNER_DIR="${AFTERBURNER_DIR:-$HERE/../cpu-afterburner}"
PATCH_REL="patches/cpu-afterburner.patch"
CACHE="$HERE/.cache/cpu-afterburner.patch"

if [ -f "$AFTERBURNER_DIR/$PATCH_REL" ]; then
    PATCH="$AFTERBURNER_DIR/$PATCH_REL"
    echo "=== patch 来源（本地）：$PATCH"
else
    echo "=== 取 patch：$AFTERBURNER_REPO"
    mkdir -p "$(dirname "$CACHE")"
    if ! curl -fsSL "$AFTERBURNER_REPO/raw/main/$PATCH_REL" -o "$CACHE"; then
        echo "下载失败。可以手动 clone 之后重跑：" >&2
        echo "  git clone $AFTERBURNER_REPO $AFTERBURNER_DIR" >&2
        exit 2
    fi
    PATCH="$CACHE"
fi

[ -f "$PATCH" ] || { echo "找不到 patch: $PATCH" >&2; exit 2; }

if [ -d "$DEST/.git" ]; then
    echo "=== 复用已有克隆：$DEST"
    cd "$DEST"
    git fetch --tags origin || true
else
    echo "=== clone 到 $DEST"
    # --filter=blob:none 只按需取 blob，比全量快很多；checkout 目标 commit 时会补齐。
    git clone --filter=blob:none https://github.com/ggml-org/llama.cpp "$DEST"
    cd "$DEST"
fi

echo "=== 退到基线 $PIN"
git checkout -f "$PIN"

echo "=== 打 patch"
git apply --check "$PATCH"
git apply "$PATCH"
echo "    ok"

if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "=== SKIP_BUILD=1，跳过编译"
    exit 0
fi

echo "=== 配置 & 编译"
# Windows 上最大的坑：MSVC 的 INCLUDE / LIB 只在「x64 Native Tools Command Prompt」
# 或 vcvars64.bat 里才有。在普通 shell 里直接 cmake 会挂在
#   ggml.h(211) fatal error C1083: 无法打开 stdbool.h
# 报错位置看起来像源码问题，其实是环境变量没设。
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    if [ -z "${INCLUDE:-}" ]; then
      echo "错误：INCLUDE 为空 —— 当前不是 MSVC 环境。" >&2
      echo "     请在「x64 Native Tools Command Prompt for VS 2022」里运行，或先执行：" >&2
      echo '       call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"' >&2
      exit 2
    fi
    ;;
esac

# GGML_NATIVE=ON 是故意的：让编译器按本机 CPU 生成指令。关掉能跑但会慢一截，
# 然后你会以为是 patch 没用。GGML_AVX_VNNI 保持默认关（见 README）。
cmake -S . -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=OFF -DGGML_VULKAN=OFF \
    -DGGML_NATIVE=ON -DLLAMA_BUILD_TESTS=OFF
cmake --build build -j

echo
echo "=== 完成。产物在 $DEST/build/bin/"
echo "    llama-server（本仓用这个）/ llama-bench / llama-tts"
echo "    下一步：GGUF=/path/to/model.gguf bash serve_mcp2b.sh"
