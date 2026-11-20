// Operation templates: a list, and the composer for one template. Steps come from the
// building blocks on the left (built-in and custom operations for the target type), are
// ordered by drag and drop (or the arrow buttons) and get their parameters and behavior on
// error on the right. Below: the schedules of the template.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../lib/api";
import type { OperationDef, Snapshot, Template, TemplateStep } from "../lib/types";
import { useApp } from "../landscape/ui";
import { Schedules } from "./Schedules";

type Draft = Omit<Template, "id" | "valid"> & { id: number | null };

const EMPTY: Draft = {
  id: null,
  key: "",
  name: "",
  description: "",
  target: "SYSTEM",
  role: "operator",
  mass: true,
  steps: [],
};

const ROLES = ["viewer", "operator", "configurator", "admin"];

function currentId(): number | "new" | null {
  const params = new URLSearchParams(window.location.search);
  if (params.get("new") !== null) return "new";
  const id = Number(params.get("id"));
  return id > 0 ? id : null;
}

export function TemplatesPage() {
  const [selected, setSelected] = useState(currentId);
  const open = (id: number | "new" | null) => {
    const query = id === null ? "" : id === "new" ? "?new" : `?id=${id}`;
    window.history.pushState(null, "", `${window.location.pathname}${query}`);
    setSelected(id);
  };
  useEffect(() => {
    const onPop = () => setSelected(currentId());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return selected === null ? <TemplateList onOpen={open} /> : <Composer id={selected} onOpen={open} />;
}

// --- list ------------------------------------------------------------------------------

function TemplateList({ onOpen }: { onOpen: (id: number | "new") => void }) {
  const { t } = useApp();
  const { data, isPending } = useQuery({ queryKey: ["templates"], queryFn: () => api<Template[]>("/templates") });
  return (
    <div className="lo-templates">
      <p className="crumbs">
        <a href="/configuration">{t("nav.configuration")}</a>
      </p>
      <div className="lo-activity-head">
        <h1>{t("tpl.title")}</h1>
        <button type="button" className="lo-primary" onClick={() => onOpen("new")}>
          {t("tpl.new")}
        </button>
      </div>
      <p className="lo-muted">{t("tpl.intro")}</p>
      {isPending ? (
        <p className="lo-muted">{t("ui.loading")}</p>
      ) : !data?.length ? (
        <p className="lo-empty">{t("tpl.empty")}</p>
      ) : (
        <div className="lo-table-wrap">
          <table className="lo-table">
            <thead>
              <tr>
                <th>{t("field.name")}</th>
                <th>{t("custom.key")}</th>
                <th>{t("field.target")}</th>
                <th>{t("tpl.steps")}</th>
                <th>{t("custom.role")}</th>
              </tr>
            </thead>
            <tbody>
              {data.map((tpl) => (
                <tr key={tpl.id}>
                  <td>
                    <button type="button" className="lo-open" onClick={() => onOpen(tpl.id)}>
                      {tpl.name}
                    </button>
                    {!tpl.valid && <span className="lo-sub error-text">{t("tpl.invalid")}</span>}
                    {tpl.description && <span className="lo-sub">{tpl.description}</span>}
                  </td>
                  <td className="lo-mono">{tpl.key}</td>
                  <td>{t(`target.${tpl.target}`)}</td>
                  <td>{tpl.steps.map((s) => s.name ?? s.operation).join(" → ")}</td>
                  <td>{t(`role.${tpl.role}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --- composer --------------------------------------------------------------------------

function blockLabel(op: OperationDef, target: string, t: (k: string) => string): string {
  return op.custom ? (op.name ?? op.key) : t(op.labels[target as "SYSTEM"] ?? `op.${op.key}`);
}

function Composer({ id, onOpen }: { id: number | "new"; onOpen: (id: number | "new" | null) => void }) {
  const { t } = useApp();
  const queries = useQueryClient();
  const landscape = useQuery({ queryKey: ["landscape"], queryFn: () => api<Snapshot>("/landscape") });
  const loaded = useQuery({
    queryKey: ["template", id],
    queryFn: () => api<Template>(`/templates/${id}`),
    enabled: id !== "new",
  });
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [dirty, setDirty] = useState(false);
  const [current, setCurrent] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState<number | null>(null);

  useEffect(() => {
    if (id === "new") setDraft(EMPTY);
    else if (loaded.data) setDraft({ ...loaded.data, description: loaded.data.description ?? "" });
    setDirty(false);
    setCurrent(null);
  }, [id, loaded.data]);

  const blocks = useMemo(
    () =>
      (landscape.data?.operations ?? []).filter((op) => !op.template && op.targets.includes(draft.target)),
    [landscape.data, draft.target],
  );
  const byKey = useMemo(() => new Map((landscape.data?.operations ?? []).map((op) => [op.key, op])), [landscape.data]);

  const change = (next: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...next }));
    setDirty(true);
  };
  const setSteps = (steps: TemplateStep[]) => change({ steps });
  const add = (op: OperationDef) => {
    setSteps([...draft.steps, { operation: op.key, parameters: {}, on_error: "stop" }]);
    setCurrent(draft.steps.length);
  };
  const move = (from: number, to: number) => {
    if (to < 0 || to >= draft.steps.length || from === to) return;
    const steps = [...draft.steps];
    const [step] = steps.splice(from, 1);
    steps.splice(to, 0, step);
    setSteps(steps);
    setCurrent(to);
  };
  const remove = (index: number) => {
    setSteps(draft.steps.filter((_, i) => i !== index));
    setCurrent(null);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    const body = {
      key: draft.key,
      name: draft.name,
      description: draft.description || null,
      target: draft.target,
      role: draft.role,
      mass: draft.mass,
      steps: draft.steps.map(({ operation, parameters, on_error }) => ({
        operation,
        on_error,
        parameters: Object.fromEntries(Object.entries(parameters).filter(([, v]) => v !== "")),
      })),
    };
    try {
      const saved =
        draft.id === null
          ? await api<Template>("/templates", { method: "POST", body })
          : await api<Template>(`/templates/${draft.id}`, { method: "PUT", body });
      await queries.invalidateQueries({ queryKey: ["templates"] });
      await queries.invalidateQueries({ queryKey: ["landscape"] });
      queries.setQueryData(["template", saved.id], saved);
      setDirty(false);
      if (draft.id === null) onOpen(saved.id);
      else setDraft({ ...saved, description: saved.description ?? "" });
    } catch (exc) {
      const e = exc as ApiError;
      setError(e.code ? `${t(`error.${e.code}`)} ${e.message}` : e.message);
    } finally {
      setSaving(false);
    }
  };

  const destroy = async () => {
    if (draft.id === null || !window.confirm(t("tpl.delete_confirm"))) return;
    try {
      await api(`/templates/${draft.id}`, { method: "DELETE" });
      await queries.invalidateQueries({ queryKey: ["templates"] });
      onOpen(null);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  if (id !== "new" && loaded.isPending) return <p className="lo-muted">{t("ui.loading")}</p>;
  const step = current !== null ? draft.steps[current] : undefined;
  const stepOp = step ? byKey.get(step.operation) : undefined;

  return (
    <div className="lo-templates">
      <p className="crumbs">
        <a href="/configuration">{t("nav.configuration")}</a>
        {" / "}
        <a
          href="?"
          onClick={(event) => {
            event.preventDefault();
            if (!dirty || window.confirm(t("tpl.discard"))) onOpen(null);
          }}
        >
          {t("tpl.title")}
        </a>
      </p>
      <div className="lo-activity-head">
        <h1>{draft.name || t("tpl.new")}</h1>
        <span className="lo-muted">{dirty ? t("tpl.unsaved") : draft.id !== null ? t("tpl.saved") : ""}</span>
        <span className="lo-head-buttons">
          {draft.id !== null && (
            <button type="button" className="lo-danger-outline" onClick={destroy}>
              {t("tpl.delete")}
            </button>
          )}
          <button type="button" className="lo-primary" onClick={save} disabled={saving || !dirty}>
            {t("action.save")}
          </button>
        </span>
      </div>
      {error && (
        <p className="flash error" role="alert">
          {error}
        </p>
      )}

      <section className="lo-card lo-fields-row">
        <label>
          {t("field.name")}
          <input value={draft.name} maxLength={128} onChange={(e) => change({ name: e.target.value })} />
        </label>
        <label>
          {t("custom.key")}
          <input
            value={draft.key}
            maxLength={64}
            pattern="[a-z][a-z0-9_]*"
            className="lo-mono"
            placeholder="kernel_update"
            onChange={(e) => change({ key: e.target.value })}
          />
        </label>
        <label>
          {t("field.target")}
          <select
            value={draft.target}
            onChange={(e) => {
              const target = e.target.value as Draft["target"];
              if (draft.steps.length && !window.confirm(t("tpl.target_change"))) return;
              change({ target, steps: [] });
              setCurrent(null);
            }}
          >
            <option value="SYSTEM">{t("target.SYSTEM")}</option>
            <option value="HOST">{t("target.HOST")}</option>
          </select>
        </label>
        <label>
          {t("custom.role")}
          <select value={draft.role} onChange={(e) => change({ role: e.target.value })}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {t(`role.${r}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="lo-wide">
          {t("field.description")}
          <input value={draft.description ?? ""} maxLength={1000} onChange={(e) => change({ description: e.target.value })} />
        </label>
        <label className="lo-inline">
          <input type="checkbox" checked={draft.mass} onChange={(e) => change({ mass: e.target.checked })} /> {t("custom.mass")}
        </label>
      </section>

      <div className="lo-composer">
        <aside className="lo-blocks" aria-label={t("tpl.blocks")}>
          <h2>{t("tpl.blocks")}</h2>
          <p className="lo-muted">{t("tpl.blocks_hint")}</p>
          {blocks.map((op) => (
            <button
              key={op.key}
              type="button"
              className="lo-block"
              draggable
              onDragStart={(event: DragEvent) => event.dataTransfer.setData("text/lo-block", op.key)}
              onClick={() => add(op)}
            >
              <span>{blockLabel(op, draft.target, t)}</span>
              <span className="lo-sub">{op.custom ? t("tpl.kind_custom") : t("tpl.kind_builtin")}</span>
            </button>
          ))}
        </aside>

        <main
          className="lo-chain"
          aria-label={t("tpl.steps")}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            const key = event.dataTransfer.getData("text/lo-block");
            const op = key ? byKey.get(key) : undefined;
            if (op) add(op);
          }}
        >
          <p className="lo-muted">{t(draft.target === "SYSTEM" ? "tpl.chain_hint_system" : "tpl.chain_hint_host")}</p>
          {!draft.steps.length && <p className="lo-drop">{t("tpl.drop_here")}</p>}
          <ol className="lo-steps">
            {draft.steps.map((s, index) => {
              const op = byKey.get(s.operation);
              return (
                <li
                  key={`${index}-${s.operation}`}
                  className={`lo-step${current === index ? " lo-on" : ""}${dragging === index ? " lo-dragging" : ""}`}
                  draggable
                  onDragStart={(event) => {
                    setDragging(index);
                    event.dataTransfer.setData("text/lo-step", String(index));
                  }}
                  onDragEnd={() => setDragging(null)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={(event) => {
                    const from = event.dataTransfer.getData("text/lo-step");
                    if (from !== "") {
                      event.stopPropagation();
                      move(Number(from), index);
                    }
                  }}
                >
                  <span className="lo-step-number">{index + 1}</span>
                  <button type="button" className="lo-step-body" onClick={() => setCurrent(index)}>
                    <span className="lo-step-name">{op ? blockLabel(op, draft.target, t) : s.operation}</span>
                    <span className="lo-sub">
                      {op?.custom ? t("tpl.kind_custom") : t("tpl.kind_builtin")} · {t(`tpl.on_error.${s.on_error}`)}
                      {Object.keys(s.parameters).length
                        ? ` · ${Object.entries(s.parameters)
                            .map(([k, v]) => `${k}=${v}`)
                            .join(", ")}`
                        : ""}
                    </span>
                  </button>
                  <span className="lo-step-tools">
                    <button type="button" aria-label={t("tpl.up")} onClick={() => move(index, index - 1)} disabled={index === 0}>
                      ↑
                    </button>
                    <button type="button" aria-label={t("tpl.down")} onClick={() => move(index, index + 1)} disabled={index === draft.steps.length - 1}>
                      ↓
                    </button>
                    <button type="button" aria-label={t("tpl.remove")} onClick={() => remove(index)}>
                      ×
                    </button>
                  </span>
                </li>
              );
            })}
          </ol>
        </main>

        <aside className="lo-step-editor" aria-label={t("tpl.step")}>
          {step && current !== null ? (
            <section className="lo-card">
              <h2>{t("tpl.step_title", { n: current + 1, name: stepOp ? blockLabel(stepOp, draft.target, t) : step.operation })}</h2>
              {stepOp?.description && <p className="lo-muted">{stepOp.description}</p>}
              <div className="lo-fields">
                {(stepOp?.parameters ?? []).map((p) => (
                  <label key={p.name}>
                    <span>
                      {p.label} <span className="lo-mono lo-muted">{p.name}</span>
                    </span>
                    {p.type === "BOOLEAN" ? (
                      <select
                        value={step.parameters[p.name] ?? ""}
                        onChange={(e) => {
                          const steps = [...draft.steps];
                          steps[current] = { ...step, parameters: { ...step.parameters, [p.name]: e.target.value } };
                          setSteps(steps);
                        }}
                      >
                        <option value="">{t("tpl.param_default", { value: p.default ?? "false" })}</option>
                        <option value="true">{t("yes")}</option>
                        <option value="false">{t("no")}</option>
                      </select>
                    ) : (
                      <input
                        value={step.parameters[p.name] ?? ""}
                        placeholder={p.default ?? ""}
                        inputMode={p.type === "INTEGER" ? "numeric" : undefined}
                        onChange={(e) => {
                          const steps = [...draft.steps];
                          steps[current] = { ...step, parameters: { ...step.parameters, [p.name]: e.target.value } };
                          setSteps(steps);
                        }}
                      />
                    )}
                    {p.help && <small className="lo-muted">{p.help}</small>}
                  </label>
                ))}
                {!stepOp?.parameters.length && <p className="lo-muted">{t("tpl.no_parameters")}</p>}
                <label>
                  {t("tpl.on_error")}
                  <select
                    value={step.on_error}
                    onChange={(e) => {
                      const steps = [...draft.steps];
                      steps[current] = { ...step, on_error: e.target.value as TemplateStep["on_error"] };
                      setSteps(steps);
                    }}
                  >
                    <option value="stop">{t("tpl.on_error.stop")}</option>
                    <option value="continue">{t("tpl.on_error.continue")}</option>
                    {draft.target === "SYSTEM" && <option value="stop_and_start">{t("tpl.on_error.stop_and_start")}</option>}
                  </select>
                </label>
              </div>
              <p className="lo-muted">{t("tpl.values_hint")}</p>
            </section>
          ) : (
            <p className="lo-muted lo-card">{t("tpl.choose_step")}</p>
          )}
          {draft.id !== null && landscape.data && (
            <Schedules operation={draft.key} target={draft.target} snapshot={landscape.data} disabled={dirty} />
          )}
        </aside>
      </div>
    </div>
  );
}
