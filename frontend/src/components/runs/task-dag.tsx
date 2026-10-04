import { readableLabel } from "@/lib/format";

export type PlannedTask = {
  id: string;
  type: string;
  agent: string;
  status: string;
  dependsOn: string[];
};

type PlacedTask = PlannedTask & { x: number; y: number; rowCount: number };
const ROW_HEIGHT = 110;
const NODE_HEIGHT = 80;
const TOP = 14;

export function layoutTasks(tasks: PlannedTask[]) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const depths = new Map<string, number>();

  function depthOf(id: string, visiting = new Set<string>()): number {
    if (depths.has(id)) return depths.get(id)!;
    const task = byId.get(id);
    if (!task || visiting.has(id)) return 0;
    visiting.add(id);
    const parents = task.dependsOn.filter((parent) => parent !== id && byId.has(parent));
    const depth = parents.length ? 1 + Math.max(...parents.map((parent) => depthOf(parent, visiting))) : 0;
    visiting.delete(id);
    depths.set(id, Math.min(depth, tasks.length - 1));
    return depths.get(id)!;
  }

  for (const task of tasks) depthOf(task.id);
  const rows = new Map<number, PlannedTask[]>();
  for (const task of tasks) {
    const level = depths.get(task.id) ?? 0;
    rows.set(level, [...(rows.get(level) ?? []), task]);
  }
  const placed: PlacedTask[] = [];
  for (const [level, row] of rows) {
    row.forEach((task, index) => placed.push({
      ...task, x: ((index + 1) / (row.length + 1)) * 1000,
      y: TOP + level * ROW_HEIGHT, rowCount: row.length,
    }));
  }
  const placedById = new Map(placed.map((task) => [task.id, task]));
  const edges = placed.flatMap((task) => task.dependsOn.flatMap((parentId) => {
    const parent = placedById.get(parentId);
    if (!parent || parent.y >= task.y) return [];
    const start = parent.y + NODE_HEIGHT;
    const middle = start + (task.y - start) / 2;
    return [{ from: parentId, to: task.id,
      path: `M ${parent.x} ${start} C ${parent.x} ${middle}, ${task.x} ${middle}, ${task.x} ${task.y}` }];
  }));
  return {
    placed,
    edges,
    height: TOP * 2 + (Math.max(0, ...placed.map((task) => (task.y - TOP) / ROW_HEIGHT)) * ROW_HEIGHT) + NODE_HEIGHT,
    minWidth: Math.max(560, ...[...rows.values()].map((row) => row.length * 170)),
  };
}

export function TaskDag({ tasks }: { tasks: PlannedTask[] }) {
  const graph = layoutTasks(tasks);
  return (
    <>
    <div className="task-dag-scroll" role="region" aria-label="任务依赖图，可横向滚动" tabIndex={0}>
      <div className="task-dag" style={{ height: graph.height, minWidth: graph.minWidth }}>
        <svg className="task-dag-edges" viewBox={`0 0 1000 ${graph.height}`} preserveAspectRatio="none" aria-hidden="true">
          <defs><marker id="task-dag-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M 0 0 L 6 3 L 0 6" /></marker></defs>
          {graph.edges.map((edge) => <path key={`${edge.from}-${edge.to}`} d={edge.path} markerEnd="url(#task-dag-arrow)" />)}
        </svg>
        <ol className="task-dag-nodes">
          {graph.placed.map((task) => <li key={task.id} className="task-dag-node" style={{ left: `${task.x / 10}%`, top: task.y, width: `min(216px, calc(100% / ${task.rowCount + 1} - 12px))` }}>
            <div className="task-dag-node-top"><span>{task.id}</span><span>{readableLabel(task.status)}</span></div>
            <strong>{readableLabel(task.type)}</strong>
            <small>{readableLabel(task.agent)}智能体</small>
            <span className="sr-only">{task.dependsOn.length ? `依赖 ${task.dependsOn.join("、")}` : "无依赖"}</span>
          </li>)}
        </ol>
      </div>
    </div>
    <p className="task-dag-hint">横向滚动可查看完整任务图</p>
    </>
  );
}
