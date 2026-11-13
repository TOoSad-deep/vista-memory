# vista-memory

VISTA 视觉记忆，以一张图片及其可选对应代码为记录单位，提供独立存取核心与 MCP 读取接口。

## 安装

需要 Python 3.11 及以上。使用 uv 安装开发依赖与 MCP 适配层。

```bash
uv sync --extra mcp --group dev
```

通过 pip 安装时，`pip install .` 提供存取核心，`pip install '.[mcp]'` 同时安装 MCP 依赖。

## 保存与回读

调用方绑定运行时数据目录，核心在其中维护 `png/`、`glsl/` 和 `json/`。每份 JSON 对应一张 PNG 和可选 GLSL，记录文件名由核心生成。

```python
from pathlib import Path

from vista_memory import VistaStore

store = VistaStore("/absolute/path/to/vista")
reference = store.save_record(
    png=Path("reference.png").read_bytes(),
    label="参考图",
)
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

print(store.list_records())
result = store.read_record(second["record"], content="both")
print(result.metadata)
print(result.code)
png_bytes = result.image
```

调用方提供本次实际 PNG、完整代码和生效的外部参数。复用代码时传入同库已有记录文件名 `reuse_glsl_from`，它与 `glsl` 互斥。代码变化时传入新的 `glsl` 正文。每次保存追加新记录，旧记录引用的文件保持稳定。

保存成功返回 `record`、`label`、`source_agent` 和 `has_glsl`。`read_record()` 默认读取图片，支持 `image`、`code`、`both`。返回对象含 `metadata`、`image` 和 `code`，其中 `image` 是 PNG 字节，`code` 是 GLSL 字符串，未选择的内容为 `None`。参考图的 `code` 为 `None`，空代码正文保留为空字符串。

核心使用 `VistaError` 报告无效输入、损坏记录和文件访问错误。目录按文件名排序，遇到损坏 JSON 时报告具体记录并使本次列出失败。内容文件按读取模式访问。保存以完整 JSON 的原子发布为完成时点，发布前失败会尽力清理本次独有文件。

## MCP 启动

```bash
uv run --extra mcp vista-memory-mcp --root /absolute/path/to/vista
```

也可使用 `python -m vista_memory.mcp --root /absolute/path/to/vista`。MCP 客户端通过 stdio 启动该命令，标准输出承载协议，日志写入标准错误。

| 工具 | 输入 | 结果 |
| --- | --- | --- |
| `list_records()` | `{}` | 记录摘要目录的 JSON 文本 |
| `read_record()` | `{"record": "记录.json", "content": "both"}` | 元数据文本及按需返回的 GLSL 文本和 PNG 图像内容 |

`content` 默认 `image`，支持 `code` 和 `both`。无关联代码时，代码请求返回“没有关联代码”；`both` 同时返回图片。图片内容通过 MCP 图像内容块传输。

## 本地示例与验证

```bash
uv run --extra mcp python examples/local_roundtrip.py --root /tmp/vista-example
uv run pytest tests/test_core.py
uv run --extra mcp pytest tests/test_mcp.py
uv run ruff check .
uv run ruff format --check .
uv build
```

示例按预设参数生成 PNG，连续登记参考图、共享代码和新代码版本，通过真实 MCP 子进程检查目录、三种读取模式、图片字节、代码正文和参数，再启动新连接回读。GLSL 使用固定存取测试正文，Shader 执行与视觉效果在业务接入时验证。重复运行示例会向指定目录追加记录。

## 设计与进度

详见[最小图片记录与 MCP 接入设计](docs/vista-minimal-image-records-design-20261113.md)和[实施计划](docs/vista-memory-implementation-plan-20261113.md)。

首轮独立存取核心、MCP 读取接口和本地协议验收已完成，71 个测试通过，连续记录与重启回读示例通过，构建及独立安装检查通过。DSH 宿主、视觉模型图像入模与 Shader Deep 业务接入安排在后续阶段。
