// Systems with their instances; a system opens to show its instances (closed at first, open
// while the instance type filter is active). Systems or instances can be selected.

import { useEffect, useState } from "react";
import type { InstanceRow, SystemRow, TargetType } from "../lib/types";
import type { Selection } from "./selection";
import { StatusBadge, StatusDot, useApp } from "./ui";

interface Props {
  rows: { system: SystemRow; instances: InstanceRow[] }[];
  expandAll: boolean;
  selection: Selection;
  onSelect: (kind: TargetType, ids: number[], add: boolean) => void;
  onOpen: (id: number) => void;
  openId: number | null;
}

const OPEN_KEY = "lo.landscape.open";

function loadOpen(): Set<number> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(OPEN_KEY) ?? "[]") as number[]);
  } catch {
    return new Set();
  }
}

export function SystemsTable({ rows, expandAll, selection, onSelect, onOpen, openId }: Props) {
  const { t } = useApp();
  const [open, setOpen] = useState<Set<number>>(loadOpen);
  useEffect(() => {
    try {
      sessionStorage.setItem(OPEN_KEY, JSON.stringify([...open]));
    } catch {
      // storage not available: the tree only forgets what was open
    }
  }, [open]);

  const selected = new Set(selection.ids);
  const systemIds = rows.map((r) => r.system.id);
  const allSystems =
    selection.kind === "SYSTEM" && systemIds.length > 0 && systemIds.every((id) => selected.has(id));
  const toggle = (id: number) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!rows.length) return <p className="lo-empty">{t("filter.no_match")}</p>;

  return (
    <div className="lo-table-wrap">
      <div className="lo-tree-tools">
        <button type="button" className="link" onClick={() => setOpen(new Set(systemIds))}>
          {t("tree.expand_all")}
        </button>
        <button type="button" className="link" onClick={() => setOpen(new Set())}>
          {t("tree.collapse_all")}
        </button>
      </div>
      <table className="lo-table">
        <thead>
          <tr>
            <th className="lo-check">
              <input
                type="checkbox"
                aria-label={t("ui.select_all_systems")}
                checked={allSystems}
                onChange={(e) => onSelect("SYSTEM", systemIds, e.target.checked)}
              />
            </th>
            <th>{t("field.system")}</th>
            <th>{t("field.status")}</th>
            <th>{t("field.instances")}</th>
            <th>{t("field.pool")}</th>
            <th>{t("deps.depends_on")}</th>
            <th>{t("field.tags")}</th>
            <th>{t("ui.activity")}</th>
          </tr>
        </thead>
        {rows.map(({ system, instances }) => {
          const isOpen = expandAll || open.has(system.id);
          const hosts = [...new Set(system.instances.map((i) => i.host.name))];
          return (
            <tbody key={system.id} className={openId === system.id ? "lo-current" : undefined}>
              <tr className={`lo-system${selection.kind === "SYSTEM" && selected.has(system.id) ? " lo-selected" : ""}`}>
                <td className="lo-check">
                  <input
                    type="checkbox"
                    aria-label={t("ui.select", { name: system.name })}
                    checked={selection.kind === "SYSTEM" && selected.has(system.id)}
                    onChange={(e) => onSelect("SYSTEM", [system.id], e.target.checked)}
                  />
                </td>
                <td>
                  <span className="lo-name-cell">
                    <button
                      type="button"
                      className="lo-toggle"
                      aria-expanded={isOpen}
                      aria-label={t("tree.toggle")}
                      disabled={expandAll}
                      onClick={() => toggle(system.id)}
                    >
                      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                        <path d={isOpen ? "M2 4l4 4 4-4" : "M4 2l4 4-4 4"} fill="none" stroke="currentColor" strokeWidth="1.6" />
                      </svg>
                    </button>
                    <button type="button" className="lo-open" onClick={() => onOpen(system.id)}>
                      {system.name}
                    </button>
                  </span>
                  {system.description && <span className="lo-sub">{system.description}</span>}
                </td>
                <td>
                  <StatusBadge status={system.status} />
                </td>
                <td>
                  <span className="lo-squares" aria-label={t("ui.instance_count", { count: system.instances.length })}>
                    {system.instances.map((i) => (
                      <StatusDot key={i.id} status={i.status} />
                    ))}
                  </span>
                  <span className="lo-sub">{hosts.join(", ")}</span>
                </td>
                <td>{system.pool?.name ?? ""}</td>
                <td className="lo-mono">{system.depends_on.join(", ")}</td>
                <td>
                  <Tags tags={system.tags} />
                </td>
                <td>
                  {system.activity && (
                    <a className="lo-badge lo-badge-running" href={`/activities/${system.activity.id}`}>
                      {t(`kind.${system.activity.kind}`)} · {t(`activity.${system.activity.status}`)}
                    </a>
                  )}
                </td>
              </tr>
              {isOpen &&
                instances.map((instance) => {
                  const checked = selection.kind === "INSTANCE" && selected.has(instance.id);
                  return (
                    <tr key={instance.id} className={`lo-instance${checked ? " lo-selected" : ""}${instance.agent ? " lo-agent" : ""}`}>
                      <td className="lo-check">
                        <input
                          type="checkbox"
                          aria-label={t("ui.select", { name: instance.label })}
                          checked={checked}
                          onChange={(e) => onSelect("INSTANCE", [instance.id], e.target.checked)}
                        />
                      </td>
                      <td className="lo-indent">
                        <span className="lo-mono">{instance.label}</span>
                        <span className="lo-sub">
                          {instance.type ? t(`itype.${instance.type}`) : t(`ui.role.${instance.role}`)}
                        </span>
                      </td>
                      <td>
                        <StatusBadge status={instance.status} />
                      </td>
                      <td>
                        <a href={`/hosts/${instance.host.id}`}>{instance.host.name}</a>
                        {instance.hostname !== instance.host.name && <span className="lo-sub">{instance.hostname}</span>}
                      </td>
                      <td></td>
                      <td className="lo-mono">{instance.depends_on.join(", ")}</td>
                      <td></td>
                      <td></td>
                    </tr>
                  );
                })}
            </tbody>
          );
        })}
      </table>
    </div>
  );
}

export function Tags({ tags }: { tags: string[] }) {
  return (
    <span className="lo-tags">
      {tags.map((tag) => (
        <span key={tag} className="lo-tag">
          {tag}
        </span>
      ))}
    </span>
  );
}
