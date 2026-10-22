import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "local_model_evaluation", ROOT / "scripts/local_model_evaluation.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "STATE", tmp_path)
    monkeypatch.setattr(MODULE, "BATCH", tmp_path)
    env = {
        "SERVICEMIND_DATABASE_URL": "postgresql+psycopg://app:private@127.0.0.1:32780/local_evaluation",
        "SERVICEMIND_MIGRATION_DATABASE_URL": "postgresql+psycopg://admin:private@127.0.0.1:32780/local_evaluation",
        "SERVICEMIND_REDIS_URL": "redis://127.0.0.1:32781",
        "NEO4J_URI": "bolt://127.0.0.1:32782",
        "SERVICEMIND_OPENSEARCH_URL": "http://127.0.0.1:32783",
        "POSTGRES_PORT": "32780",
        "POSTGRES_DB": "local_evaluation",
        "COMPATIBLE_BASE_URL": "http://127.0.0.1:11441/v1",
        "COMPATIBLE_MODEL": MODULE.MODEL,
        "DEFAULT_MODEL": "openai-compatible",
        "SERVICEMIND_MODEL_ALLOWED_MODELS": MODULE.MODEL,
        "SERVICEMIND_MODEL_ALLOWED_PROVIDERS": "openai",
    }
    (tmp_path / "isolation-manifest.json").write_text(
        json.dumps({"ports": {"pg": 32780, "redis": 32781, "neo4j": 32782, "os": 32783}})
    )
    return env, tmp_path / "environment.json"


def test_disposable_persistence_and_local_model_are_admitted(isolated_environment):
    env, path = isolated_environment
    path.write_text(json.dumps(env))
    assert MODULE.environment() == env


@pytest.mark.parametrize(
    "name,value",
    [
        ("SERVICEMIND_DATABASE_URL", "postgresql+psycopg://app:private@127.0.0.1:55434/live"),
        ("POSTGRES_PORT", "55434"),
        ("POSTGRES_DB", "live"),
        ("SERVICEMIND_REDIS_URL", "redis://127.0.0.1:6379"),
        ("NEO4J_URI", "bolt://127.0.0.1:17687"),
        ("SERVICEMIND_OPENSEARCH_URL", "http://127.0.0.1:9200"),
        ("COMPATIBLE_BASE_URL", "https://paid-model.invalid/v1"),
        ("DEFAULT_MODEL", "deepseek-v4-flash"),
        ("SERVICEMIND_MODEL_ALLOWED_MODELS", "*"),
    ],
)
def test_live_persistence_or_a_cloud_model_cannot_be_used(isolated_environment, name, value):
    env, path = isolated_environment
    env[name] = value
    path.write_text(json.dumps(env))
    with pytest.raises(RuntimeError):
        MODULE.environment()
