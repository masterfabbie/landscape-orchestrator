import type { HostRow, TargetType } from "../lib/types";
import { reachOf } from "./filter";
import type { Selection } from "./selection";
import { Tags } from "./SystemsTable";
import { StatusBadge, formatTime, useApp } from "./ui";

interface Props {
  rows: HostRow[];
  selection: Selection;
  onSelect: (kind: TargetType, ids: number[], add: boolean) => void;
  onOpen: (id: number) => void;
  openId: number | null;
}

export function HostsTable({ rows, selection, onSelect, onOpen, openId }: Props) {
  const { t, language } = useApp();
  const selected = new Set(selection.kind === "HOST" ? selection.ids : []);
  const ids = rows.map((h) => h.id);
  if (!rows.length) return <p className="lo-empty">{t("filter.no_match")}</p>;
  return (
    <div className="lo-table-wrap">
      <table className="lo-table">
        <thead>
          <tr>
            <th className="lo-check">
              <input
                type="checkbox"
                aria-label={t("ui.select_all_hosts")}
                checked={ids.every((id) => selected.has(id))}
                onChange={(e) => onSelect("HOST", ids, e.target.checked)}
              />
            </th>
            <th>{t("field.host")}</th>
            <th>{t("field.reachable")}</th>
            <th>{t("field.hostagent")}</th>
            <th>{t("field.os")}</th>
            <th>{t("field.instances")}</th>
            <th>{t("field.tags")}</th>
            <th>{t("field.last_discovery")}</th>
            <th>{t("ui.activity")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((host) => (
            <tr
              key={host.id}
              className={`${selected.has(host.id) ? "lo-selected" : ""}${openId === host.id ? " lo-current" : ""}`}
            >
              <td className="lo-check">
                <input
                  type="checkbox"
                  aria-label={t("ui.select", { name: host.name })}
                  checked={selected.has(host.id)}
                  onChange={(e) => onSelect("HOST", [host.id], e.target.checked)}
                />
              </td>
              <td>
                <button type="button" className="lo-open" onClick={() => onOpen(host.id)}>
                  {host.name}
                </button>
                {host.address !== host.name && <span className="lo-sub lo-mono">{host.address}</span>}
              </td>
              <td>
                <StatusBadge status={reachOf(host)} prefix="reach" />
              </td>
              <td>{host.hostagent_version ?? ""}</td>
              <td>{host.os ?? ""}</td>
              <td>{host.instance_count}</td>
              <td>
                <Tags tags={host.tags} />
              </td>
              <td className="lo-muted">{formatTime(host.last_discovery_at, language) || t("hosts.never")}</td>
              <td>
                {host.activity && (
                  <a className="lo-badge lo-badge-running" href={`/activities/${host.activity.id}`}>
                    {t(`kind.${host.activity.kind}`)}
                  </a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
