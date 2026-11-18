#!/usr/bin/env bash
# 复现 fork：clone 上游 → 退到基线 → 打 patch → 编译。
#
#   bash scripts/setup_llama.sh            # 完整流程
#   DEST=/somewhere/llama.cpp bash scripts/setup_llama.sh
#   SKIP_BUILD=1 bash scripts/setup_llama.sh   # 只打 patch，不编译
#
# patch 必须打在 **5096f2a** 上。上游每周几十个提交，ggml 那块重构频繁，
# 打不上时报错会指到一个跟你改动无关的地方（README「不要这么干」里有说明）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN=5096f2a42468daff7ff38d97b48bd17432196e98
DEST="${DEST:-$HERE/llama.cpp}"
PATCH="$HERE/patches/llama-cpp-cpu-tuning.patch"

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
echo "=== 完成。llama-server 在 $DEST/build/bin/"
echo "    下一步：GGUF=/path/to/model.gguf bash serve_mcp2b.sh"
