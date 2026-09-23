"""Seed the local GLPI instance with a realistic ITSM ticket corpus.

ServiceMind runs are ticket-driven: ``POST /v1/servicemind/runs`` requires
``ticket_id >= 1`` and every plan starts with ``get_ticket``. A GLPI instance with
an empty ``glpi_tickets`` table therefore makes every run fail at T1 with a 404
(recorded as ``provider_not_found``) and escalate to human review.

This script populates the tenant's entity with a diverse corpus so the console,
the agent plans and the acceptance walkthroughs have real material to work with.
It talks to GLPI through the same resolved tenant integration the product uses
(``resolve_glpi_config``), so the entity/profile match production exactly.

The script is idempotent: a ticket whose name already exists is left untouched, except
that a ticket with no followup timeline yet gets the demo timeline appended once. Only
the ticket description and the timeline are written; no state, assignment or solution
field is mutated.

    uv run python scripts/seed_demo_glpi_tickets.py
    uv run python scripts/seed_demo_glpi_tickets.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from uuid import UUID

from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.security.auth import TenantContext

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")

# GLPI reference ids: type 1 = Incident, 2 = Request.
INCIDENT, REQUEST = 1, 2


@dataclass(frozen=True)
class DemoTicket:
    name: str
    content: str
    type: int
    urgency: int
    impact: int
    #: Timeline entries, appended in order when the ticket has none yet. The
    #: Analysis Agent reads the followup timeline, so a ticket whose body alone is
    #: a one-line symptom can only support a hedged hypothesis -- and the Reviewer,
    #: which requires each claim to be entailed, then has to abstain. A followup
    #: that records what was actually found gives the run something to ground a
    #: concrete recommendation in, which is what the resolved tickets below carry.
    followups: tuple[str, ...] = ()


DEMO_TICKETS: tuple[DemoTicket, ...] = (
    DemoTicket(
        name="笔记本连不上公司 VPN",
        content=(
            "用户报告自今早开始无法通过公司 VPN 接入内网。VPN 客户端显示“无法建立隧道”。"
            "受影响服务：Remote Access / VPN。用户在办公室有线网络下可正常访问内网，"
            "家庭宽带下必然失败。已尝试重启客户端与路由器，问题依旧。"
        ),
        type=INCIDENT,
        urgency=4,
        impact=3,
        followups=(
            "一线跟进：受影响用户在办公室有线网络下可正常建立隧道，家庭宽带下必然失败；"
            "网关侧日志中未出现该用户的连接尝试，初判问题在用户侧出口而非网关。",
            "抓包分析：家庭宽带出口对 IKE 协商的 UDP 500/4500 端口无响应，"
            "判断为用户所在 ISP 侧封禁；改用客户端 TCP 443 回退模式后隧道成功建立。",
            "处置结果：为客户端启用 TCP 443 回退配置，连续观察 30 分钟隧道稳定；"
            "已建议用户向 ISP 确认 UDP 策略。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="更换手机后 MFA 令牌无法验证",
        content=(
            "用户更换手机后未重新登记 MFA 令牌，登录 VPN 时提示验证码无效。"
            "受影响服务：Identity / MFA 令牌登记。用户仍持有旧设备，但旧设备上的"
            "令牌已失效。需要核实是否为令牌登记问题而非网关故障。"
        ),
        type=INCIDENT,
        urgency=4,
        impact=3,
        followups=(
            "一线核查：用户在 MFA 登记库中的令牌记录状态为 suspended，"
            "与其更换手机后未重新登记一致；同一时段网关侧无认证失败告警，"
            "网关连通性正常。",
            "处置过程：吊销旧设备令牌并在新设备上重新登记，用户登录 VPN 验证通过。",
            "后续动作：在自助指引中补充“更换设备需重新登记令牌”条目，由 Network Team 归档。",
        ),
    ),
    DemoTicket(
        name="VPN 客户端升级后无法连接",
        content=(
            "用户安装 VPN 客户端 7.4.2 后无法连接，回退到 7.3.9 可正常使用。"
            "受影响服务：Remote Access / VPN 客户端。多台同型号笔记本在升级后复现，"
            "怀疑与客户端版本兼容性有关。"
        ),
        type=INCIDENT,
        urgency=4,
        impact=3,
        followups=(
            "一线复现：三台同型号笔记本在 7.4.2 上均于 IKEv2 协商阶段失败；"
            "同一账号在回退到 7.3.9 的设备上五分钟内建立隧道，确认为客户端版本相关，"
            "而非账号或网络问题。",
            "厂商支持确认：7.4.2 调整了 IKEv2 的 DH 组协商顺序，与网关现有配置不兼容，"
            "该缺陷已在 7.4.3 修复。",
            "处置结果：受影响设备统一升级到 7.4.3，用户回访确认隧道正常；"
            "版本兼容性说明已更新到 VPN 客户端部署基线。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="邮箱收不到外部邮件",
        content=(
            "用户反馈从昨天下午起收不到任何外部域发来的邮件，内部邮件正常。"
            "受影响服务：Exchange Online / 邮件流。发送方未收到退信，"
            "邮件跟踪显示邮件在边界网关处被丢弃。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=3,
        followups=(
            "邮件跟踪：外部域邮件均在边界网关处被丢弃，内部邮件投递正常；"
            "网关日志记录的丢弃原因为发件域未通过 SPF 校验。",
            "处置结果：与发件方确认其 SPF 记录后将其域加入边界网关的合规发件域策略，"
            "外部邮件恢复投递。归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="邮件附件被安全网关拦截",
        content=(
            "用户报告来自合作方的 PDF 附件被安全网关拦截，提示“附件类型受限”。"
            "受影响服务：邮件安全网关。用户需要该附件完成当日合同评审。"
        ),
        type=INCIDENT,
        urgency=2,
        impact=2,
    ),
    DemoTicket(
        name="邮箱容量不足，申请扩容",
        content=(
            "用户邮箱已使用 49.2 GB，接近 50 GB 配额上限，已无法接收新邮件。"
            "受影响服务：Exchange Online / 邮箱配额。申请将配额提升至 100 GB。"
        ),
        type=REQUEST,
        urgency=3,
        impact=2,
    ),
    DemoTicket(
        name="共享盘访问被拒绝",
        content=(
            "用户访问财务共享盘 \\\\fileserver\\finance 时提示“拒绝访问”。"
            "受影响服务：File Services / 权限。用户上周仍在同一目录正常工作，"
            "期间未变更岗位。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=2,
        followups=(
            "权限核查：用户在目录服务中已被移出 finance-share 访问组，"
            "与上月岗位调整记录一致；文件服务器本身未见故障或告警。",
            "处置结果：服务责任人确认访问必要性后重新加入访问组，"
            "用户验证可正常访问。共享盘权限变更需经服务责任人审批。归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="财务共享盘访问缓慢",
        content=(
            "多名财务同事反馈共享盘打开大文件需等待 30 秒以上。受影响服务："
            "File Services / 性能。受影响范围限于财务部门，其他部门暂未反馈。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=3,
        followups=(
            "性能核查：文件服务器在 09:00-11:00 区间磁盘队列长度持续高于告警阈值，"
            "与财务月末批量对账任务时段重合；其他部门同期无同类反馈。",
            "处置结果：对账任务改为分批执行并错峰到 20:00 之后，"
            "财务共享盘打开大文件耗时回落到 5 秒以内。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="打印机离线无法打印",
        content=(
            "三楼东侧打印机在打印队列中显示离线，重启打印机后短时恢复又再次离线。"
            "受影响服务：Print Services。本层约 12 名同事受影响。"
        ),
        type=INCIDENT,
        urgency=2,
        impact=2,
        followups=(
            "现场核查：打印机网线在配线架侧松动，链路抖动导致设备在打印队列中反复离线。",
            "处置结果：更换跳线并固定端口，打印机连续在线两日无掉线，本层打印恢复。"
            "归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="会议室视频设备无声",
        content=(
            "大会议室视频会议系统画面正常但无音频输出。受影响服务："
            "Meeting Room AV。今日 14:00 有客户会议，需在此之前恢复。"
        ),
        type=INCIDENT,
        urgency=4,
        impact=3,
    ),
    DemoTicket(
        name="办公电脑频繁蓝屏重启",
        content=(
            "用户笔记本每日蓝屏 2-3 次，错误码 WHEA_UNCORRECTABLE_ERROR。"
            "受影响服务：End User Compute。设备已过保，用户担心数据丢失。"
        ),
        type=INCIDENT,
        urgency=4,
        impact=2,
        followups=(
            "硬件诊断：故障码 WHEA_UNCORRECTABLE_ERROR 指向处理器缓存错误，"
            "多条内存与主板检测未发现其他异常，原设备已过保但故障可稳定复现。",
            "处置结果：更换备用整机，用户确认本地数据完好；原设备降级为备件，"
            "不再用于生产办公。归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="无法访问内部知识库 Wiki",
        content=(
            "用户访问内部 Wiki 时页面持续加载后超时，同一网络下的同事可正常访问。"
            "受影响服务：Internal Portal。用户已清理浏览器缓存，问题依旧。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=3,
        followups=(
            "对比测试：同一网段其他终端访问正常，仅该用户的浏览器代理配置指向"
            "一台已下线的旧代理地址，导致请求超时。",
            "处置结果：清除浏览器代理配置后页面正常加载；旧代理地址已登记为待清理项。"
            "归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="忘记域账号密码，申请重置",
        content=(
            "用户遗忘域账号密码，已连续输错三次导致账号锁定，申请重置密码并解锁。"
            "受影响服务：Identity / Active Directory。用户携带工牌可到现场核验身份。"
        ),
        type=REQUEST,
        urgency=4,
        impact=2,
        followups=(
            "身份核验：用户持工牌到现场，已通过工牌与直属主管两种独立方式核验身份。",
            "处置结果：在身份目录中执行口令重置并解除锁定，要求用户下次登录强制改密；"
            "一周内未再出现该用户的重置请求。归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="新员工入职账号开通申请",
        content=(
            "新入职同事需要开通域账号、邮箱、VPN 与共享盘权限。受影响服务："
            "Identity / 入职开通。入职日期为下周一，需提前完成开通。"
        ),
        type=REQUEST,
        urgency=3,
        impact=2,
    ),
    DemoTicket(
        name="申请安装办公软件",
        content=(
            "用户申请在办公电脑上安装项目所需的绘图软件。受影响服务："
            "End User Compute / 软件分发。软件许可由部门预算承担。"
        ),
        type=REQUEST,
        urgency=2,
        impact=1,
    ),
    # The batch below gives every knowledge-base standard a ticket with a closed
    # timeline. Without one, a retrieval round returns the standard but no ticket fact
    # can entail a claim about *this* environment, so the Reviewer can only abstain.
    DemoTicket(
        name="VPN 网关证书临近到期，申请续期",
        content=(
            "监控平台告警：VPN 网关对外证书剩余有效期 12 天，低于 VPN 网关 45 天的"
            "提前告警阈值。受影响服务：Remote Access / 证书。用户侧暂未出现连接失败。"
        ),
        type=REQUEST,
        urgency=4,
        impact=3,
        followups=(
            "核查记录：证书自动化平台已为该网关生成续期任务，当前证书剩余 12 天，"
            "仍高于内部服务 7 天的续期下限，但已低于 VPN 网关 45 天的告警阈值，"
            "因此本例为提前处置而非故障。",
            "处置结果：通过证书自动化完成续期，并在生效后核验了实况端点的叶证书与"
            "信任链，续期记录已关联到 Remote Access 服务记录。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="VPN 验证码被拒，疑似网关时钟偏移",
        content=(
            "多名用户反馈在 MFA 中输入的动态验证码被拒，重新生成后仍失败，"
            "影响面持续扩大。受影响服务：Identity / MFA 令牌验证。"
        ),
        type=INCIDENT,
        urgency=5,
        impact=4,
        followups=(
            "排查记录：网关时钟与 NTP 源比对偏移 210 秒，超过 90 秒的容忍上限，"
            "因此一次性验证码在校验时被判为过期；令牌登记状态本身正常。",
            "处置结果：恢复网关与 NTP 的时间同步，偏移回落到 1 秒内，"
            "验证码校验恢复正常。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="新员工域账号被锁定，申请解锁",
        content=(
            "入职两周的新同事域账号被锁定，本人表示未连续输错密码。"
            "受影响服务：Identity / Active Directory。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=2,
        followups=(
            "核查记录：锁定计数器来源为一台已离职同事留用的终端，该终端仍以新员工"
            "账号缓存凭据反复重试登录，并非用户本人输错密码。",
            "处置结果：解除账号锁定并在该终端上清除旧凭据缓存；"
            "按口令重置流程要求用户下次登录强制改密。归属 Service Desk。",
        ),
    ),
    DemoTicket(
        name="核心数据库服务器磁盘使用率超阈值",
        content=(
            "容量看板告警：核心数据库服务器数据盘使用率 82%，超过 80% 的告警阈值。"
            "受影响服务：Database Platform。当前业务未受影响。"
        ),
        type=INCIDENT,
        urgency=3,
        impact=3,
        followups=(
            "核查记录：增长主要来自归档日志保留窗口延长，磁盘使用率在两周内由 71% "
            "升至 82%，已超过容量看板 80% 的告警阈值，但尚未触及服务不可用。",
            "处置结果：按容量标准提交扩容变更单（而非在主机上直接调整），"
            "按 95 分位负载与 12 个月增长预测完成扩容，使用率回落至 61%。"
            "归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="恢复演练未达标，申请恢复请求评估",
        content=(
            "上季度恢复演练显示核心数据库实际恢复耗时 5.5 小时，"
            "超过 4 小时的恢复时间目标。受影响服务：Database Platform。"
        ),
        type=REQUEST,
        urgency=3,
        impact=3,
        followups=(
            "演练记录：恢复在预发布实例完成，行数与校验和一致，但实际耗时 5.5 小时，"
            "超过核心数据库 4 小时的恢复时间目标；恢复点目标 15 分钟达成。",
            "处置结果：按恢复流程提交变更系统恢复请求并更新运行手册中的实测恢复时间，"
            "演练报告已归档，下一季度复测。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="生产环境紧急变更申请：认证基础设施",
        content=(
            "因认证网关故障需在生产环境执行紧急变更。受影响服务："
            "Identity / 认证基础设施。变更窗口无法等待常规审批。"
        ),
        type=REQUEST,
        urgency=5,
        impact=4,
        followups=(
            "审批记录：该变更修改认证基础设施，按策略属于高风险紧急变更，"
            "需值班主管授权；变更单已声明受影响配置项、回滚方案与客户影响。",
            "处置结果：值班主管授权后在窗口内实施，MFA 运行手册已关联、"
            "令牌存储可达性在实施前完成验证；按要求在 24 小时内补做复盘审查。"
            "归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="P1 故障影响面扩大，申请开通指挥桥",
        content=(
            "远程接入服务大面积不可用，受影响用户超过 800 人，"
            "且影响面仍在扩大。受影响服务：Remote Access。"
        ),
        type=INCIDENT,
        urgency=5,
        impact=5,
        followups=(
            "事件记录：故障影响业务关键服务且影响面持续扩大，"
            "已按重大事件定义开通指挥桥并指定单一事件指挥；"
            "对外状态通告自宣布起每 30 分钟发布一次。",
            "处置结果：恢复后由指挥把完整时间线移交变更流程，"
            "修复动作转为受跟踪的变更单，复盘记录已关联。归属 Network Team。",
        ),
    ),
    DemoTicket(
        name="VPN 故障本月第三次复发，申请创建问题单",
        content=(
            "同一服务本月第三次出现同类 VPN 中断，每次由不同工单处理，"
            "缺乏根因跟踪。受影响服务：Remote Access。"
        ),
        type=REQUEST,
        urgency=3,
        impact=3,
        followups=(
            "复发核查：本月三次中断的事件记录指向同一台网关设备的同一类隧道"
            "协商失败，属于重复出现且根因未定位的同类事件。",
            "处置结果：按重复问题流程创建问题单并关联三条事件记录，"
            "在根因定位前保留逐次应急处置记录。归属 Network Team。",
        ),
    ),
)


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        user_id="glpi-seed-script",
        username="glpi-seed-script",
        roles={"viewer", "analyst"},
        allowed_glpi_entity_ids={1},
    )


async def seed(*, dry_run: bool) -> int:
    async with GlpiClient(await resolve_glpi_config(_context())) as glpi:
        created: list[int] = []
        skipped: list[str] = []
        enriched: list[int] = []
        for ticket in DEMO_TICKETS:
            # Match on the exact name: the corpus may grow past the 20-row listing
            # limit, and a fuzzy search would confuse near-identical tickets.
            matches = await glpi.search_tickets(ticket.name, limit=20)
            existing = next((item for item in matches if item.name == ticket.name), None)
            if existing is not None:
                skipped.append(ticket.name)
                if dry_run or not ticket.followups:
                    continue
                # The timeline is the ticket's own history. Append it only when the
                # ticket has none, so a re-run of this script cannot duplicate it --
                # and so tickets seeded before followups existed are brought up to
                # date rather than left as a bare description.
                if await glpi.list_ticket_followups(existing.id):
                    continue
                for note in ticket.followups:
                    await glpi.append_ticket_followup(existing.id, note)
                enriched.append(existing.id)
                continue
            if dry_run:
                created.append(-1)
                continue
            result = await glpi._request(  # noqa: SLF001 - the client exposes no create method
                "POST",
                f"{glpi.api_prefix}/Assistance/Ticket",
                json={
                    "name": ticket.name,
                    "content": ticket.content,
                    "type": ticket.type,
                    "urgency": ticket.urgency,
                    "impact": ticket.impact,
                },
            )
            ticket_id = int(result["id"])
            created.append(ticket_id)
            for note in ticket.followups:
                await glpi.append_ticket_followup(ticket_id, note)

        total = len(await glpi.list_recent_tickets(limit=20))

    label = "待创建" if dry_run else "已创建"
    print(f"{label}: {len(created)} 条；已存在跳过: {len(skipped)} 条；当前可见工单: {total} 条")
    if created and not dry_run:
        print("新工单 id:", ", ".join(str(item) for item in created))
    if enriched:
        print("已补写跟进时间线:", ", ".join(str(item) for item in enriched))
    for name in skipped:
        print(f"  跳过(已存在): {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只报告将要创建的内容，不写入 GLPI")
    args = parser.parse_args()
    return asyncio.run(seed(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
