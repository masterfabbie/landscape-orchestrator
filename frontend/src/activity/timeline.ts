// Timeline of an activity: one lane per instance (or host, for custom operations), built
// from the structured log entries (event, subject, data; see operations.py). For a mass
// operation the lanes are its child activities.

import type { Activity, ActivityDetail, LogEntry } from "../lib/types";

export type SegmentKind = "stop" | "start" | "op" | "child";
export type SegmentState = "running" | "done" | "failed";

export interface Segment {
  kind: SegmentKind;
  from: number;
  to: number | null; // null: still running
  state: SegmentState;
  /** Status the instance must reach (start: GREEN, stop: GRAY). */
  target?: string;
  activityId?: number;
}

export interface Lane {
  subject: string;
  segments: Segment[];
  status: string | null; // last status seen
}

export interface Timeline {
  lanes: Lane[];
  start: number;
  end: number;
  running: boolean;
}

const ACTIVE = new Set(["PENDING", "RUNNING"]);
const time = (value: string) => new Date(value).getTime();

function lane(lanes: Map<string, Lane>, subject: string): Lane {
  let found = lanes.get(subject);
  if (!found) {
    found = { subject, segments: [], status: null };
    lanes.set(subject, found);
  }
  return found;
}

function open(l: Lane, kind?: SegmentKind): Segment | undefined {
  return [...l.segments].reverse().find((s) => s.to === null && (!kind || s.kind === kind));
}

function finish(timeline: Map<string, Lane>, activity: Activity, now: number): void {
  const running = ACTIVE.has(activity.status);
  const end = activity.finished_at ? time(activity.finished_at) : now;
  const failed = ["FAILED", "CANCELLED"].includes(activity.status);
  for (const l of timeline.values())
    for (const segment of l.segments)
      if (segment.to === null && !running) {
        segment.to = end;
        if (segment.state === "running") segment.state = failed ? "failed" : "done";
      }
}

export function buildTimeline(activity: ActivityDetail, now = Date.now()): Timeline {
  const running = ACTIVE.has(activity.status);
  const start = time(activity.started_at ?? activity.requested_at);
  const end = activity.finished_at ? time(activity.finished_at) : Math.max(now, start + 1000);
  const lanes = new Map<string, Lane>();

  if (activity.target_type === "MASS") {
    for (const child of activity.children) {
      const l = lane(lanes, child.target_label);
      l.status = child.status;
      if (!child.started_at) continue;
      l.segments.push({
        kind: "child",
        from: time(child.started_at),
        to: child.finished_at ? time(child.finished_at) : null,
        state: ACTIVE.has(child.status) ? "running" : child.status === "SUCCEEDED" ? "done" : "failed",
        activityId: child.id,
      });
    }
    finish(lanes, activity, now);
    return { lanes: [...lanes.values()], start, end, running };
  }

  for (const entry of [...activity.entries].sort(byTime)) {
    const at = time(entry.timestamp);
    if (entry.event === "request_start" || entry.event === "request_stop") {
      const l = lane(lanes, entry.subject ?? "?");
      const stop = entry.event === "request_stop";
      l.segments.push({ kind: stop ? "stop" : "start", from: at, to: null, state: "running", target: stop ? "GRAY" : "GREEN" });
    } else if (entry.event === "status" && entry.data) {
      for (const [label, status] of Object.entries(entry.data)) {
        const l = lanes.get(label);
        if (!l) continue; // only instances that were asked to change get a lane
        l.status = status;
        const segment = open(l);
        if (segment && segment.target === status) {
          segment.to = at;
          segment.state = "done";
        }
      }
    } else if (entry.subject) {
      // Lines of a custom operation on an instance or host.
      const l = lane(lanes, entry.subject);
      let segment = open(l, "op");
      if (!segment) {
        if (entry.event !== "op_started" && l.segments.some((s) => s.kind === "op")) continue;
        segment = { kind: "op", from: at, to: null, state: "running" };
        l.segments.push(segment);
      }
      if (/hook successfully executed/.test(entry.message)) {
        segment.to = at;
        segment.state = "done";
      } else if (/hook failed|cancelled on/.test(entry.message) || entry.level === "ERROR") {
        segment.to = at;
        segment.state = "failed";
      }
    }
  }
  finish(lanes, activity, now);
  return { lanes: [...lanes.values()].filter((l) => l.segments.length), start, end, running };
}

function byTime(a: LogEntry, b: LogEntry): number {
  return time(a.timestamp) - time(b.timestamp) || a.id - b.id;
}

/** Position of a segment as percentages of the whole timeline. */
export function place(segment: Segment, timeline: Timeline, now = Date.now()): { left: number; width: number } {
  const span = Math.max(timeline.end - timeline.start, 1);
  const to = segment.to ?? Math.min(now, timeline.end);
  const left = ((segment.from - timeline.start) / span) * 100;
  const width = Math.max(((to - segment.from) / span) * 100, 0.8);
  return { left: Math.min(Math.max(left, 0), 99.2), width: Math.min(width, 100 - Math.max(left, 0)) };
}

export function formatDuration(ms: number): string {
  const seconds = Math.max(Math.round(ms / 1000), 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
