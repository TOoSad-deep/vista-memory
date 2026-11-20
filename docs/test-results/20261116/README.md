# 2026-11-16 基础回归与 DSH 后台入口验证

基础回归通过，DSH 安装包内的 SDK 入口完成了程序化初始化、创建测试会话和关闭。独立测试会话的工具集合恰好包含两项 VISTA 工具。

本轮通过终端脚本执行。DSH 探针使用独立临时 `DSH_HOME` 和工作目录，实际模型调用、图像出站检查及推理收益实验仍列在后续清单。

## 基础回归结果

| 检查 | 实测结果 | 证据 |
| --- | --- | --- |
| 锁定依赖同步 | 离线同步成功，MCP 与开发依赖可用 | [依赖日志](dependencies.log) |
| 核心、路径竞态与 MCP | 79 项通过，0 项失败、0 项跳过，耗时 3.33 秒 | [pytest 日志](pytest.log)、[JUnit 结果](pytest.xml) |
| 连续记录第 1 次 | 0 → 4 条，运行中追加和新服务进程回读通过 | [第 1 次日志](roundtrip-1.log) |
| 连续记录第 2 次 | 4 → 8 条，本次新增 4 条在运行中及服务重启后准确回读 | [第 2 次日志](roundtrip-2.log)、[目录与计数](roundtrip.json) |
| 描述符释放 | 100 次成功读取与 100 次预期失败后，打开描述符数量保持 4 | [探针结果](descriptor-check.json) |
| 静态检查与格式 | Ruff 通过，11 个文件符合格式 | [检查日志](ruff.log)、[格式日志](format.log) |
| 锁文件一致性 | `uv lock --check --offline` 通过 | [锁文件日志](lock.log) |
| 构建 | wheel 与源码分发包构建成功 | [构建日志](build.log) |

代码提交为 `48a575a15b2767ad07166c8004fea9cd545d1d46`。执行时工作区包含测试规划与进度文档改动。环境为 macOS、Python 3.11.15、Pillow 13.0.0、MCP SDK 2.3.0、pytest 9.2.0 和 Ruff 0.17.5。环境、源码与锁文件哈希保存在清单中。[环境清单](environment.json)

连续记录示例使用 Pillow 生成图片，GLSL 是固定存取夹具。此次结果覆盖存储对应关系和 MCP 传输。真实 Shader 执行与推理任务评分按后续阶段执行。

## DSH 后台入口实测

本机安装版本为 DeepSeek Harness `0.2.2-rc.2`。安装包提供以下 CLI。

```text
/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh
```

`--profile sdk-minimal` 提供逐行 JSON-RPC stdio。`DSH_HOME` 指向独立临时目录，`--patch` 加载测试覆盖配置。本轮调用顺序如下。

1. `initialize` 设置测试工作目录，并解析模型路由配置。
2. `session/working-directory/get` 为新 `sessionId` 创建 Agent。
3. 探针插件通过 `ctx.tools.schemas(agent)` 输出当前工具定义名称。
4. `shutdown` 关闭 SDK 与 MCP 进程。

两轮探针均以退出码 0 结束。第一轮验证初始化与关闭，第二轮加载真实 VISTA MCP 服务并创建 Agent。第二轮枚举结果如下。[握手结果](dsh-sdk-probe.json)、[会话与工具结果](dsh-tools-probe.json)

```json
[
  "mcp__vista__list_records",
  "mcp__vista__read_record"
]
```

探针的路由字段采用 `deepseek-official` / `deepseek-chat`，用于初始化配置解析。本轮发出了初始化、工作目录查询和关闭请求。后续视觉测试需配置与既有桌面冒烟一致的 `gpt-6.1-sol` 路由，并实际验证模型调用。

stderr 出现 Electron 的 `task_name_for_pid` 诊断。两轮 JSON-RPC 均正常响应，第二轮工具发现成功。完整诊断保留在对应结果文件。

测试插件和覆盖配置已保存。覆盖配置中的路径记录本轮实际临时目录，重现时应替换为新测试目录。[探针插件](dsh-tools-probe.mjs)、[覆盖配置](dsh-probe-overlay.json)

## 后续执行方式

后台运行使用安装包中的同版本 DSH SDK runtime。CLI 的 `desktop` profile 由 Electron 桌面宿主管理，批量实验采用独立 SDK profile。桌面会话行为继续使用既有桌面验证证据及后续定点验收。

安装包源码提供 `session/prompt`、`session/wait`、会话通知与查询接口，可用于组织后续独立运行。当前已完成初始化和会话创建探针，批量 prompt、等待、超时与评分驱动还需实现和验证。

DSH 日志中的 `request/header`、`request/context` 以及附件记录处于内部请求表示层。提供方适配器随后生成实际模型请求。D8 将在测试进程的传输层记录出站图片 MIME、哈希和来源对应关系，并对必要证据脱敏。

- [x] 完成本轮基础回归和构建。
- [x] 验证独立 DSH SDK 初始化、会话创建和关闭。
- [x] 验证测试 Agent 的可用工具恰好为两项 VISTA 工具。
- [ ] 配置并验证实际视觉模型路由。
- [ ] 完成 DSH D1 至 D8 的受控场景与出站图像检查。
- [ ] 准备冻结案例，执行校准与推理收益对照实验。

后续具体操作和评分规则沿用[测试规划与执行清单](../../vista-memory-test-plan-20261113.md)。
