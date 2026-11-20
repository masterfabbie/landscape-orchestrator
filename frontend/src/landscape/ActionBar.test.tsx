import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InstanceRow } from "../lib/types";
import { ActionBar } from "./ActionBar";
import { DEV, OPERATIONS } from "./fixtures";
import { AppContext } from "./ui";

const t = (key: string, values?: Record<string, string | number>) =>
  values ? `${key}:${Object.values(values).join(",")}` : key;
const instances = new Map<number, InstanceRow>(DEV.instances.map((i) => [i.id, i]));

function bar(ids: number[], onRun = vi.fn()) {
  render(
    <AppContext.Provider value={{ t, role: "admin", language: "en" }}>
      <ActionBar selection={{ kind: "SYSTEM", ids }} operations={OPERATIONS} instances={instances} onRun={onRun} onClear={() => {}} />
    </AppContext.Provider>,
  );
  return onRun;
}

describe("action bar", () => {
  afterEach(cleanup);

  it("is hidden without a selection", () => {
    bar([]);
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("runs a mass operation with its settings", () => {
    const onRun = bar([1, 2]);
    expect(screen.getByText("ui.selected_system:2")).toBeTruthy();
    fireEvent.click(screen.getByText("op.start_system"));
    expect(onRun).toHaveBeenCalledWith(OPERATIONS[0], { kind: "SYSTEM", ids: [1, 2] }, { parallel: 5, stopOnError: false });
  });

  it("asks a second time before stopping", () => {
    const onRun = bar([1]);
    fireEvent.click(screen.getByText("op.stop_system"));
    expect(onRun).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("ui.confirm_operation:op.stop_system"));
    expect(onRun).toHaveBeenCalledTimes(1);
  });
});
