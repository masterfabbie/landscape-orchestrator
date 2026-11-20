// Panel on the right with the details of a system or host. For a system it reads the
// processes of each instance live from its sapstartsrv (GetProcessList).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { hasRole } from "../lib/i18n";
import type { HostRow, InstanceRow, OperationDef, ProcessList, SystemRow } from "../lib/types";
import { reachOf } from "./filter";
import { type Selection, operationLabel, operationsFor } from "./selection";
import { Tags } from "./SystemsTable";
import type { Toast } from "./Toasts";
import { StatusBadge, StatusDot, formatTime, useApp } from "./ui";

type Run = (op: OperationDef, selection: Selection) => void;

function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !document.querySelector("dialog[open]")) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

function Header({ title, onClose, children }: { title: string; onClose: () => void; children?: React.ReactNode }) {
  const { t } = useApp();
  return (
    <div className="lo-drawer-head">
      <h2>{title}</h2>
      {children}
      <button type="button" className="lo-close" onClick={onClose} aria-label={t("ui.close")}>
        ×
      </button>
    </div>
  );
}

function Operations({ operations, selection, onRun }: { operations: OperationDef[]; selection: Selection; onRun: Run }) {
  const { t } = useApp();
  const available = operationsFor(operations, selection, new Map());
  if (!available.length) return null;
  return (
    <div className="lo-drawer-ops">
      {available.map((op) => (
        <button
          key={op.key}
          type="button"
          className={op.danger ? "lo-danger" : "secondary"}
          onClick={() => {
            if (!op.danger || window.confirm(t("op.confirm"))) onRun(op, selection);
          }}
        >
          {operationLabel(op, selection.kind, t)}
          {op.custom && op.parameters.length ? "…" : ""}
        </button>
      ))}
    </div>
  );
}

function Processes({ instance }: { instance: InstanceRow }) {
  const { t } = useApp();
  const { data, isPending, isError } = useQuery({
    queryKey: ["processes", instance.id],
    queryFn: () => api<ProcessList>(`/instances/${instance.id}/processes`),
    refetchInterval: 15_000,
  });
  if (isPending) return <p className="lo-muted">{t("ui.loading")}</p>;
  if (isError || !data) return <p className="error-text">{t("ui.load_failed")}</p>;
  if (data.error) return <p className="error-text">{t("ui.processes_unavailable")}: {data.error}</p>;
  if (!data.processes.length) return <p className="lo-muted">{t("ui.no_processes")}</p>;
  return (
    <table className="lo-processes">
      <thead>
        <tr>
          <th>{t("ui.process")}</th>
          <th>{t("field.status")}</th>
          <th>{t("ui.started_at")}</th>
          <th>PID</th>
        </tr>
      </thead>
      <tbody>
        {data.processes.map((p) => (
          <tr key={`${p.name}-${p.pid}`}>
            <td>
              <span className="lo-mono">{p.name}</span>
              <span className="lo-sub">{p.description}</span>
            </td>
            <td>
              <span className="lo-process-status">
                <StatusDot status={p.status} />
                {p.text}
              </span>
            </td>
            <td className="lo-muted">{p.started}</td>
            <td className="lo-mono">{p.pid || ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SystemDrawer({
  system,
  operations,
  onClose,
  onRun,
  notify,
}: {
  system: SystemRow | undefined;
  operations: OperationDef[];
  onClose: () => void;
  onRun: Run;
  notify: (toast: Omit<Toast, "id">) => void;
}) {
  const { t, role, language } = useApp();
  const queries = useQueryClient();
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [refreshing, setRefreshing] = useState(false);
  useEscape(onClose);
  useEffect(() => {
    // Main instances open, diagnostics agents closed, as in the landscape before.
    setOpen(new Set(system?.instances.filter((i) => !i.agent).map((i) => i.id) ?? []));
    // Only when another system is shown; reloads of the same system keep what is open.
  }, [system?.id]);

  if (!system) return null;
  const refresh = async () => {
    setRefreshing(true);
    try {
      const result = await api<{ warnings: string[] }>(`/systems/${system.id}/refresh`, { method: "POST" });
      for (const warning of result.warnings) notify({ kind: "error", text: warning });
      await queries.invalidateQueries({ queryKey: ["processes"] });
      await queries.invalidateQueries({ queryKey: ["landscape"] });
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <aside className="lo-drawer" aria-label={system.name}>
      <Header title={system.name} onClose={onClose}>
        <StatusBadge status={system.status} />
      </Header>
      {system.description && <p>{system.description}</p>}
      <dl className="lo-facts">
        {system.pool && (
          <>
            <dt>{t("field.pool")}</dt>
            <dd>{system.pool.name}</dd>
          </>
        )}
        <dt>{t("deps.depends_on")}</dt>
        <dd>{system.depends_on.join(", ") || t("deps.none")}</dd>
        {system.tags.length > 0 && (
          <>
            <dt>{t("field.tags")}</dt>
            <dd>
              <Tags tags={system.tags} />
            </dd>
          </>
        )}
        {system.activity && (
          <>
            <dt>{t("ui.activity")}</dt>
            <dd>
              <a href={`/activities/${system.activity.id}`}>
                {t(`kind.${system.activity.kind}`)} · {t(`activity.${system.activity.status}`)}
              </a>
            </dd>
          </>
        )}
      </dl>
      <Operations operations={operations} selection={{ kind: "SYSTEM", ids: [system.id] }} onRun={onRun} />
      <div className="lo-drawer-links">
        <button type="button" className="secondary" onClick={refresh} disabled={refreshing}>
          {t("ui.refresh_status")}
        </button>
        <a href={`/systems/${system.id}`}>{t("ui.open_page")}</a>
        {hasRole(role, "configurator") && <a href={`/configuration/systems/${system.id}`}>{t("action.configure")}</a>}
      </div>

      <h3>{t("ui.instances_in_order")}</h3>
      <ol className="lo-instances">
        {system.instances.map((instance) => {
          const isOpen = open.has(instance.id);
          return (
            <li key={instance.id} className={instance.agent ? "lo-agent" : undefined}>
              <button
                type="button"
                className="lo-instance-head"
                aria-expanded={isOpen}
                onClick={() =>
                  setOpen((current) => {
                    const next = new Set(current);
                    if (next.has(instance.id)) next.delete(instance.id);
                    else next.add(instance.id);
                    return next;
                  })
                }
              >
                <StatusDot status={instance.status} />
                <span className="lo-mono">{instance.label}</span>
                <span className="lo-sub">
                  {instance.type ? t(`itype.${instance.type}`) : t(`ui.role.${instance.role}`)} · {instance.host.name}
                </span>
              </button>
              {isOpen && (
                <div className="lo-instance-body">
                  <Operations operations={operations} selection={{ kind: "INSTANCE", ids: [instance.id] }} onRun={onRun} />
                  <Processes instance={instance} />
                  {instance.status_checked_at && (
                    <p className="lo-muted">{t("ui.checked_at", { time: formatTime(instance.status_checked_at, language) })}</p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

export function HostDrawer({
  host,
  systems,
  operations,
  onClose,
  onRun,
}: {
  host: HostRow | undefined;
  systems: SystemRow[];
  operations: OperationDef[];
  onClose: () => void;
  onRun: Run;
}) {
  const { t, role, language } = useApp();
  useEscape(onClose);
  if (!host) return null;
  const onHost = systems.flatMap((s) => s.instances.filter((i) => i.host.id === host.id).map((i) => ({ system: s, instance: i })));
  return (
    <aside className="lo-drawer" aria-label={host.name}>
      <Header title={host.name} onClose={onClose}>
        <StatusBadge status={reachOf(host)} prefix="reach" />
      </Header>
      <dl className="lo-facts">
        <dt>{t("field.address")}</dt>
        <dd className="lo-mono">{host.address}</dd>
        <dt>{t("field.hostagent")}</dt>
        <dd>{host.hostagent_version ?? "–"}</dd>
        <dt>{t("field.os")}</dt>
        <dd>{host.os ?? "–"}</dd>
        <dt>{t("field.last_discovery")}</dt>
        <dd>{formatTime(host.last_discovery_at, language) || t("hosts.never")}</dd>
        {host.last_error && (
          <>
            <dt>{t("field.last_error")}</dt>
            <dd className="error-text">{host.last_error}</dd>
          </>
        )}
      </dl>
      <Operations operations={operations} selection={{ kind: "HOST", ids: [host.id] }} onRun={onRun} />
      <div className="lo-drawer-links">
        <a href={`/hosts/${host.id}`}>{t("ui.open_page")}</a>
        {hasRole(role, "configurator") && <a href={`/configuration/hosts/${host.id}`}>{t("action.configure")}</a>}
      </div>
      <h3>{t("field.instances")}</h3>
      {onHost.length ? (
        <ul className="lo-instances">
          {onHost.map(({ system, instance }) => (
            <li key={instance.id}>
              <span className="lo-instance-head">
                <StatusDot status={instance.status} />
                <span className="lo-mono">{instance.label}</span>
                <span className="lo-sub">{system.name}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="lo-muted">{t("ui.no_instances_on_host")}</p>
      )}
    </aside>
  );
}
