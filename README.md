# vista-memory

VISTA 视觉记忆，以一张图片及其可选对应代码为记录单位，提供独立存取核心与 MCP 读取接口。

## 设计

设计包含 PNG、可选 GLSL 和 JSON 元数据的保存与回读，以及 `list_records()` 和 `read_record()` 两个 MCP 工具。

详见[最小图片记录与 MCP 接入设计](docs/vista-minimal-image-records-design-20261113.md)。

## 当前进度

仓库初始化完成，设计文档已归档。下一步确定实施语言、依赖、MCP 启动方式与测试入口。
