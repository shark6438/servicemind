import { describe, expect, it } from "vitest";
import { layoutTasks, type PlannedTask } from "./task-dag";

const task = (id: string, dependsOn: string[] = []): PlannedTask => ({
  id, dependsOn, type: "get_ticket", agent: "data", status: "success",
});

describe("task dependency layout", () => {
  it("places parallel roots together and draws both edges into a join", () => {
    const graph = layoutTasks([task("T3", ["T1", "T2"]), task("T1"), task("T2"), task("T4", ["T3"])]);
    const byId = new Map(graph.placed.map((item) => [item.id, item]));
    expect(byId.get("T1")?.y).toBe(byId.get("T2")?.y);
    expect(byId.get("T3")!.y).toBeGreaterThan(byId.get("T1")!.y);
    expect(byId.get("T4")!.y).toBeGreaterThan(byId.get("T3")!.y);
    expect(graph.edges.map((edge) => `${edge.from}->${edge.to}`).sort()).toEqual(["T1->T3", "T2->T3", "T3->T4"]);
  });

  it("does not invent an edge for a missing dependency", () => {
    const graph = layoutTasks([task("T1", ["external"])]);
    expect(graph.edges).toEqual([]);
    expect(graph.placed[0]?.y).toBe(14);
  });
});
