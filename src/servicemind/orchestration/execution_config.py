"""Execution policy supplied by the application bootstrap, independent of its settings shell."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunExecutionConfig:
    mode: Literal["in_process", "worker"] = "in_process"
    per_tenant_limit: int = 2
    queue_limit: int = 100


_config = RunExecutionConfig()


def configure_run_execution(config: RunExecutionConfig) -> None:
    global _config
    _config = config


def get_run_execution_config() -> RunExecutionConfig:
    return _config
