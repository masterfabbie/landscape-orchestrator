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
