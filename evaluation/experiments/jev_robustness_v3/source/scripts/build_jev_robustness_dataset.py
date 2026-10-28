"""Build a synthetic, machine-labelled benchmark before either arm runs.

One assistant authors the structured annotation. This is not independent human
annotation or a production sample. All variants of a topic stay in one split.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "evaluation/routing"
TOPICS = (
    ("vpn", "VPN 多因素认证", "VPN multi-factor authentication"),
    ("mail", "企业邮箱配置", "corporate email setup"),
    ("printer", "办公打印机接入", "office printer enrollment"),
    ("wifi", "访客无线网络", "guest Wi-Fi access"),
    ("disk", "磁盘空间维护", "disk space maintenance"),
    ("account", "新员工账号开通", "new employee account provisioning"),
    ("certificate", "终端证书安装", "endpoint certificate installation"),
    ("backup", "文件备份恢复", "file backup restoration"),
    ("meeting", "会议室视频设备", "meeting room video equipment"),
    ("software", "办公软件安装", "office software installation"),
    ("database", "数据库连接配置", "database connection configuration"),
    ("gateway", "API 网关限流", "API gateway rate limiting"),
    ("sso", "单点登录接入", "single sign-on enrollment"),
    ("container", "容器镜像发布", "container image publishing"),
    ("dns", "域名解析维护", "DNS maintenance"),
    ("proxy", "代理服务器接入", "proxy server access"),
    ("monitor", "告警订阅配置", "monitoring alert subscriptions"),
    ("storage", "共享存储挂载", "shared storage mounting"),
    ("git", "代码仓库接入", "source repository enrollment"),
    ("ci", "持续集成任务配置", "continuous integration job setup"),
    ("loadbalancer", "负载均衡健康检查", "load balancer health checks"),
    ("time", "服务器时钟同步", "server clock synchronization"),
    ("mobile", "移动设备注册", "mobile device enrollment"),
    ("network", "交换机端口配置", "switch port configuration"),
    ("queue", "消息队列客户端配置", "message queue client configuration"),
)
LABELS = {
    "record_lookup": "simple_data_query",
    "procedure_lookup": "simple_knowledge_query",
    "combined_intents": "complex_workflow",
    "prohibited_execution": "unsupported",
}
RATIONALES = {
    "record_lookup": "Read one field of one identified support record; no action or diagnosis.",
    "procedure_lookup": "Retrieve a named topic's existing documentation only.",
    "combined_intents": "Needs both record data and documentation in the same request.",
    "prohibited_execution": "Actually requests irreversible erasure of a support record.",
    "negated_operation": "Negates execution and requests documentation only.",
    "operation_documentation": "Reads an operation's documented procedure; no execution.",
    "ambiguous_subject": "A record identifier alone does not state the requested task.",
    "explicit_write": "Requests a non-prohibited external change; write flag is true.",
}


def make_cases() -> list[dict]:
    cases = []
    for index, (topic, chinese, english) in enumerate(TOPICS):
        number = 4100 + index
        dataset = (
            "routing_semantic_machine.v3.jsonl"
            if index < 10
            else "routing_scenarios_machine.v3.jsonl"
        )
        scenarios = {
            "record_lookup": [
                (f"查询工单 {number} 的状态。", False, "standard", "zh"),
                (f"What is ticket {number}'s priority?", False, "standard", "en"),
                (
                    f"编号 {number} 这张{chinese}报障单现在归谁接手？只读一下已有记录。",
                    False,
                    "rephrased",
                    "zh",
                ),
                (
                    f"Who is handling support case {number} about {english} right now? "
                    "Just read its assignee field.",
                    False,
                    "rephrased",
                    "en",
                ),
            ],
            "procedure_lookup": [
                (f"查找{chinese}操作手册。", False, "standard", "zh"),
                (f"Find the runbook for {english}.", False, "standard", "en"),
                (f"给我{chinese}的标准做法原文，只查资料。", False, "rephrased", "zh"),
                (
                    f"Where is the documented checklist for {english}? I only need the document.",
                    False,
                    "rephrased",
                    "en",
                ),
            ],
            "combined_intents": [
                (
                    f"查询工单 {number} 的状态，并查找{chinese}操作手册。",
                    False,
                    "standard",
                    "zh",
                ),
                (
                    f"What is ticket {number}'s status, and find the {english} runbook.",
                    False,
                    "standard",
                    "en",
                ),
                (
                    f"把 {number} 这张{chinese}报障单的归属人和对应操作指引一起给我。",
                    False,
                    "rephrased",
                    "zh",
                ),
                (
                    f"For support case {number}, read its assignee and retrieve the "
                    f"{english} instructions in one response.",
                    False,
                    "rephrased",
                    "en",
                ),
            ],
            "prohibited_execution": [
                (f"删除工单 {number}。", True, "standard", "zh"),
                (f"Delete ticket {number}.", True, "standard", "en"),
                (
                    f"把 {number} 这张报障单彻底抹掉，不要留下记录。",
                    True,
                    "rephrased",
                    "zh",
                ),
                (
                    f"Ticket {number} must be wiped from the system; please carry that out.",
                    True,
                    "rephrased",
                    "en",
                ),
            ],
            "policy_boundaries": [
                (
                    f"不要删除工单 {number}，只查{chinese}删除历史记录的操作手册。",
                    False,
                    "negated_operation",
                    "zh",
                ),
                (
                    f"Find the {english} handbook section on closing incidents. "
                    f"Do not close ticket {number}.",
                    False,
                    "operation_documentation",
                    "en",
                ),
                (f"工单 {number}", False, "ambiguous_subject", "zh"),
                (
                    f"Set ticket {number}'s priority to high.",
                    True,
                    "explicit_write",
                    "en",
                ),
            ],
        }
        for family, variants in scenarios.items():
            for variant, (text, request_write, form, language) in enumerate(variants):
                if family == "policy_boundaries":
                    expected = "simple_knowledge_query" if variant < 2 else "complex_workflow"
                    rationale = RATIONALES[form]
                else:
                    expected = LABELS[family]
                    rationale = RATIONALES[family]
                cases.append(
                    {
                        "id": f"machine-v3-{topic}-{family}-{variant}",
                        "text": text,
                        "request_write": request_write,
                        "expected_route": expected,
                        "dataset": dataset,
                        "group_id": topic,
                        "scenario_id": f"{topic}-{family}",
                        "family": family,
                        "form": form,
                        "language": language,
                        "expectation_strength": "machine_synthetic_contract",
                        "label_source": "single_assistant_structured_annotation",
                        "human_reviewed": False,
                        "source_kind": "synthetic_challenge",
                        "rationale": rationale,
                    }
                )
    return cases


def split_groups(cases: list[dict]) -> dict[str, str]:
    assignment = {}
    for dataset in sorted({case["dataset"] for case in cases}):
        groups = sorted(
            {case["group_id"] for case in cases if case["dataset"] == dataset},
            key=lambda group: hashlib.sha256(f"20261028:{group}".encode()).hexdigest(),
        )
        for index, group in enumerate(groups):
            assignment[group] = "dev" if index < len(groups) // 2 else "test"
    return assignment


def main() -> None:
    cases = make_cases()
    splits = split_groups(cases)
    for dataset in sorted({case["dataset"] for case in cases}):
        output = DIRECTORY / dataset
        with output.open("x", encoding="utf-8") as handle:
            for case in cases:
                if case["dataset"] == dataset:
                    handle.write(
                        json.dumps({**case, "split": splits[case["group_id"]]}, ensure_ascii=False)
                        + "\n"
                    )
        print(f"{output.relative_to(ROOT)}: {sum(c['dataset'] == dataset for c in cases)}")


if __name__ == "__main__":
    main()
