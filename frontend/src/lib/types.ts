// Shapes of the API responses the interface uses (see landscape.py and api/schemas.py).

export type TargetType = "SYSTEM" | "INSTANCE" | "HOST";

export interface ActivityRef {
  id: number;
  kind: string;
  status: string;
}

export interface InstanceRow {
  id: number;
  label: string;
  sid: string;
  number: string;
  host: { id: number; name: string; tags: string[] };
  hostname: string;
  role: "database" | "central" | "application" | "agent";
  type: string | null;
  agent: boolean;
  status: string;
  status_checked_at: string | null;
  start_priority: string | null;
  version: string | null;
  depends_on: string[];
}

export interface SystemRow {
  id: number;
  name: string;
  description: string | null;
  status: string;
  pool: { id: number; name: string } | null;
  tags: string[];
  depends_on: string[];
  activity: ActivityRef | null;
  instances: InstanceRow[];
}

export interface HostRow {
  id: number;
  name: string;
  address: string;
  reachable: boolean | null;
  hostagent_version: string | null;
  os: string | null;
  tags: string[];
  last_discovery_at: string | null;
  last_error: string | null;
  instance_count: number;
  activity: ActivityRef | null;
}

export interface Parameter {
  name: string;
  label: string;
  type: "STRING" | "INTEGER" | "BOOLEAN";
  required: boolean;
  default: string | null;
  pattern: string | null;
  help: string | null;
}

export interface OperationDef {
  key: string;
  labels: Partial<Record<TargetType, string>>;
  name: string | null;
  description: string | null;
  targets: TargetType[];
  mass: boolean;
  danger: boolean;
  custom: boolean;
  template?: boolean;
  steps?: string[];
  instance_types: string[];
  parameters: Parameter[];
}

export interface Snapshot {
  user: { name: string; role: string };
  systems: SystemRow[];
  hosts: HostRow[];
  pools: { id: number; name: string }[];
  operations: OperationDef[];
}

export interface Process {
  name: string;
  description: string;
  status: string;
  text: string;
  started: string;
  elapsed: string;
  pid: number;
}

export interface ProcessList {
  status: string;
  processes: Process[];
  error: string | null;
}

export interface Activity {
  id: number;
  kind: string;
  target_type: string;
  target_label: string;
  requested_by: string | null;
  system_id: number | null;
  instance_id: number | null;
  host_id: number | null;
  parent_id: number | null;
  status: string;
  timeout_seconds: number;
  error: string | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface LogEntry {
  id: number;
  timestamp: string;
  level: string;
  message: string;
  event: string | null;
  subject: string | null;
  data: Record<string, string> | null;
}

export interface ActivityDetail extends Activity {
  entries: LogEntry[];
  children: Activity[];
  options: Record<string, unknown> | null;
}

export interface TemplateStep {
  position?: number;
  operation: string;
  name?: string | null;
  parameters: Record<string, string>;
  on_error: "stop" | "continue" | "stop_and_start";
}

export interface Template {
  id: number;
  key: string;
  name: string;
  description: string | null;
  target: "SYSTEM" | "HOST";
  role: string;
  mass: boolean;
  steps: TemplateStep[];
  valid: boolean;
}

export interface ScheduleRow {
  id: number;
  operation: string;
  target_type: TargetType;
  target_ids: number[];
  parameters: Record<string, string | number | boolean>;
  recurrence: "once" | "daily" | "interval";
  run_at: string | null;
  time_of_day: string | null;
  weekdays: string;
  interval_minutes: number | null;
  enabled: boolean;
  created_by: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_activity_id: number | null;
  last_error: string | null;
}
