from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from servicemind.context.contracts import ContextAgent
from servicemind.skills.contracts import PublishedSkill, ResolvedSkill, SkillManifest, SkillScope

UNSAFE_INSTRUCTIONS = (
    "ignore previous",
    "ignore all prior",
    "override policy",
    "bypass approval",
    "reveal credential",
    "忽略所有",
    "绕过审批",
    "无视系统",
)


def _normalise_body(body: str) -> str:
    return body.strip().replace("\r\n", "\n") + "\n"


class SkillRegistry:
    """Checksum-verified, metadata-first registry for reviewed in-repo skills."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._skills: dict[tuple[str, str], tuple[SkillManifest, Path]] = {}

    def load(self) -> None:
        loaded: dict[tuple[str, str], tuple[SkillManifest, Path]] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            real = path.resolve()
            if not real.is_relative_to(self.root) or path.is_symlink():
                raise PermissionError(f"skill path escapes registry root: {path}")
            if path.stat().st_size > 64_000:
                raise ValueError(f"skill package exceeds 64 KiB: {path}")
            manifest, body = self._parse(path.read_text(encoding="utf-8"), path)
            key = (manifest.skill_id, manifest.version)
            if key in loaded:
                raise ValueError(f"duplicate skill version: {manifest.skill_id}@{manifest.version}")
            loaded[key] = (manifest, path)
        self._skills = loaded

    @staticmethod
    def _parse(raw: str, path: Path) -> tuple[SkillManifest, str]:
        if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
            raise ValueError(f"skill must start with JSON front matter: {path}")
        manifest_text, body = raw[4:].split("\n---\n", 1)
        manifest_value = json.loads(manifest_text)
        manifest = SkillManifest.model_validate(manifest_value)
        body = _normalise_body(body)
        unsigned = {key: value for key, value in manifest_value.items() if key != "checksum"}
        canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual = hashlib.sha256(f"{canonical}\n{body}".encode()).hexdigest()
        if actual != manifest.checksum:
            raise ValueError(f"skill checksum mismatch: {manifest.skill_id}@{manifest.version}")
        lowered = body.casefold()
        if any(marker in lowered for marker in UNSAFE_INSTRUCTIONS):
            raise PermissionError(
                f"skill contains a policy bypass instruction: {manifest.skill_id}"
            )
        return manifest, body

    def metadata(self, *, tenant_id: UUID, agent: ContextAgent) -> tuple[SkillManifest, ...]:
        return tuple(
            manifest
            for manifest, _ in self._skills.values()
            if agent in manifest.allowed_agents
            and (manifest.scope is SkillScope.GLOBAL or manifest.tenant_id == str(tenant_id))
            and manifest.deprecated_at is None
        )

    def resolve(
        self,
        *,
        tenant_id: UUID,
        agent: ContextAgent,
        task_text: str,
        agent_capabilities: frozenset[str],
        user_permissions: frozenset[str],
        tenant_policy: frozenset[str],
        tool_policy: frozenset[str],
        limit: int = 2,
    ) -> tuple[ResolvedSkill, ...]:
        base = agent_capabilities & user_permissions & tenant_policy & tool_policy
        tokens = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", task_text.casefold()))
        resolved: list[ResolvedSkill] = []
        for manifest in self.metadata(tenant_id=tenant_id, agent=agent):
            if not manifest.required_tools <= base:
                continue
            overlap = len(tokens & {value.casefold() for value in manifest.keywords})
            if overlap == 0:
                continue
            _, path = self._skills[(manifest.skill_id, manifest.version)]
            verified_manifest, body = self._parse(path.read_text(encoding="utf-8"), path)
            if verified_manifest != manifest:
                raise RuntimeError("skill changed after metadata discovery")
            skill = PublishedSkill(
                manifest=manifest,
                instructions=body,
                source_path=str(path),
            )
            effective = base & manifest.required_tools
            resolved.append(
                ResolvedSkill(
                    skill=skill,
                    effective_capabilities=effective,
                    match_score=min(1.0, overlap / max(len(manifest.keywords), 1)),
                    resolution_reason="keyword_match_and_capability_intersection",
                )
            )
        resolved.sort(
            key=lambda value: (
                -value.match_score,
                value.skill.manifest.skill_id,
                value.skill.manifest.version,
            )
        )
        return tuple(resolved[:limit])
