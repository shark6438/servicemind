# 2026-10-22 定点安全审计证据

对应方案：[ServiceMind 安全测评实施方案](../../../docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md)。

源码基线：`19d9a377fb98e1c3f56edc59be40b374013bc08b`。通过 `git archive` 在不含真实 `.env` 的临时目录执行，使用项目已安装 Python 3.14 虚拟环境。未启动实际服务、未调用真实模型、数据库、GLPI 或身份服务。

| 文件 | 内容 |
| --- | --- |
| `summary.json` | 版本、测试数量、范围限制、观测和文件 SHA-256 |
| `audit-junit.xml` | 9 个文件的 113 项测试，0 失败、0 错误、0 跳过 |
| `pytest-output.txt` | pytest 原始输出与第三方弃用警告 |
| `audit_probes.py` / `audit-probes.json` | 时间戳、混合通过/跳过、BLOCKED 验收导入的定点观测 |
| `audit_http_probes.py` / `audit-http-probes.json` | 角色缺失与超长时间戳的 ASGI HTTP 观测 |

复现命令见方案 §9.1。必须在隔离源码副本执行，显式提供假凭据与不可用的测试数据库地址。HTTP 探针替换 OIDC dependency、Repository 和 CredentialCipher；返回的 canary 为合成数据。它验证路由控制流，不证明真实 token 可以伪造，也不证明生产数据已经泄漏。

探针用于记录缺陷，正常退出不表示安全通过。当前预期观测是：无角色主体读取详情和事件为 200，timeline 为 403；5000 位 Webhook timestamp 为 500；混合通过/跳过被聚合为 passed；BLOCKED 验收导入抛 AttributeError。修复后应新增安全期望断言，而非强制保留这些观测。

原始日志的时间戳采用执行主机时钟；审计日期与方案排期采用用户提供的 Asia/Shanghai 日期。本证据包不是完整的生产发布测评报告。
