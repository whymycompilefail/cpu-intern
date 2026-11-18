#!/usr/bin/env bash
# 把本地 gguf 挂到 8299 上，给 harness 当本地模型后端。
#
# 必须和调用方同一条命令：llama-server 用裸 `&` 起，外层 shell 一结束进程就被回收。
# 所以这里前台阻塞，由调用方的后台任务挂住（Windows 下别用 nohup / setsid）。
#
# 用法：
#   GGUF=/path/to/model.gguf bash serve_mcp2b.sh
#   GGUF=... LLAMA=/path/to/llama-server PORT=8299 THREADS=18 bash serve_mcp2b.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# llama-server 的位置：默认假设 llama.cpp 被 clone 到本仓库下的 llama.cpp/，
# 用 scripts/setup_llama.sh 就会长在那儿。装在别处就用 LLAMA= 覆盖。
LLAMA="${LLAMA:-$HERE/llama.cpp/build/bin/llama-server.exe}"
[ -x "$LLAMA" ] || LLAMA="${LLAMA%.exe}"
GGUF="${GGUF:-}"
PORT="${PORT:-8299}"

# 线程数默认 18。来源：Ultra 7 155H（6P+8E+2LPE，22 逻辑线程）上 llama-bench 实测，
# 两个不同规模的模型峰值都落在 18：
#   gpt-oss-20b MXFP4   t=18 -> 21.5 tok/s（t=14 19.9 / t=22 20.1）
#   mcp2b_v4 2B Q4_K_M  t=18 -> 25.8 tok/s（t=16 25.0 / t=22 23.9 / t=12 22.4）
# 换模型 / 换机器都要重扫。扫描时注意两个坑（见 README「怎么调」）：
#   顺序扫 12→22 会被"越跑越快"漂移骗到 15%，必须正反交替；
#   t=14 是最不稳的点（会踩到 SMT 兄弟）。
THREADS="${THREADS:-18}"
# 模型专属参数（如 gpt-oss 的 --reasoning-effort low --reasoning-format deepseek）
EXTRA_ARGS="${EXTRA_ARGS:-}"

if [ -z "$GGUF" ]; then
    echo "错误：没有指定模型。用 GGUF=/path/to/model.gguf bash $0" >&2
    exit 2
fi
if [ ! -x "$LLAMA" ]; then
    echo "错误：找不到 llama-server（$LLAMA）。" >&2
    echo "     先跑 scripts/setup_llama.sh，或用 LLAMA=/path/to/llama-server 指定。" >&2
    exit 2
fi

echo "=== llama-server :$PORT <- $GGUF  (threads=$THREADS)"
[ -n "$EXTRA_ARGS" ] && echo "=== EXTRA: $EXTRA_ARGS"
echo "=== health 就绪后本进程会一直挂着（Ctrl+C / 停后台任务即释放）"

"$LLAMA" -m "$GGUF" \
    -c 4096 -t "$THREADS" -fa on --jinja \
    $EXTRA_ARGS \
    --host 127.0.0.1 --port "$PORT"
