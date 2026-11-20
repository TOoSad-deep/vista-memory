# 2026-11-16 DSH SDK 受控验收

本轮通过安装版 DeepSeek Harness `0.2.2-rc.2` 的 SDK runtime 执行 D1 至 D8。共完成 12 个真实模型回合、11 个隔离会话和 4 次 SDK 进程启动。31 次模型 HTTP 请求均返回 200。

自动核验结果为 12 个案例全部通过，简单视觉回答已按参考图和确定性夹具核对。[自动核验结果](assessment.json)、[视觉回答核对](visual-review.json)

测试采用独立 `sdk-minimal` profile，沿用桌面配置的 `openai` / `gpt-6.1-sol` 路由。模型可调用工具始终为 `mcp__vista__list_records` 和 `mcp__vista__read_record`。本轮操作由后台脚本完成。

## 验收结果

| 项目 | 实测结果 | 运行证据 |
| --- | --- | --- |
| D1 目录与默认图片读取 | 先列目录，随后省略 `content` 读取参考图，正确描述中心浅灰圆及深蓝背景 | [D1 轨迹](run-d1.json) |
| D2 三种模式 | 三个独立会话分别执行 `image`、`code`、`both`，对应图片、完整 GLSL 和参数准确返回 | [模式与动态记录轨迹](run-modes-live.json) |
| D3 无关联代码 | 参考图的 `code` 正常返回无关联代码，`both` 同时返回图片 | [模式与动态记录轨迹](run-modes-live.json) |
| D4 共享代码与新版本 | r-b 与 r-c 代码相同，各自图片和半径准确，r-d 返回新代码版本 | [模式与动态记录轨迹](run-modes-live.json) |
| D5 运行中追加 | 同一会话先看到 4 条，程序追加后直接读取新记录，再列目录得到 5 条 | [模式与动态记录轨迹](run-modes-live.json) |
| D6 新会话与新进程回读 | 新 SDK 与 MCP 进程中的新会话从目录找到已有参考图，图片准确返回 | [重启轨迹](run-restart.json) |
| D7 错误恢复 | 缺失记录、损坏 JSON、缺失 PNG 均明确报错，每次随后有效读取成功 | [错误恢复轨迹](run-errors.json) |
| D8 实际出站图像与代码 | 5 张夹具图片在实际请求中的哈希与原 PNG 一致，请求代码时完整正文进入出站内容 | [HTTP 摘要](http-audit.jsonl) |

D2 的 code-only 会话全部出站请求包含零张图片。D7 的缺失图片场景随后成功读取同一记录的代码，符合按请求内容访问的合同。

DSH 将相邻 MCP 文本块合并为一段文本。核对时先解析 JSON 元数据，再逐字符比较完整 GLSL，图片块按附件哈希核对。

## 图片证据

HTTP 审计插件在提供方请求已序列化后包装 `globalThis.fetch`，从实际 `input_image.image_url` 解码图片并计算 SHA-256。原 `input` 和 `init` 原样转交给原 fetch，日志记录 MIME、尺寸、哈希、工具名称和响应状态。

参考图 r-a 的原 PNG、DSH 附件以及模型出站图片均为 96 × 96 PNG，389 字节，SHA-256 为 `ddadc310e93934a77c170d501b217eb8b61ccd32b167a600346789793ddd708c`。其余图片的对应关系保存在夹具清单中。[夹具清单](fixtures.json)

![本轮参考图](r-a.png)

出站请求同时记录了所含完整代码的哈希。目录、tool-result 与模型回答来自实时 JSON-RPC 事件，图片传输依据来自 HTTP 审计摘要。

## 模型配置与运行范围

初始化设置为 `reasoningEffort=high`、`maxTokens=8192`。该版本 pi-ai 对本模型使用中途配置更新，顶层 `reasoning.effort` 固定为 `medium`，实际 `high` 通过 `input` 中的 `configuration_update` 发送。后两次 SDK 启动已在 HTTP 摘要中记录该字段，内容为 `high`。

12 个回合的 DSH usage 合计为输入 12,793、输出 2,549、缓存写入 2,752、总计 18,094 tokens。费用金额沿用提供方账单。

本轮使用确定性 PNG 和固定 GLSL 存取夹具，覆盖记录对应关系、宿主传输与简单识图。真实 Shader 执行、下一步动作的视觉效果和 48 次推理收益对照实验仍按测试规划执行。

## 复现入口

运行器接收独立 home、覆盖配置、工作目录、案例文件和输出路径。

```bash
.venv/bin/python scripts/dsh_sdk_run.py \
  --home /private/tmp/vista-dsh-acceptance-20261116-nuo9zod_/home \
  --overlay docs/test-results/20261116-dsh/sdk-overlay.json \
  --workspace /private/tmp/vista-dsh-acceptance-20261116-nuo9zod_/workspace \
  --cases docs/test-results/20261116-dsh/cases-d1.json \
  --output /private/tmp/vista-dsh-d1-rerun.json
```

路径和会话 ID 记录的是本轮运行值。重新执行时准备新的夹具目录与会话 ID，并同步覆盖配置。D5 案例会在 SDK 运行期间追加一条记录。

运行器依据 `turn/end.reason.kind=completed` 检查回合结束，再由验收脚本核对实际工具输出和 HTTP 摘要。

```bash
.venv/bin/python scripts/check_dsh_acceptance.py \
  --directory docs/test-results/20261116-dsh
```

测试凭据通过现有 DSH credential provider 在进程内解析。覆盖配置保留凭据引用，HTTP 审计保存派生摘要。原始 HTTP 请求体和认证头保留在运行时传输路径中。

完整执行清单见[测试规划](../../vista-memory-test-plan-20261113.md)。
