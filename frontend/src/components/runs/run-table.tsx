import Link from "next/link";
import type { RunSummary } from "@/lib/contracts";
import { formatDate, shortId } from "@/lib/format";
import { StatusBadge } from "@/components/ui/status";

export function RunTable({ runs }: { runs: RunSummary[] }) {
  return <div className="table-wrap"><table><thead><tr><th>运行与工单</th><th>调查目标</th><th>操作范围</th><th>状态</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{runs.map((run) => <tr key={run.id}><td><Link className="primary-cell" href={`/runs/${run.id}`}>{shortId(run.id)}</Link><small>GLPI 工单 #{run.ticket_id}</small></td><td className="goal-cell">{run.goal}</td><td>{run.request_write ? <span className="write-intent">可生成写操作建议</span> : "只读分析"}</td><td><StatusBadge status={run.status} /></td><td className="mono">{formatDate(run.updated_at)}</td><td><Link className="row-action" aria-label={`查看运行 ${shortId(run.id)}`} href={`/runs/${run.id}`}>查看</Link></td></tr>)}</tbody></table></div>;
}
