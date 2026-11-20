# 原生 DSH 回看目录配对验收

两套合成图标共 40 个回合。26 个配对的首请求都符合各自的投递条件，38 个回合给出最终回答，2 个达到预设请求上限。分层后，两套折叠题都少查了两次目录。主动提醒在两个配对中的结果不一致。

## 冻结条件与判分

每套 43 张 PNG。各组共用原图、原标签、题面、已裁剪的 DSH 检查点和工具定义。第二套交换高光稿与边缘稿的 R2、R5 短号。A 使用平铺目录，B1 使用分层目录，B2 在同一分层算法上加入来源备注。每套的查询组与提醒组共用一个冻结 B2 视图。两组都能按需查询、翻页、读图，只有提醒组的首请求包含目录。

原生 DSH 使用 `deepseek-account` 的 `deepseek-flash` 路由。每回合最多 7 次模型请求、3 次成功回读，目录上限 400 token。用 DeepSeek-V4.1-Flash 官方词表计数，包含视图号、展开说明及入口。输入 token 合计 DSH 上报的普通输入、缓存读取和缓存写入，表中另列缓存读取。首请求比较归一化临时目录和上传文件号，再移除本条件的目录文字。40 个首请求均只携带当前稿 PNG。检查点由测试脚本受控替换。[官方词表](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/tokenizer.json) · [实际宿主裁剪证据](host.md)

判分先看第一次原图请求的短号，再用工具调用 ID 和 PNG 哈希核对成功的模型请求，最后复核回答。方向题看指定图的缺口，无图题接受基于有限证据说明未找到。各回合的工具请求、模型请求和回答可单独复核。[冻结清单与完整短目录](paired-evidence/frozen.json) · [逐请求汇总](paired-evidence/scores.json) · [回答复核](paired-evidence/answer-review.json)

## 主动提醒与按需查询

| 材料 | 投递方式 | 目录首次入模 | 回读顺序 | 正确 PNG 首次入模 | 目录调用 / 无关回读 / 额外往返 | 输入 token / 缓存命中 | 回答状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [套 1](paired-evidence/runs/set-1-delivery-query/wire.jsonl) | 仅查询 | 第 2 请求 | R3、R42、R2 | 第 6 请求 | 5 / 2 / 5 | 15,164 / 12,416 | 已答 |
| [套 1](paired-evidence/runs/set-1-delivery-auto/wire.jsonl) | 主动提醒 | 第 1 请求 | R43、R41、R36 | 未送达 | 2 / 3 / 2 | 7,031 / 4,736 | 已答 |
| [套 2](paired-evidence/runs/set-2-delivery-query/wire.jsonl) | 仅查询 | 第 2 请求 | R43、R5、R3 | 第 4 请求 | 3 / 2 / 4 | 10,621 / 7,936 | 已答 |
| [套 2](paired-evidence/runs/set-2-delivery-auto/wire.jsonl) | 主动提醒 | 第 1 请求 | R43、R5、R3 | 第 4 请求 | 6 / 2 / 6 | 22,352 / 18,944 | 达到请求额度 |

第一套主动提醒减少三次模型请求，但漏读 R2。第二套的两个条件都在第 4 次请求收到 R5，主动提醒随后耗尽模型请求额度。四个回合都没有第一张就选中目标。

## 平铺、分层与备注

| 条件 | 视觉题首图命中 | 正确 PNG 入模 | 视觉回答正确 | 目录调用 | 成功回读 | 无关回读 | 额外往返 | 输入 token | 缓存命中 token |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 6/6 | 6/6 | 4/6 | 42 | 28 | 18 | 42 | 141,229 | 111,744 |
| B1 | 5/6 | 6/6 | 5/6 | 37 | 26 | 16 | 35 | 109,073 | 83,968 |
| B2 | 6/6 | 6/6 | 6/6 | 31 | 25 | 17 | 32 | 98,995 | 74,880 |

- B1 对 A。两套折叠题的目录调用都从 3 次减到 1 次。额外模型往返分别从 4 次减到 2 次、从 4 次减到 3 次。B1 在第一套相似图先打开边缘稿并以它作答。A 的第一套相似图达请求上限，第二套虽读了两张校准稿，仍把高光稿归属留待用户确认。
- B2 对 B1。两套相似图都凭备注区分了高光稿与边缘稿，第一次回读就命中，并答对方向。第二套 B1 也答对，当时可见的目录没有能确认归属的备注。B2 总目录调用和输入 token 更少，无关回读多 1 次。
- 六个文字已够的颜色回合都额外回读。两类无图题共 12 个回合，都回答未找到目标，并用满 3 张回读额度。实际被额度拒绝与刚好用满后结束分别记录。

两次请求额度中止分别在第一套相似图 A 和第二套主动提醒。后续请求尝试由本地预算拦截。正式发出的请求都有 HTTP 200 回执和用量。另有 36 次读图额度拒绝和 7 次目录入口错误。第一套相似图 B1 的首跑收到 HTTP 200 后响应流超时，保持冻结条件仅补跑这一回合。[超时首跑](paired-evidence/retry-evidence/set-1-similar-B1-stream-timeout/wire.jsonl)

### 第一层实际显示

| 材料 | 组别 | 选中与代表短号 | 展开入口和成员数 | 实测 token |
| --- | --- | --- | --- | --- |
| set-1 | A | R43、R3、R1、R42、R41、R40、R39、R38、R37、R36、R35、R34、R33、R32、R31、R30、R29、R28、R27、R26、R25、R24、R23 | rest（20） | 398 |
| set-1 | B1 | R43、R3、R42、R41、R40、R39、R38、R37、R5 | G1（2）、G2（3）、G3（7）、G4（7）、G5（7）、G6（7）、G7（7）、G8（2） | 282 |
| set-1 | B2 | R43、R3、R5、R42、R41、R40、R39、R38、R37 | G1（2）、G2（2）、G3（3）、G4（7）、G5（7）、G6（7）、G7（7）、G8（7） | 294 |
| set-2 | A | R43、R3、R1、R42、R41、R40、R39、R38、R37、R36、R35、R34、R33、R32、R31、R30、R29、R28、R27、R26、R25、R24、R23 | rest（20） | 396 |
| set-2 | B1 | R43、R3、R42、R41、R40、R39、R38、R37、R5 | G1（2）、G2（3）、G3（7）、G4（7）、G5（7）、G6（7）、G7（7）、G8（2） | 278 |
| set-2 | B2 | R43、R3、R5、R42、R41、R40、R39、R38、R37 | G1（2）、G2（2）、G3（3）、G4（7）、G5（7）、G6（7）、G7（7）、G8（7） | 298 |

两套折叠题的目标都是 R4。分层第一层只露该主题及较新的代表图，展开可见 R4。平铺第一层通过其余图片入口可展开到同一目标。

### 逐例结果

当前稿从首请求开始可见。下表只计后续工具回读。PNG 请求编号指与目标图工具调用匹配的第一条成功模型请求。

| 回合 | 回读顺序 | 正确 PNG 请求 | 回答 | 目录调用 / 无关回读 / 额外往返 | 输入 token / 缓存命中 | 停止方式 |
| --- | --- | --- | --- | --- | --- | --- |
| [套 1 需要旧图 A](paired-evidence/runs/set-1-visual-A/wire.jsonl) | R3、R1 | 2 | 正确 | 1 / 1 / 2 | 6,628 / 4,480 | — |
| [套 1 需要旧图 B1](paired-evidence/runs/set-1-visual-B1/wire.jsonl) | R3 | 2 | 正确 | 1 / 0 / 1 | 3,308 / 2,176 | — |
| [套 1 需要旧图 B2](paired-evidence/runs/set-1-visual-B2/wire.jsonl) | R3 | 3 | 正确 | 1 / 0 / 2 | 4,887 / 3,584 | — |
| [套 1 文字已够 A](paired-evidence/runs/set-1-text-A/wire.jsonl) | R43、R3 | — | 正确 | 1 / 2 / 2 | 6,694 / 4,992 | — |
| [套 1 文字已够 B1](paired-evidence/runs/set-1-text-B1/wire.jsonl) | R43 | — | 正确 | 0 / 1 / 1 | 3,336 / 2,176 | — |
| [套 1 文字已够 B2](paired-evidence/runs/set-1-text-B2/wire.jsonl) | R43、R3 | — | 正确 | 1 / 2 / 2 | 6,486 / 4,864 | — |
| [套 1 折叠 A](paired-evidence/runs/set-1-folded-A/wire.jsonl) | R4 | 5 | 正确 | 3 / 0 / 4 | 11,288 / 9,216 | — |
| [套 1 折叠 B1](paired-evidence/runs/set-1-folded-B1/wire.jsonl) | R4 | 3 | 正确 | 1 / 0 / 2 | 4,714 / 3,584 | — |
| [套 1 折叠 B2](paired-evidence/runs/set-1-folded-B2/wire.jsonl) | R4 | 3 | 正确 | 1 / 0 / 2 | 4,727 / 3,456 | — |
| [套 1 相似 A](paired-evidence/runs/set-1-similar-A/wire.jsonl) | R2、R5、R1 | 6 | 额度中止 | 10 / 2 / 6 | 20,559 / 17,536 | 请求额度中止 |
| [套 1 相似 B1](paired-evidence/runs/set-1-similar-B1/wire.jsonl) | R5、R2、R3 | 3 | 选错高光稿 | 2 / 2 / 3 | 9,749 / 7,680 | — |
| [套 1 相似 B2](paired-evidence/runs/set-1-similar-B2/wire.jsonl) | R2、R3、R1 | 3 | 正确 | 3 / 2 / 3 | 9,873 / 7,680 | — |
| [套 1 无目标／文字 A](paired-evidence/runs/set-1-absent-text-A/wire.jsonl) | R3、R1、R42 | — | 正确 | 4 / 3 / 4 | 14,427 / 11,264 | 读图额度拒绝 |
| [套 1 无目标／文字 B1](paired-evidence/runs/set-1-absent-text-B1/wire.jsonl) | R3、R1、R42 | — | 正确 | 4 / 3 / 4 | 13,129 / 10,624 | 读图额度拒绝 |
| [套 1 无目标／文字 B2](paired-evidence/runs/set-1-absent-text-B2/wire.jsonl) | R3、R1、R42 | — | 正确 | 9 / 3 / 3 | 11,685 / 8,320 | 读图额度拒绝 |
| [套 1 无目标／近似 A](paired-evidence/runs/set-1-absent-near-A/wire.jsonl) | R3、R43、R1 | — | 正确 | 5 / 1 / 4 | 15,670 / 12,672 | 读图额度拒绝 |
| [套 1 无目标／近似 B1](paired-evidence/runs/set-1-absent-near-B1/wire.jsonl) | R3、R1、R42 | — | 正确 | 8 / 1 / 3 | 10,919 / 7,936 | 读图额度拒绝 |
| [套 1 无目标／近似 B2](paired-evidence/runs/set-1-absent-near-B2/wire.jsonl) | R3、R42、R6 | — | 正确 | 4 / 2 / 4 | 13,025 / 10,496 | 读图额度拒绝 |
| [套 2 需要旧图 B2](paired-evidence/runs/set-2-visual-B2/wire.jsonl) | R3 | 2 | 正确 | 1 / 0 / 1 | 3,345 / 1,920 | — |
| [套 2 需要旧图 B1](paired-evidence/runs/set-2-visual-B1/wire.jsonl) | R3 | 2 | 正确 | 1 / 0 / 1 | 3,264 / 1,792 | — |
| [套 2 需要旧图 A](paired-evidence/runs/set-2-visual-A/wire.jsonl) | R3 | 3 | 正确 | 2 / 0 / 2 | 5,260 / 3,584 | — |
| [套 2 文字已够 B2](paired-evidence/runs/set-2-text-B2/wire.jsonl) | R43、R3、R1 | — | 正确 | 1 / 3 / 3 | 10,059 / 7,808 | — |
| [套 2 文字已够 B1](paired-evidence/runs/set-2-text-B1/wire.jsonl) | R43、R3 | — | 正确 | 2 / 2 / 3 | 8,824 / 6,784 | — |
| [套 2 文字已够 A](paired-evidence/runs/set-2-text-A/wire.jsonl) | R43、R3、R1 | — | 正确 | 1 / 3 / 2 | 7,809 / 5,376 | — |
| [套 2 折叠 B2](paired-evidence/runs/set-2-folded-B2/wire.jsonl) | R4 | 3 | 正确 | 1 / 0 / 2 | 5,026 / 3,712 | — |
| [套 2 折叠 B1](paired-evidence/runs/set-2-folded-B1/wire.jsonl) | R4、R6、R42 | 3 | 正确 | 1 / 2 / 3 | 8,041 / 5,760 | — |
| [套 2 折叠 A](paired-evidence/runs/set-2-folded-A/wire.jsonl) | R4、R6 | 4 | 正确 | 3 / 1 / 4 | 11,810 / 9,600 | — |
| [套 2 相似 B2](paired-evidence/runs/set-2-similar-B2/wire.jsonl) | R5 | 3 | 正确 | 1 / 0 / 2 | 5,045 / 3,712 | — |
| [套 2 相似 B1](paired-evidence/runs/set-2-similar-B1/wire.jsonl) | R5、R2 | 3 | 正确 | 2 / 1 / 2 | 5,400 / 3,712 | — |
| [套 2 相似 A](paired-evidence/runs/set-2-similar-A/wire.jsonl) | R5、R2 | 5 | 未确认目标 | 8 / 1 / 5 | 17,586 / 14,592 | — |
| [套 2 无目标／文字 B2](paired-evidence/runs/set-2-absent-text-B2/wire.jsonl) | R3、R1、R4 | — | 正确 | 4 / 3 / 3 | 9,878 / 7,040 | 读图额度拒绝 |
| [套 2 无目标／文字 B1](paired-evidence/runs/set-2-absent-text-B1/wire.jsonl) | R3、R1、R2 | — | 正确 | 10 / 3 / 6 | 20,520 / 16,768 | 读图额度拒绝 |
| [套 2 无目标／文字 A](paired-evidence/runs/set-2-absent-text-A/wire.jsonl) | R3、R42、R1 | — | 正确 | 3 / 3 / 4 | 14,352 / 11,648 | 读图额度拒绝 |
| [套 2 无目标／近似 B2](paired-evidence/runs/set-2-absent-near-B2/wire.jsonl) | R3、R42、R6 | — | 正确 | 4 / 2 / 5 | 14,959 / 12,288 | 读图额度拒绝 |
| [套 2 无目标／近似 B1](paired-evidence/runs/set-2-absent-near-B1/wire.jsonl) | R3、R1、R42 | — | 正确 | 5 / 1 / 6 | 17,869 / 14,976 | 读图额度拒绝 |
| [套 2 无目标／近似 A](paired-evidence/runs/set-2-absent-near-A/wire.jsonl) | R3、R1、R42 | — | 正确 | 1 / 1 / 3 | 9,146 / 6,784 | 用满读图额度后结束 |

## 复现

```bash
uv run --with tokenizers==0.23.3 python scripts/run_vista_history_experiment.py /tmp/vista-history-pairs --tokenizer /path/to/tokenizer.json
uv run python scripts/score_vista_history_experiment.py /tmp/vista-history-pairs
```

使用能访问原生 DSH 账号的本机。脚本默认用 `DSH_HOME` 下的 `.credentials.yaml`，未设置时用 `~/.dsh/.credentials.yaml`。也可用 `--credentials-file` 指定路径。词表 SHA-256 随冻结清单保存。仅生成材料可加 `--prepare`，运行单例可加 `--only set-1-folded-B1`。再次运行会复用已完成的案例。若已有案例失败或中断，命令返回非零。加 `--retry-failed` 可将旧尝试归档到 `retry-evidence/` 后重跑。
