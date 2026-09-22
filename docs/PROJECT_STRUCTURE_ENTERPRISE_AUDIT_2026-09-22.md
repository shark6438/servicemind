# ServiceMind 企业级项目结构审计与冻结方案

> 审计日期：2026-09-22  
> 适用范围：`/home/shihongye/data1/servicemind` 的代码、包、进程与依赖结构  
> 结论：**结构门禁通过；采用“可安装的模块化单体 + 独立进程适配器”，不把实例 Agent 计入平台职责审计**

## 1. 裁定

当前结构适合 ServiceMind 的事务一致性与单机部署形态，不需要为追求“微服务”标签拆散
PostgreSQL 事务、RLS 和 outbox。冻结后的结构是：

```text
repository
├── src/servicemind/
│   ├── domain/              # 纯业务契约、调用身份、摘要完整性
│   ├── foundation/          # 跨上下文、无供应商依赖的基础原语
│   ├── orchestration/       # LangGraph 工作流、规划、恢复、治理
│   ├── interfaces/http/     # FastAPI 入站适配器
│   ├── context|rag|memory/  # 受治理的知识与上下文能力
│   ├── model_gateway/       # 模型策略、审计与调用适配器
│   ├── tool_platform|mcp/   # 工具治理与 MCP 协议边界
│   ├── persistence/         # 数据库、RLS、事务 outbox 仓储
│   ├── reliability/         # outbox relay 与恢复执行
│   └── integrations/        # GLPI 等外部系统适配器
├── src/service/             # FastAPI composition root (`create_app`)
├── src/pages/               # Streamlit 人工审核页面，包含在发行包中
├── src/agents|core|…        # 上游实例 Agent 运行壳；保留但不纳入平台职责评价
├── tests/                   # 通过已安装 editable distribution 导入
├── evaluation/              # 固定数据、实验快照与机器报告
├── deploy/systemd/          # API / Streamlit / outbox 三个版本化进程清单
└── pyproject.toml           # 构建、依赖、入口、测试与静态门禁唯一配置
```

这是一种模块化单体。业务事务仍在一个权威数据边界内，API、交互界面和 outbox worker
以独立进程运行。该结构同时保留进程级扩缩容与清晰的代码依赖方向。

## 2. 对照最新官方实践

| 官方实践 | 本项目落实 |
| --- | --- |
| PyPA `src` layout 要求测试和运行使用已安装分发，避免源码目录意外遮蔽漏包 | 新增 `setuptools.build_meta`、显式包发现和 editable 安装；删除 pytest `pythonpath=src` |
| PyPA 用 `[project.scripts]` 声明稳定进程入口 | 新增 `servicemind-api`、`servicemind-outbox`；systemd 不再注入 `PYTHONPATH` |
| FastAPI 大型应用用 `APIRouter` 拆分并由主应用组合 | 新增 `create_app`；Memory Review 移入 `interfaces/http/memory_review.py`，URL 与鉴权契约不变 |
| LangGraph 将步骤、原始状态、恢复和人审作为显式边界 | 规划与恢复收敛到 `orchestration/`；长期 Memory 继续与 checkpoint 分离 |
| Agent 平台把 guardrail、handoff、tool 与 trace 作为独立治理边界 | 现有 context / model / tool / handoff / approval 契约保持分离，实例 Agent 不拥有绕过能力 |
| OpenTelemetry GenAI 约定统一 workflow、agent、tool、usage 属性并警示内容敏感性 | 现有 span 使用 `gen_ai.agent.name` / `gen_ai.tool.*`；内容与提示默认不进入结构报告 |
| Twelve-Factor 明确依赖、环境配置、build-release-run 与进程类型 | lockfile、环境配置、可构建 wheel、三个进程清单分别落盘 |

官方依据：

- [PyPA：src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
- [PyPA：pyproject 与 entry points](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [FastAPI：Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [LangGraph：Thinking in LangGraph](https://docs.langchain.com/oss/javascript/langgraph/thinking-in-langgraph)
- [OpenAI Agents SDK：Agents、guardrails 与 tracing](https://openai.github.io/openai-agents-python/)
- [OpenTelemetry：GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [The Twelve-Factor App](https://12factor.net/)

## 3. 本轮发现并修复的结构缺陷

### 3.1 项目声明存在，实际不可安装

原 `pyproject.toml` 没有 build backend。`uv.lock` 把根项目标为 virtual，虚拟环境中
`importlib.metadata.distribution("servicemind")` 返回 `PackageNotFoundError`。pytest 通过
`pythonpath = ["src"]` 直接导入源码，因此漏打包不会让 CI 变红。

修复后：

- wheel 可构建；
- 虚拟环境存在 `servicemind==0.1.0` 元数据；
- 从 `/tmp`（不在仓库工作目录）可导入 `service.create_app`；
- console entry points 安装成功；
- pytest 使用 `--import-mode=importlib`，不再注入源码路径。

### 3.2 五个包级依赖环

修复前存在五个强连通分量：

1. `domain ↔ runtime` 的领域反向依赖；
2. `context ↔ rag` 的 tokenizer 归属错误；
3. `model_gateway ↔ runtime` 的调用身份归属错误；
4. `mcp ↔ tool_platform` 的传输适配器归属错误；
5. `persistence ↔ reliability` 以及 `agents / harness / orchestration` 的组合职责错位。

修复方式：

- `stable_digest` 与 `AgentInvocationContext` 下沉到 `domain/`；
- 离线安全 tokenizer 抽到 `foundation/`；
- MCP client/provider transport 归入 `tool_platform/`，`mcp/transport.py` 只保留兼容导出；
- outbox repository 归入 `persistence/`，relay 保留在 `reliability/`；
- dynamic planner 与 recovery 归入 `orchestration/`。

当前 `src/servicemind` 子包依赖图的强连通分量为 **0**。

### 3.3 API 与部署入口不可审计

Memory Review 的模型、查询构造和路由原先继续堆在总 API 文件。API 与 Streamlit 的
systemd unit 只存在于单机用户目录，仓库只跟踪 outbox unit；API/outbox 依赖
`PYTHONPATH=src`。

修复后：

- Memory Review 成为独立入站 HTTP adapter；
- `service.service:create_app` 是唯一 FastAPI composition root；
- API、Streamlit、outbox 三个 unit 全部版本化；
- API/outbox 使用已安装入口，unit 加入 `NoNewPrivileges` 与 `PrivateTmp`。

## 4. 机器门禁

`scripts/audit_project_structure.py` 生成：

- `evaluation/reports/project_structure_latest.json`
- `evaluation/reports/project_structure_latest.md`

`--check` 会拒绝以下漂移：

- build backend、包发现或 process entry point 消失；
- pytest 退回 `PYTHONPATH` 源码注入；
- ServiceMind 子包出现依赖环；
- domain 导入 runtime、database、HTTP 或供应商适配器；
- 生产源码导入 tests/scripts/evaluation 或修改 `sys.path`；
- 开发脚本通过 `PYTHONPATH=src` 或 `sys.path` 绕过已安装发行包；
- app factory、Memory Review adapter 或任一进程清单缺失。

复现：

```sh
.venv/bin/python scripts/audit_project_structure.py --check
uv build --wheel
cd /tmp
/home/shihongye/data1/servicemind/.venv/bin/python -c \
  'from importlib.metadata import version; from service import create_app; print(version("servicemind"), len(create_app().routes))'
```

本轮实跑结果：结构门禁 **13/13 PASS**，wheel 构建成功且包含 Memory Review 页面与新适配器，
全仓 **576 passed / 6 skipped / 0 failed**；Ruff 全绿、Pyrefly 0 error。真实 PostgreSQL
治理、Keycloak 身份漂移、lexical/TEI Memory 门禁与运行时核验均通过；API、Streamlit、outbox
三个 user unit 为 active，仓库清单与已安装 unit 逐字一致。

## 5. 诚实边界

1. 结构门禁证明代码依赖、打包和进程清单符合冻结规则，不证明模型输出、租户 RAG 质量或
   任一实例 Agent 的业务表现。
2. `src/agents|core|memory|schema|client|voice` 是上游工具包的承重运行壳。本轮不审计其中
   实例 Agent 的职责，也不做高风险大搬迁；显式包白名单避免未来把任意目录误装入发行包。
3. 当前构建是本机可复现 wheel 验证，尚未声称 SLSA Build L2/L3。签名 provenance 需要
   托管 CI 构建身份和外部签名基础设施，不能由本地代码修改冒充。
4. RAG 仍是 `DOMAIN_QUALITY_NOT_CERTIFIED`：项目结构通过不会改变租户六门禁
   `NOT_EVALUATED`，也不会把 TechQA 诊断结果写成生产质量提升。
