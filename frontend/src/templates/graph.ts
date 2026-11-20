// Pure logic of the template graph: layout from top to bottom, predecessors without
// cycles, adding and removing steps. Kept free of React for tests.

import type { TemplateStep } from "../lib/types";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 84;
const GAP_X = 48;
const GAP_Y = 56;

/** Layer of each step: 0 without predecessors, else one below its lowest predecessor. */
export function layers(steps: TemplateStep[]): Map<number, number> {
  const after = new Map(steps.map((s) => [s.position, s.after]));
  const result = new Map<number, number>();
  const visiting = new Set<number>();
  const of = (position: number): number => {
    const known = result.get(position);
    if (known !== undefined) return known;
    if (visiting.has(position)) return 0; // a cycle; the server refuses those
    visiting.add(position);
    const previous = (after.get(position) ?? []).filter((p) => after.has(p));
    const layer = previous.length ? Math.max(...previous.map(of)) + 1 : 0;
    visiting.delete(position);
    result.set(position, layer);
    return layer;
  };
  for (const step of steps) of(step.position);
  return result;
}

/** Top-left corner of each node; the steps of a layer side by side, centered. */
export function layout(steps: TemplateStep[]): Map<number, { x: number; y: number }> {
  const layer = layers(steps);
  const rows = new Map<number, number[]>();
  for (const step of [...steps].sort((a, b) => a.position - b.position)) {
    const l = layer.get(step.position) ?? 0;
    rows.set(l, [...(rows.get(l) ?? []), step.position]);
  }
  const widest = Math.max(1, ...[...rows.values()].map((r) => r.length));
  const positions = new Map<number, { x: number; y: number }>();
  for (const [l, row] of rows) {
    const offset = ((widest - row.length) * (NODE_WIDTH + GAP_X)) / 2;
    row.forEach((position, index) => {
      positions.set(position, { x: offset + index * (NODE_WIDTH + GAP_X), y: l * (NODE_HEIGHT + GAP_Y) });
    });
  }
  return positions;
}

/** Every step that comes before ``position`` (its predecessors, theirs, …). */
export function ancestors(steps: TemplateStep[], position: number): Set<number> {
  const after = new Map(steps.map((s) => [s.position, s.after]));
  const found = new Set<number>();
  const stack = [...(after.get(position) ?? [])];
  while (stack.length) {
    const next = stack.pop() as number;
    if (found.has(next)) continue;
    found.add(next);
    stack.push(...(after.get(next) ?? []));
  }
  return found;
}

/** Whether ``from`` may become a predecessor of ``to`` without a cycle. */
export function canConnect(steps: TemplateStep[], from: number, to: number): boolean {
  if (from === to) return false;
  const step = steps.find((s) => s.position === to);
  if (!step || step.after.includes(from)) return false;
  return !ancestors(steps, from).has(to);
}

export function connect(steps: TemplateStep[], from: number, to: number): TemplateStep[] {
  if (!canConnect(steps, from, to)) return steps;
  return steps.map((s) => (s.position === to ? { ...s, after: [...s.after, from].sort((a, b) => a - b) } : s));
}

export function disconnect(steps: TemplateStep[], from: number, to: number): TemplateStep[] {
  return steps.map((s) => (s.position === to ? { ...s, after: s.after.filter((a) => a !== from) } : s));
}

export function nextPosition(steps: TemplateStep[]): number {
  return Math.max(0, ...steps.map((s) => s.position)) + 1;
}

/** Add a step; with ``after`` as its predecessor (the "+" on a step). */
export function addStep(
  steps: TemplateStep[],
  step: Omit<TemplateStep, "position" | "after">,
  after: number | null,
): { steps: TemplateStep[]; position: number } {
  const position = nextPosition(steps);
  return { steps: [...steps, { ...step, position, after: after === null ? [] : [after] }], position };
}

/** Remove a step; its successors take over its predecessors, so the order stays. */
export function removeStep(steps: TemplateStep[], position: number): TemplateStep[] {
  const removed = steps.find((s) => s.position === position);
  const inherited = removed?.after ?? [];
  return steps
    .filter((s) => s.position !== position)
    .map((s) =>
      s.after.includes(position)
        ? { ...s, after: [...new Set([...s.after.filter((a) => a !== position), ...inherited])].sort((a, b) => a - b) }
        : s,
    );
}

/** Type of the selection the steps without fixed targets need, or null; "mixed" if they
 * disagree (the server refuses that). */
export function selectionType(steps: TemplateStep[]): string | null {
  const types = new Set(steps.filter((s) => !s.targets.length).map((s) => s.target_type));
  if (types.size > 1) return "mixed";
  return [...types][0] ?? null;
}
