import { describe, expect, it } from "vitest";
import type { ActivityDetail, LogEntry } from "../lib/types";
import { buildTimeline, formatDuration, place } from "./timeline";

const T0 = Date.parse("2026-11-20T10:00:00Z");
let id = 0;
const entry = (seconds: number, message: string, extra: Partial<LogEntry> = {}): LogEntry => ({
  id: ++id,
  timestamp: new Date(T0 + seconds * 1000).toISOString(),
  level: "INFO",
  message,
  event: null,
  subject: null,
  data: null,
  ...extra,
});

function activity(entries: LogEntry[], extra: Partial<ActivityDetail> = {}): ActivityDetail {
  return {
    id: 1,
    kind: "RESTART",
    target_type: "SYSTEM",
    target_label: "DEV",
    requested_by: "op",
    system_id: 1,
    instance_id: null,
    host_id: null,
    parent_id: null,
    status: "SUCCEEDED",
    timeout_seconds: 600,
    error: null,
    requested_at: new Date(T0).toISOString(),
    started_at: new Date(T0).toISOString(),
    finished_at: new Date(T0 + 100_000).toISOString(),
    entries,
    children: [],
    options: null,
    ...extra,
  };
}

describe("timeline of a restart", () => {
  const entries = [
    entry(0, "System DEV status: ...", { event: "status", data: { "DEV/00": "GREEN", "DEV/01": "GREEN" } }),
    entry(1, "Stop of DEV/00 requested", { event: "request_stop", subject: "DEV/00" }),
    entry(20, "System DEV status: ...", { event: "status", data: { "DEV/00": "GRAY", "DEV/01": "GREEN" } }),
    entry(21, "Stop of DEV/01 requested", { event: "request_stop", subject: "DEV/01" }),
    entry(30, "System DEV status: ...", { event: "status", data: { "DEV/00": "GRAY", "DEV/01": "GRAY" } }),
    entry(31, "Start of DEV/01 requested", { event: "request_start", subject: "DEV/01" }),
    entry(50, "System DEV status: ...", { event: "status", data: { "DEV/00": "GRAY", "DEV/01": "GREEN" } }),
    entry(51, "Start of DEV/00 requested", { event: "request_start", subject: "DEV/00" }),
  ];

  it("has a lane per instance with stop and start", () => {
    const timeline = buildTimeline(activity(entries, { status: "RUNNING", finished_at: null }), T0 + 60_000);
    expect(timeline.lanes.map((l) => l.subject)).toEqual(["DEV/00", "DEV/01"]);
    const [app, central] = timeline.lanes;
    expect(app.segments.map((s) => [s.kind, s.from - T0, s.to && s.to - T0, s.state])).toEqual([
      ["stop", 1000, 20000, "done"],
      ["start", 51000, null, "running"],
    ]);
    expect(central.segments.map((s) => s.state)).toEqual(["done", "done"]);
    expect(central.status).toBe("GREEN");
    expect(timeline.running).toBe(true);
  });

  it("closes open segments when the activity has failed", () => {
    const timeline = buildTimeline(activity(entries, { status: "FAILED" }));
    const last = timeline.lanes[0].segments[1];
    expect(last.to! - T0).toBe(100_000);
    expect(last.state).toBe("failed");
  });
});

describe("timeline of a custom operation", () => {
  it("runs from start to the hook result per subject", () => {
    const entries = [
      entry(0, "Copy kernel: host agent operation X, parameters none"),
      entry(1, "DEV/00: Started on sapapp1, operation 1", { event: "op_started", subject: "DEV/00" }),
      entry(2, "DEV/01: Started on sapapp1, operation 2", { event: "op_started", subject: "DEV/01" }),
      entry(5, "DEV/00: saphostcontrol: X hook successfully executed", { subject: "DEV/00" }),
      entry(9, "DEV/01: saphostcontrol: X hook failed (exit code = 3)", { subject: "DEV/01", level: "ERROR" }),
    ];
    const timeline = buildTimeline(activity(entries, { kind: "CUSTOM", status: "FAILED" }));
    expect(timeline.lanes.map((l) => [l.subject, l.segments[0].state, l.segments[0].to! - T0])).toEqual([
      ["DEV/00", "done", 5000],
      ["DEV/01", "failed", 9000],
    ]);
  });
});

describe("timeline of a mass operation", () => {
  it("has a lane per child activity", () => {
    const child = (n: number, status: string, from: number, to: number | null) => ({
      ...activity([]),
      id: n,
      target_label: `S${n}`,
      status,
      started_at: new Date(T0 + from * 1000).toISOString(),
      finished_at: to === null ? null : new Date(T0 + to * 1000).toISOString(),
    });
    const timeline = buildTimeline(
      activity([], { target_type: "MASS", status: "RUNNING", finished_at: null, children: [child(2, "SUCCEEDED", 0, 10), child(3, "RUNNING", 10, null)] }),
      T0 + 20_000,
    );
    expect(timeline.lanes.map((l) => [l.subject, l.segments[0].state, l.segments[0].activityId])).toEqual([
      ["S2", "done", 2],
      ["S3", "running", 3],
    ]);
  });
});

describe("helpers", () => {
  it("places segments and formats durations", () => {
    const timeline = { lanes: [], start: 0, end: 100, running: false };
    expect(place({ kind: "op", from: 25, to: 75, state: "done" }, timeline)).toEqual({ left: 25, width: 50 });
    expect(formatDuration(65_000)).toBe("1:05");
    expect(formatDuration(3_725_000)).toBe("1:02:05");
  });
});

describe("timeline of a template", () => {
  it("has a lane per step, named from the stored template", () => {
    const child = (n: number, kind: string, status: string, from: number, to: number) => ({
      ...activity([]),
      id: n,
      kind,
      status,
      started_at: new Date(T0 + from * 1000).toISOString(),
      finished_at: new Date(T0 + to * 1000).toISOString(),
    });
    const options = { template: { target: "SYSTEM", steps: [{ operation: "copy", custom: { name: "Copy kernel" } }, { operation: "stop", custom: null }] } };
    const timeline = buildTimeline(
      activity([], { kind: "TEMPLATE", status: "FAILED", options, children: [child(2, "CUSTOM", "SUCCEEDED", 0, 5), child(3, "STOP", "FAILED", 5, 9), child(4, "START", "SUCCEEDED", 9, 12)] }),
    );
    expect(timeline.lanes.map((l) => [l.subject, l.segments[0].state])).toEqual([
      ["1. Copy kernel", "done"],
      ["2. op.stop_system", "failed"],
      ["kind.START: " + activity([]).target_label, "done"],
    ]);
  });

  it("names the children of parallel steps from the step entries of the log", () => {
    const child = (n: number, kind: string) => ({ ...activity([]), id: n, kind, status: "RUNNING", started_at: new Date(T0).toISOString(), finished_at: null });
    const options = {
      template: {
        selection: "SYSTEM",
        steps: [
          { position: 1, operation: "discover", target_type: "HOST", custom: null },
          { position: 2, operation: "discover", target_type: "HOST", custom: null },
          { position: 3, operation: "restart", target_type: "SYSTEM", custom: null },
        ],
      },
    };
    const step = (position: number, child: number) => ({
      id: position,
      timestamp: new Date(T0).toISOString(),
      level: "INFO",
      message: `Step ${position}/3`,
      event: "step",
      subject: String(position),
      data: { activity: String(child) },
    });
    const timeline = buildTimeline(
      activity([step(2, 5), step(1, 6)], { kind: "TEMPLATE", status: "RUNNING", finished_at: null, options, children: [child(5, "DISCOVER"), child(6, "DISCOVER")] }),
      T0 + 1000,
    );
    expect(timeline.lanes.map((l) => l.subject)).toEqual(["2. op.discover", "1. op.discover"]);
  });
});
