# 原生 DSH 日常回看实测

**两套稿都能走通保存、评价、切换当前稿和裁剪后回读。** Agent 两次都先选对获认可稿，读到原 PNG 后答对图案。停手结果有差别。两次改写都没有读图，两次近似稿都只读一张后主动停手，明确没保存的稿件仍触发了多余回读。[固定测试方案](protocol.md) · [逐例证据](evidence/scores.json)

## 日常入口与压缩覆盖

通过原生 `session/prompt` 发送附件和文本。可选入口插件把 `保存图片：标签` 和 `评价 Rn：正文` 转成 VISTA 保存和评价动作。共保存 8 张图，每张的短号回执和原 PNG 都进入了该保存回合的成功请求。两套分别确认评价写入 R2 和 R3，最后一张都由用户指定为当前稿，目录将 R4 标为最近保存。实测入口是隔离 `sdk-minimal` 的普通 SDK 消息，加载了日常入口插件和 VISTA 适配器。[套一入口审计](evidence/set-1/seed/entry.jsonl) · [套二入口审计](evidence/set-2/seed/entry.jsonl)

| 材料 | 日常交接消息 | 模型已上报的最高输入量 | 默认压力压缩 | 原生诊断检查点 |
| --- | --- | ---: | --- | --- |
| 潮声周末 | 发出 8 条，1 条遇到输出上限，1 条响应流超时 | 16,210 token | 未触发 | `/compact`，事件 122、123、125 |
| 林间夜读 | 8 条均完成 | 21,636 token | 未触发 | `/compact`，事件 114、115、117 |

DSH 0.2.2-rc.2 报告的上下文窗口是 1,048,576 token。沿用当前路由的 4,096 token 输出上限，宿主默认压力触发阈值约为 838,860 token，本轮输入量离阈值较远。默认自动摘要和独立滑窗仍待覆盖。第一套从原会话恢复后执行诊断，超时请求的 HTTP 200 及缺少响应尾部的情况保留在原始日志。每套 4 个题目分支都核对了恢复后的 DSH 会话日志与同一检查点逐字节前缀一致。[套一会话与回执](evidence/set-1/seed/seed.json) · [套二会话与回执](evidence/set-2/seed/seed.json)

## 选图、回答和停手

下表编号来自各分支的实际成功请求。首请求均未携带 PNG，且两个视觉题的首请求均未给出所问图案。回读入模用工具调用 ID 和原 PNG 的 SHA-256 核对。技术上允许每轮读 3 张。八个问题分支均完成，均未因读图或模型请求额度而中止。

| 材料和题目 | 首次目录入模 | 首次选图 | 成功送入模型的 PNG | 多打开 | 回答与停手 |
| --- | ---: | --- | --- | ---: | --- |
| [潮声周末 · 必看旧图](evidence/set-1/visual/wire.jsonl) | 16 | **R2** | 原 R2，第 17 请求 | 0 | 答对菱形，读一张后停手 |
| [林间夜读 · 必看旧图](evidence/set-2/visual/wire.jsonl) | 15 | **R3** | 原 R3，第 16 请求 | 0 | 答对四角星，读一张后停手 |
| [潮声周末 · 文字已够](evidence/set-1/rewrite/wire.jsonl) | 16 | 无 | 无 | 0 | 给出蓝色定稿的交付句，但还回答了上轮未完成的交接问题，整条回复超过一句 |
| [林间夜读 · 文字已够](evidence/set-2/rewrite/wire.jsonl) | 15 | 无 | 无 | 0 | 一句话完成改写 |
| [潮声周末 · 明确未存](evidence/set-1/absent-text/wire.jsonl) | 16 | R4 | R4、R2，第 18 请求 | 2 | 答对没有可交付稿，但不必要地读了两张 |
| [林间夜读 · 明确未存](evidence/set-2/absent-text/wire.jsonl) | 15 | R4 | R4，第 16 请求 | 1 | 答对没有可交付稿，但不必要地读了一张 |
| [潮声周末 · 有近似稿](evidence/set-1/absent-near/wire.jsonl) | 16 | **R3** | 近似稿 R3，第 19 请求 | 0 | 核查一张后确认没有头像，主动停手 |
| [林间夜读 · 有近似稿](evidence/set-2/absent-near/wire.jsonl) | 15 | **R1** | 近似稿 R1，第 18 请求 | 0 | 核查一张后确认没有头像，主动停手 |

第一套受前一回合响应超时影响，若按整条回复只有一句验收，改写题严格通过 **1/2**，零回读 **2/2**。明确未存稿的两题虽都正确说明不存在，零回读 **0/2**。有近似稿的两题都是只读一张相关原图后主动停手。[固定题面、素材哈希和答案](evidence/frozen.json)

## 目录为何又出现

两份 `/compact` 摘要都让旧 PNG 和原保存回执退出，但仍用自然语言保留了“R2 是参照稿”或“R3 是参照稿”。八个分支的首请求均补入目录。按“最后一条有效线索也退出才补”的验收规则，这两套首轮目录属于多余补入。这轮也无法把两次正确回读单独归因于目录，因为摘要已经能指明应读哪张。[两个视觉题首请求与摘要](evidence/scores.json)

两个视觉分支随后继续问了一次无关的短问题，原目录随请求保留，新增补入均为 0。第一套的两道无图题各多补了一次。两次都紧跟 `vista_history_observe` 成功写入新观察，出站同时携带原目录和新视图。观察写入后的版本变化与这两次再次补入对应。[明确未存题第 19 请求](evidence/set-1/absent-text/wire.jsonl) · [近似稿题第 20 请求](evidence/set-1/absent-near/wire.jsonl)

## 复核与复现

```bash
uv run python scripts/score_vista_history_daily.py /tmp/vista-daily-scores.json \
  docs/test-results/20261120-vista-dsh-daily/evidence
uv run --with tokenizers==0.23.3 python scripts/run_vista_history_daily.py \
  /tmp/vista-daily-new-run --tokenizer /path/to/tokenizer.json
```

第一条命令离线核对目录、读图和会话前缀，第二条使用本机可用的原生 DSH 账号发起真实请求。新试验目录可以用 `--only set-1` 或 `--only set-2` 只跑一套。复现运行使用现有的 DSH 凭据配置，或用 `--credentials-file` 指定。本地回归通过 126 项 Python 测试和 4 项与 DSH 回看有关的 Node 测试，相关源代码通过 Ruff。
