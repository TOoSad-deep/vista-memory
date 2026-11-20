"""Print a local VISTA stdio configuration for an MCP client or a DSH profile."""

import argparse
import json
import sys
from importlib import import_module
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="VISTA 数据目录")
    parser.add_argument(
        "--format",
        choices=("mcpServers", "dsh"),
        default="mcpServers",
        help="输出 mcpServers 对象（默认）或原生 DSH 的 --patch 数组",
    )
    args = parser.parse_args()
    try:
        import_module("vista_memory.mcp")
    except ImportError as exc:
        parser.error(
            f"当前 Python 环境无法加载 VISTA MCP ({exc})。"
            "请在项目根目录运行 uv sync --locked --extra mcp，"
            "然后用同一环境的 Python 运行本脚本。"
        )

    root = args.root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        parser.error(f"数据目录是一个文件：{root}")
    server = {
        # A venv's python may be a symlink. Keep its path to retain its packages.
        "command": str(Path(sys.executable).absolute()),
        "args": ["-m", "vista_memory.mcp", "--root", str(root)],
    }
    if args.format == "dsh":
        plugin = {
            "id": "mcp-vista",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": "vista",
                "transport": "stdio",
                **server,
                "failOnStartupError": True,
            },
        }
        config = [{"insert": [plugin]}]
    else:
        config = {"mcpServers": {"vista": server}}
    print(json.dumps(config, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
