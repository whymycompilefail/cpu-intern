#!/usr/bin/env bash
# 取到能编译的 llama.cpp：clone cpu-afterburner（源码就在它仓库里）→ 编译。
#
#   bash scripts/setup_llama.sh
#   AFTERBURNER_DIR=/d/cpu-afterburner bash scripts/setup_llama.sh   # 用本地已有克隆
#   BUILD=/d/build bash scripts/setup_llama.sh                       # 构建目录放别处
#   SKIP_BUILD=1 bash scripts/setup_llama.sh                         # 只取源码
#
# 为什么不再"clone 上游 → 退到基线 → 打 patch"：那条链上每一步都会单独失败，
# 而且失败得很像别的问题。基线 commit 曾写成一个官方仓库里不存在的哈希
# （见 cpu-afterburner 的 patches/README.md），退一步找"日期最接近"的，
# 20 个文件里 17 个已被上游改过、git apply 只报 2 个失败其余静默错位。
# 现在 cpu-afterburner 直接托管打好的源码树：一次 clone，没有基线、没有 apply。
#
# TTS 的改动（tools/tts、tools/mtmd）本来就在同一棵树里，不是额外塞的东西：
# MoE 推理和 TTS 走同一套 ggml 内核。本仓只用 llama-server，不编 TTS 就不参与。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AFTERBURNER_REPO="${AFTERBURNER_REPO:-https://github.com/whymycompilefail/cpu-afterburner}"
CLONE="${AFTERBURNER_DIR:-$HERE/../cpu-afterburner}"
SRC="$CLONE/llama.cpp"
BUILD="${BUILD:-$SRC/build}"

if [ -f "$SRC/CMakeLists.txt" ]; then
    echo "=== 用本地已有的源码树：$SRC"
else
    echo "=== clone $AFTERBURNER_REPO -> $CLONE"
    # --depth=1：只要一棵能编的树，不需要那一堆历史。
    git clone --depth=1 "$AFTERBURNER_REPO" "$CLONE"
fi

[ -f "$SRC/CMakeLists.txt" ] || {
    echo "clone 完了却找不到 $SRC/CMakeLists.txt —— 仓库结构变了？" >&2
    exit 2
}

if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "=== SKIP_BUILD=1，源码在 $SRC"
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
# 然后你会以为是优化没用。GGML_AVX_VNNI 保持默认关（见 README）。
cmake -S "$SRC" -B "$BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=OFF -DGGML_VULKAN=OFF \
    -DGGML_NATIVE=ON -DLLAMA_BUILD_TESTS=OFF
cmake --build "$BUILD" -j

echo
echo "=== 完成。产物在 $BUILD/bin/"
echo "    llama-server（本仓用这个）/ llama-bench / llama-tts"
echo "    下一步：GGUF=/path/to/model.gguf bash path1/serve_mcp2b.sh"
