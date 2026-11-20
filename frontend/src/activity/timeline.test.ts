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
