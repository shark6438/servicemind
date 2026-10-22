# 2026-10-22 本地与模拟模型安全复测

本目录与 `audit_2026-10-22` 分开：后者是修复前缺陷观测，本目录是修复后证据。用户明确选择不调用真实模型。本地 PASS 只指声明的控制回归，不是生产发布认证，也不提供真实模型 ASR。

## 证据与范围

- `full-junit.xml` / `full-pytest.log`：全项目本地及隔离 PostgreSQL、Redis、Neo4j、OpenSearch 回归。`-m 'not deployed_stack' --run-docker`，1458 项通过；4 项依赖既有部署地址的旧测试未执行。
- `replays/`：82 个本地控制场景，逐节点保存 pytest setup/call/teardown 和变异判定；最终 82 PASS / 0 FAIL / 0 BLOCKED。
- `report.json` / `report.md`：纯 grader 从本批 replay 生成，本地 PASS；`full-scope-report.*` 则保留完整范围 BLOCKED，不用历史报告补齐。
- `mutation-evidence/`：每个变异实验的正常基线、变异阶段和还原后基线。
- `manifest.json` / `SHA256SUMS`：环境、版本与文件校验清单。
- `source.patch` / `changed-source/`：本轮代码修改快照（不含真实 `.env` 或用户文档）。

本地清单是 `../scenarios.local.v1.json`。它包含历史 72 项中的 71 项的本地测试/变异投影及 11 项新增控制；只含现场验收的 `SEC-APPROVAL-03` 未计入分母。`../local-scope.json` 保留被排除的 12 条验收引用（11 个独立案例）。原始 `../scenarios.v1.json`、历史 replay、历史报告均未覆盖。

模拟边界：JWT 使用真实 RSA 签名、模拟 JWKS 返回；工具/模型由可观察替身提供；PostgreSQL/Redis/Neo4j/OpenSearch 是真实独立容器。OpenSearch 测试容器关闭认证，仅在本机随机端口可达，因此其结果证明索引与 ACL 行为，不证明 TLS 或生产认证配置。

## 复跑前提

使用已安装依赖和本机 Docker 镜像，不复制真实配置，不在共享数据库上运行。先把当前修改复制到新 worktree；不能只检出 HEAD（修复尚未提交）。以下命令在仓库根运行，工作目录和容器名称需保持本轮唯一。

```bash
set -euo pipefail
SEC_ROOT="$PWD"
SEC_VENV="$SEC_ROOT/.venv"
SEC_WORK="$(mktemp -d /tmp/servicemind-security-replay.XXXXXX)"
git worktree add --detach "$SEC_WORK" HEAD
export SEC_ROOT SEC_WORK
python3 - <<'PY'
import os, shutil, subprocess
from pathlib import Path
root, work = Path(os.environ['SEC_ROOT']), Path(os.environ['SEC_WORK'])
paths = set()
for args in [('diff','HEAD','--name-only','-z'), ('ls-files','--others','--exclude-standard','-z')]:
    paths.update(p for p in subprocess.check_output(['git',*args],cwd=root).decode().split('\0') if p)
for path in paths:
    if path.startswith(('docs/', 'evaluation/security/audit_', 'evaluation/security/local_2026-10-22/')):
        continue
    source, target = root/path, work/path
    if source.is_file():
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    elif target.is_file():
        target.unlink()
PY
```

启动独立实例（不使用共享卷），正式复现应把标签替换为 manifest 中的镜像 ID。以下仅创建合成数据环境，不运行项目 Compose：

```bash
SEC_TAG="servicemind-security-$(date -u +%Y%m%dT%H%M%SZ)"
docker run -d --name "$SEC_TAG-pg" --label servicemind.purpose=security-assessment \
  --memory 768m --cpus 2 -p 127.0.0.1::5432 \
  -e POSTGRES_USER=security_admin -e POSTGRES_PASSWORD=isolated-test-only \
  -e POSTGRES_DB=servicemind_security postgres:16
docker run -d --name "$SEC_TAG-redis" --label servicemind.purpose=security-assessment \
  --memory 256m --cpus 1 -p 127.0.0.1::6379 redis:8.2-alpine
docker run -d --name "$SEC_TAG-neo4j" --label servicemind.purpose=security-assessment \
  --memory 1536m --cpus 2 -p 127.0.0.1::7687 \
  -e NEO4J_AUTH=neo4j/isolated-test-only \
  -e NEO4J_server_memory_heap_initial__size=256m \
  -e NEO4J_server_memory_heap_max__size=512m \
  -e NEO4J_server_memory_pagecache_size=256m neo4j:5.26
docker run -d --name "$SEC_TAG-os" --label servicemind.purpose=security-assessment \
  --memory 2g --cpus 2 -p 127.0.0.1::9200 -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true -e DISABLE_INSTALL_DEMO_CONFIG=true \
  -e 'OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m' opensearchproject/opensearch:3.8.0
```

服务就绪后以迁移管理员执行 `alembic upgrade head`，再用 `docker exec -i "$SEC_TAG-pg" psql -U security_admin -d servicemind_security` 执行以下 SQL。迁移命令和环境函数在下方给出。


```sql
CREATE ROLE security_app LOGIN PASSWORD 'isolated-test-only'
  NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE servicemind_security TO security_app;
GRANT USAGE ON SCHEMA public TO security_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO security_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO security_app;
```

测试账号与密码只用于合成数据容器，不用于真实环境。使用 `docker port 容器名` 获取映射端口并替换下方 `PGPORT/REDISPORT/NEO4JPORT/OSPORT`。Neo4j 设置 `NEO4J_AUTH=neo4j/isolated-test-only`；OpenSearch 设置 `discovery.type=single-node`、`DISABLE_SECURITY_PLUGIN=true`、`DISABLE_INSTALL_DEMO_CONFIG=true` 和 `OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m`。这些仅为隔离行为测试配置。

```bash
cd "$SEC_WORK"
# 端口取自你新建的容器；不要填现有项目端口。
PGPORT=32768
REDISPORT=32771
NEO4JPORT=32769
OSPORT=32770
sec_run() {
  env -i PATH="$SEC_VENV/bin:$HOME/.local/bin:/usr/bin:/bin" HOME=/tmp \
    PYTHONPATH="$SEC_WORK/src:$SEC_WORK/scripts" PYTHON_DOTENV_DISABLED=1 \
    UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT="$SEC_VENV" \
    SERVICEMIND_DATABASE_URL="postgresql+psycopg://security_app:isolated-test-only@127.0.0.1:$PGPORT/servicemind_security" \
    SERVICEMIND_REDIS_URL="redis://127.0.0.1:$REDISPORT" \
    NEO4J_URI="bolt://127.0.0.1:$NEO4JPORT" NEO4J_PASSWORD=isolated-test-only \
    SERVICEMIND_OPENSEARCH_URL="http://127.0.0.1:$OSPORT" \
    SERVICEMIND_OPENSEARCH_PASSWORD=isolated-test-only \
    OPENAI_API_KEY=sk-fake-openai-key "$@"
}
# 迁移时临时覆盖管理员 URL；迁移完成后测试始终使用 security_app。
sec_run env SERVICEMIND_MIGRATION_DATABASE_URL="postgresql+psycopg://security_admin:isolated-test-only@127.0.0.1:$PGPORT/servicemind_security" alembic upgrade head
# 此时执行上面的 CREATE ROLE / GRANT，再开始测试。
BATCH="evaluation/security/replay-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BATCH"
sec_run python -m pytest -m 'not deployed_stack' --run-docker -q --junitxml="$BATCH/full-junit.xml"
sec_run python scripts/verify_phase7_security.py \
  --scenarios evaluation/security/scenarios.local.v1.json --run-docker --replays "$BATCH/replays"
SEC_REV="$(sec_run python -c 'from pathlib import Path; from servicemind.evaluation.source_revision import source_revision; print(source_revision(Path.cwd()))')"
sec_run python scripts/gate_phase7_security.py --check --replay-only \
  --scenarios evaluation/security/scenarios.local.v1.json --replays "$BATCH/replays" \
  --expect-revision "$SEC_REV" --report "$BATCH/report.md"
sec_run ruff check .
sec_run ruff format --check .
sec_run pyrefly check
```

不可并行执行变异脚本或在变异时编辑被测目录。变异必须满足：正常基线全过 → 移除控制后 call 阶段失败 → 还原后全过；收集/初始化失败、跳过、超时不算检测成功。runner 拒绝覆写已有 replay 批次。

复跑结束先保存日志、`.runtime/mutate_*.evidence.json` 和报告，再删除本轮自建容器及其匿名卷。不要删除共享容器；不要在共享实例执行测试清理语句。取消控制的生效点是取消接口成功返回：此前已提交的在途写入可能存在，之后的新 GLPI 写入被拒；不承诺回滚外部系统或立即终止模型线程。

本批变异实际采用 `run_parallel_mutations.py`：每个实验/分片使用独立 worktree，权限实验分成 5 组各 10 项，每组均有正常/还原基线。`assemble_evidence.py` 只接受源码摘要一致、完整且成功结束的结果，再交给同一个纯 grader；原始未执行变异的测试批次不直接称为 PASS。

最终源码标识：`19d9a377fb98e1c3f56edc59be40b374013bc08b+patch(e4951fd79716)`。本批 82 场景全部 PASS、106 变异全部检测；45 场景有变异背书。replay 的 observed_at 为 runner 批次开始时间，结束时间见 assembly.json 的 assembled_at；不是逐节点耗时。Markdown 报告仅校正了模板固定目录说明，判定与未修改的 JSON 报告一致。
