# vista-memory

给 Agent 一个可以回看的本地图片库。你的程序用 Python 保存一张 PNG，可以同时存下生成它的 GLSL 和渲染参数。之后 Agent 通过 MCP 列出记录，按记录名取回当时的原图和代码。

## 快速开始

准备好 Git、uv 和支持 POSIX 目录操作的本地文件系统（macOS 或 Linux）。项目要求 Python 3.11 及以上。示例会自己画图，首次运行可能需要联网下载 Python 或依赖，全程无需模型账号。[uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```bash
git clone https://github.com/TOoSad-deep/vista-memory.git
cd vista-memory
./scripts/quickstart.sh --root ./vista/demo
```

脚本按 `uv.lock` 安装依赖，往 `vista/demo/` 追加 4 条示例记录，再用两个先后启动的 MCP 进程核对目录、原图字节和代码。看到以下字段，就说明写入和重启回读都成功了。

```json
{
  "status": "passed",
  "records_added": 4,
  "restart_readback_verified": true
}
```

打开 `vista/demo/png/` 就能看到示例图。`json/` 下的每条记录关联一张 PNG 和可选的 `glsl/` 文件。同一目录再跑一次会追加 4 条。只想临时试用时，直接运行 `./scripts/quickstart.sh`，脚本会新建并打印本次的临时目录。

## 让 Agent 打开刚才的图

**通用 MCP 客户端。** 对于使用 `mcpServers` 的客户端，在仓库根目录运行下面的命令。它打印适合这台机器的 JSON，包含 Python 解释器和示例库的绝对路径。如果已有图库，可以跳过示例，先执行 `uv sync --locked --no-dev --extra mcp --inexact`，再将 `--root` 换成自己的目录。

```bash
uv run --no-sync python scripts/mcp_config.py --root ./vista/demo
```

把输出中的 `vista` 条目合并到客户端现有的 `mcpServers` 下，重载客户端。然后请支持图片的 Agent 试着回看一张图。

> 请列出 VISTA 图片，打开标签为 Image fixture 3 的那张，告诉我圆是什么颜色。

客户端应先调用 `list_records`，再把返回的 `record` 文件名传给 `read_record`。示例图中，深色背景上有一个橙色圆。你也可以直接在客户端的工具面板中调用这两个工具。

| 工具 | 传入 | 取回 |
| --- | --- | --- |
| `list_records` | `{}` | 记录文件名、标签、来源和是否有关联代码 |
| `read_record` | `record` 填上一步返回的文件名，`content` 可选 | 元数据及所选内容。默认 `image` 读原 PNG，`code` 读 GLSL，`both` 同时取图和代码 |

图片通过 MCP 图像内容块传输。读取无代码记录时，`code` 返回“没有关联代码”，`both` 仍会返回图片。客户端会自行通过 stdio 启动服务。如果手动调试，可运行 `uv run --no-sync vista-memory-mcp --root ./vista/demo`，终端会等待客户端输入协议消息。

**原生 DeepSeek Harness（DSH）。** 已装 DSH 的用户可以为 SDK 单独生成一份补丁，使用同一份图片库。

```bash
uv run --no-sync python scripts/mcp_config.py --root ./vista/demo --format dsh > ./vista/dsh-patch.json
dsh --profile sdk-minimal --patch "$PWD/vista/dsh-patch.json"
```

这里的 `dsh` 指本机的可执行文件。macOS 桌面版如果未加入 PATH，可换成 `"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"`。这个命令供 SDK 客户端通过 stdio 连接。会话中的工具名为 `mcp__vista__list_records` 和 `mcp__vista__read_record`。需要让 Agent 真正看图时，在宿主中配置支持图片的模型。[原生 DSH 测试计划](docs/vista-memory-test-plan-20261113.md) · [工具调用和图片送达记录](docs/test-results/20261116-dsh/README.md)

**常见问题**

- **列表是空的。** 核对生成配置时的 `--root` 与快速启动打印的目录。一个新的可写目录会被初始化为空库。
- **客户端报找不到 Python 或模块。** 移动仓库或重建虚拟环境后，重新生成配置并重载客户端。生成配置要用装有 `vista-memory[mcp]` 的 Python。
- **想连自己的图库。** 用下面的 Python 接口保存 PNG，再用同一个根目录生成 MCP 配置。只有通过 VISTA 登记的图才会出现在列表里。

## 用 Python 保存自己的图

快速启动已经装好 Python 包。只需要 Python 存取接口时，也可以在自己的虚拟环境里从仓库根目录运行 `python -m pip install .`。需要 MCP 时使用 `python -m pip install '.[mcp]'`。

把一张 PNG 放在当前目录，命名为 `reference.png`。用过快速启动的读者可以把下面的代码保存为 `save_image.py`，在仓库根目录执行 `uv run --no-sync python save_image.py`。

```python
from pathlib import Path
from vista_memory import VistaStore

store = VistaStore("vista/my-images")
reference = store.save_record(png=Path("reference.png").read_bytes(), label="参考图")
result = store.read_record(reference["record"])
print(result.metadata["label"], len(result.image))  # 参考图、取回的 PNG 字节数
```

如果图片是渲染结果，还可以传入完整代码和当时生效的参数。下面假设你已有两张渲染图和一份 GLSL。两次只改了参数，可以复用第一条记录的代码。

```python
first = store.save_record(
    png=Path("render-001.png").read_bytes(),
    label="第 1 次渲染",
    glsl=Path("shader.glsl").read_bytes().decode("utf-8"),
    source_agent="generation",
    render={"time": 0, "uniforms": {"radius": 0.2}},
)
second = store.save_record(
    png=Path("render-002.png").read_bytes(),
    label="第 2 次渲染",
    reuse_glsl_from=first["record"],
    source_agent="generation",
    render={"time": 0, "uniforms": {"radius": 0.3}},
)
result = store.read_record(second["record"], content="both")
print(result.metadata, result.code)
```

`glsl` 与 `reuse_glsl_from` 二选一，代码变化时传新的正文。每次保存会追加一条记录。返回的 `record` 是后续读取所需的文件名，`label` 是给人看的名称。Python 读取时，未请求的内容和未关联的代码为 `None`。输入无效、记录损坏或文件访问失败会引发 `VistaError`。

## 在一个任务里用 R1、R2 回看

如果要记住“用户认可了哪张图”，可以为任务建立短号和备注。下面把上面保存的参考图登记到一个任务，然后按短号取回原图。任务目录用来保存短号、反馈和目录视图。

```python
from vista_memory.history_catalog import HistoryCatalog
from vista_memory.history_store import TaskHistory

history = TaskHistory(store, "vista/tasks/icon-review", task_id="icon-review")
item = history.import_record(f"import-{reference['record']}", reference["record"])
catalog = HistoryCatalog(history)
catalog.append_user_feedback(f"feedback-{item.short_id}", f"{item.short_id} 的留白刚好。")
print(catalog.short_view(selected=item.short_id).text)
png_bytes = catalog.new_turn(image_limit=3).read(item.short_id).image
```

写入新图时可用 `history.save(request_id, png=..., label=...)` 同时完成保存和登记。重试同一次写入时沿用同一请求 ID 和相同输入。短目录默认预算为 400 token，接入端可传入 `count_tokens` 实测。DSH 的按短号回看和自动补入目录需要单独加载任务历史插件，绑定会话 ID、任务和数据目录。[插件配置示例](docs/test-results/20261120-vista-image-history/host.md#接入方式) · [日常会话实测](docs/test-results/20261120-vista-dsh-daily/README.md)

## 开发与验证

在单独克隆的仓库中，可以运行以下检查，覆盖 Python 存取、路径竞态、MCP 工具和任务内历史。

```bash
uv sync --locked --extra mcp --group dev
uv run --no-sync pytest tests/test_core.py tests/test_path_races.py tests/test_mcp.py tests/test_history_store.py tests/test_history_catalog.py
uv run --no-sync ruff check src tests scripts examples
uv build
```

要跑完整 Python 测试，另需一份带 `runtime/vista_bridge.py` 的 `dsh-shader` 业务仓库。将 `VISTA_BUSINESS_ROOT` 指向其根目录后运行 `uv run --no-sync pytest`。本地快速启动和上面的独立库检查都可以单独完成。

## 设计与实验记录

- 图片、可选代码、记录发布顺序和调用约定见[最小图片记录设计](docs/vista-minimal-image-records-design-20261113.md)和[实施计划](docs/vista-memory-implementation-plan-20261113.md)。需要跨进程补登记的写入方可预留 UUID 作为 `record_id`，恢复时先核对实际内容。[业务接入恢复清单](docs/test-results/20261117-png-shader-recall/implementation-checklist.md)
- [基础回归记录](docs/test-results/20261116/README.md)和[任务历史实施报告](docs/test-results/20261120-vista-image-history/README.md)给出了各阶段的测试环境、覆盖范围和已知限制。
- 48 次合成任务对照中，文字历史组成功率为 75%，原图回读组为 100%。后续 Shader 业务诊断中，明确要求回顾时能取回图和代码，仅增强历史说明的自然任务仍未触发旧图回读。[合成实验报告](docs/test-results/20261116-eval/README.md) · [业务诊断](docs/test-results/20261117-png-shader-recall/focused-followup/results.md)
