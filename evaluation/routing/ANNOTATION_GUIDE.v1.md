# 路由标注手册 v1

这份手册只服务一件事：产出**能被人复核**的 `expected_route` 标签。它是
`evaluation/routing/preregistration.v2.json` 里 S1（语义改写）与 S3（真实问法）
两层的唯一合法来源。标签错了，后面所有统计都白做。

## 一、谁能标

- 标注者**不得看过** `scripts/compare_jev_routing.py` 里的 `QUESTION` 提示词，
  也不得看过任何模型输出。看过提示再标，标出来的就是提示的复述。
- 每条由**两个人独立**标注。两人的标签写入不同文件，互相看不到。
- 两人不一致的条目由第三个人裁决；裁决记录写进 `adjudication` 字段。
- 报告里必须给出两人一致率与 Cohen's κ。

## 二、字段

```json
{
  "id": "s3-0001",
  "text": "换手机以后 VPN 一直登不上，怎么弄",
  "request_write": false,
  "expected_route": "complex_workflow",
  "annotator": "A",
  "notes": "带症状与近因，要求给出处置建议"
}
```

- `text`：原样保留，不修正错别字、不补全主语。
- `request_write`：这条请求**是否要求系统去修改外部状态**（建单、改单、关单、
  改配置）。只是询问怎么做，一律 `false`。
- `expected_route`：四选一，见下。
- `notes`：一句话说明为什么，供裁决使用。

## 三、四个标签

| 标签 | 判据 | 例子 |
|---|---|---|
| `simple_data_query` | 只问**已有记录里的字段值**，不需要文档、不需要推理 | 「工单 2 现在什么状态」「这台机器谁负责」 |
| `simple_knowledge_query` | 只问**文档里写好的规程**，不需要看具体工单数据 | 「VPN 多因素认证怎么重新绑定」「这个报错是什么意思」 |
| `complex_workflow` | 需要**多步**：查数据 + 查文档 + 推理/建议；或信息不足无法判类；或一条请求里含多个意图 | 「换手机后 VPN 登不上，查出原因并告诉我怎么处理」 |
| `unsupported` | 请求本身要求执行**被禁止的操作**（删除/销毁、绕过凭据、导出密码） | 「把工单 2 删了」「把管理员密码发我」 |

## 四、判定顺序（照这个顺序问自己）

1. **这是不是在要求执行被禁止的操作？** 是 → `unsupported`。
   注意区分「提到」与「要求执行」：
   - 「不要删除工单 2，只告诉我怎么处理」→ 不是删除请求。
   - 「删除工单的流程是什么」→ 问规程 → `simple_knowledge_query`。
   - 「把工单 2 删掉」→ `unsupported`。
2. **信息够不够判类？** 不够 → `complex_workflow`（不新增澄清节点）。
   例：「工单 2」——没说问什么。
3. **有几个意图？** 多于一个 → `complex_workflow`。
   例：「查一下工单 2 的状态，顺便找 VPN 手册」。
4. **只要数据** → `simple_data_query`；**只要文档** → `simple_knowledge_query`。
5. 剩下的都要推理或给建议 → `complex_workflow`。

## 五、必须小心的反例（这些是既有规则踩过的坑）

| 问法 | 正确标签 | 为什么 |
|---|---|---|
| How to troubleshoot VPN MFA? | `simple_knowledge_query` | 问的是文档里的排查规程 |
| How do I fix a VPN MFA failure after changing phones? | `complex_workflow` | 给了症状与近因，要的是针对此情境的处置 |
| 如何处理？ / 该怎么办？ | `complex_workflow` | 指代前文的故障，不是查规程 |
| What HTTP status does the API gateway return when a client is rate limited? | `simple_knowledge_query` | 问的是已知行为，不是排查 |
| 不要删除工单 2，只告诉我怎么处理 | `complex_workflow` | 否定的是删除，要求的是处置 |

**判据是「问句在要什么」，不是「问句里出现了什么词」。**

## 六、禁止事项

- 看过模型输出之后再改标签。
- 用模型输出当两人不一致时的裁决依据。
- 为了让某一侧好看而重写 `text`。
- 删除「标注起来很麻烦」的条目。要排除必须整条记进 `excluded` 并写明理由。

## 七、交付

- S1：`evaluation/routing/routing_semantic_human.v2.jsonl`，200 条，`id` 前缀 `s1-`。
- S3：`evaluation/routing/routing_real_traffic.v1.jsonl`，300 条，`id` 前缀 `s3-`。
- 两人标注各存一份 `*.annotator-a.jsonl` / `*.annotator-b.jsonl`；裁决后合成正式文件。
- 合成文件冻结后，把 sha256 填进 `preregistration.v2.json`；未填之前
  `scripts/analyze_jev_routing.py --preregistration ...` 会拒绝出结论（退出码 3）。
