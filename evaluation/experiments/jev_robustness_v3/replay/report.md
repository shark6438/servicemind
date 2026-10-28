# 已有 Jev 观测的探索性回放

固定阈值：0.85。新增 API 调用：0，新增费用：0。
阈值及源结果已经暴露；本报告不构成独立留出验证。

| 数据 | n | 规则正确 | Jev 正确 | 混合正确 | 危险误分：规则 / Jev / 混合 |
| --- | ---: | ---: | ---: | ---: | --- |
| routing.jsonl | 107 | 107 | 106 | 107 | 0 / 1 / 0 |
| routing_hard.v1.jsonl | 41 | 36 | 35 | 40 | 4 / 4 / 0 |
| routing_semantic_probe.v1.jsonl | 64 | 30 | 64 | 60 | 5 / 0 / 0 |

## 解释边界

- The threshold and source results were previously observed; this is not a new held-out test.
- The semantic probe is assistant-authored, unreviewed and synthetic.
- All hybrid rows still require Jev inference to obtain its confidence; coverage is not API savings.
- No independent human annotation, production traffic, deployment or end-to-end savings measured.
