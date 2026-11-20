#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
用法：scripts/quickstart.sh [--root PATH]

安装锁定的 MCP 依赖，生成 4 条示例记录并通过两个 MCP 进程回读。
默认在系统临时目录中为每次运行新建一个数据目录，退出时保留。
--root PATH  保存到指定目录，支持相对于当前工作目录的路径。
             在同一目录重复运行，每次会追加 4 条记录。
EOF
}

if [ "$#" -eq 1 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ]; }; then
    usage
    exit 0
fi
if [ "$#" -ne 0 ] && { [ "$#" -ne 2 ] || [ "$1" != "--root" ] || [ -z "$2" ]; }; then
    usage >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    echo '需要 uv。请先安装 https://docs.astral.sh/uv/getting-started/installation/ ，再重新运行。' >&2
    exit 1
fi

project=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd -P)
uv sync --project "$project" --locked --no-dev --extra mcp --inexact

if [ "$#" -eq 0 ]; then
    root=$(mktemp -d "${TMPDIR:-/tmp}/vista-quickstart.XXXXXXXX")
else
    case "$2" in
        /*) root=$2 ;;
        *) root=$PWD/$2 ;;
    esac
    mkdir -p "$root"
fi
root=$(CDPATH= cd -P "$root" && pwd -P)
printf 'VISTA 示例数据目录：%s\n' "$root" >&2
exec uv run --project "$project" --no-sync python "$project/examples/local_roundtrip.py" --root "$root"
