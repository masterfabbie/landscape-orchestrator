import { describe, expect, it } from "vitest";
import type { TemplateStep } from "../lib/types";
import { addStep, canConnect, connect, disconnect, layers, layout, removeStep, selectionType } from "./graph";

const step = (position: number, after: number[] = [], extra: Partial<TemplateStep> = {}): TemplateStep => ({
  position,
  operation: "stop",
  target_type: "SYSTEM",
  targets: [],
  after,
  parameters: {},
  ask: [],
  on_error: "stop",
  ...extra,
});

// 1 and 2 in parallel, 3 after both, 4 after 3 (like a kernel update in LaMa).
const graph = [step(1), step(2), step(3, [1, 2]), step(4, [3])];

describe("template graph", () => {
  it("puts steps in layers below their lowest predecessor", () => {
    expect([...layers(graph)]).toEqual([
      [1, 0],
      [2, 0],
      [3, 1],
      [4, 2],
    ]);
    const positions = layout(graph);
    expect(positions.get(1)?.y).toBe(positions.get(2)?.y);
    expect(positions.get(1)?.x).toBeLessThan(positions.get(2)?.x ?? 0);
    expect(positions.get(4)?.y).toBeGreaterThan(positions.get(3)?.y ?? 0);
  });

  it("connects steps only without a cycle", () => {
    expect(canConnect(graph, 4, 1)).toBe(false); // 1 comes before 4
    expect(canConnect(graph, 1, 3)).toBe(false); // already connected
    expect(canConnect(graph, 1, 1)).toBe(false);
    expect(canConnect(graph, 1, 2)).toBe(true);
    expect(connect(graph, 1, 2).find((s) => s.position === 2)?.after).toEqual([1]);
    expect(connect(graph, 4, 1)).toBe(graph);
    expect(disconnect(graph, 1, 3).find((s) => s.position === 3)?.after).toEqual([2]);
  });

  it("adds steps after a step and keeps the order when one is removed", () => {
    const { steps, position } = addStep(graph, { operation: "start", target_type: "SYSTEM", targets: [], parameters: {}, ask: [], on_error: "stop" }, 4);
    expect(position).toBe(5);
    expect(steps.at(-1)?.after).toEqual([4]);
    const without = removeStep(graph, 3);
    expect(without.find((s) => s.position === 4)?.after).toEqual([1, 2]);
  });

  it("derives the type of the selection", () => {
    expect(selectionType(graph)).toBe("SYSTEM");
    expect(selectionType([step(1, [], { targets: [3] })])).toBeNull();
    expect(selectionType([step(1), step(2, [], { target_type: "HOST" })])).toBe("mixed");
  });
});
