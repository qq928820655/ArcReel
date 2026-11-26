import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { SourcePriorityResponse } from "@/types/system";
import { SourcePrioritySection } from "./SourcePrioritySection";

const ENTRY = (provider_id: string, model_id: string) => ({ provider_id, model_id });

/** 项目层未配置、回落全局的读视图：source=global。 */
function fallbackResponse(): SourcePriorityResponse {
  return {
    scope: "global",
    video: {
      enabled: true,
      entries: [ENTRY("custom-10", "seedance-2.0-mini"), ENTRY("custom-5", "seedance-2.0"), ENTRY("custom-2", "seedance-2.0")],
      active_index: 0,
      active: true,
      source: "global",
    },
    image: { enabled: false, entries: [], active_index: 0, active: false, source: "global" },
    text: { enabled: false, entries: [], active_index: 0, active: false, source: "global" },
  };
}

/** 项目层已拥有该通道的读视图：source=project。 */
function ownedResponse(activeIndex: number): SourcePriorityResponse {
  const base = fallbackResponse();
  return {
    ...base,
    scope: "mixed",
    video: { ...base.video, active_index: activeIndex, source: "project" },
  };
}

function stubCommon() {
  vi.spyOn(API, "getModelCandidates").mockResolvedValue({
    image: { default: [], buckets: { t2i: [], i2i: [] }, filtered: { t2i: [], i2i: [] } },
    video: {
      default: ["custom-10/seedance-2.0-mini", "custom-5/seedance-2.0", "custom-2/seedance-2.0"],
      buckets: { i2v: ["custom-10/seedance-2.0-mini", "custom-5/seedance-2.0", "custom-2/seedance-2.0"], r2v: [] },
      filtered: { i2v: [], r2v: [] },
    },
    provider_names: { "custom-10": "seedance池-1", "custom-5": "b-dola", "custom-2": "a-dola" },
    model_names: {},
    lifecycle: {},
  } as never);
  vi.spyOn(API, "getProviders").mockResolvedValue({ providers: [] } as never);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcePrioritySection 项目层点行启用", () => {
  it("回落全局时点行，先落本项目配置再切行，不再触发 409", async () => {
    stubCommon();
    vi.spyOn(API, "getProjectSourcePriority").mockResolvedValue(fallbackResponse());
    const patchSpy = vi.spyOn(API, "updateProjectSourcePriority").mockResolvedValue(ownedResponse(0));
    const selectSpy = vi.spyOn(API, "selectProjectSourcePriority").mockResolvedValue(ownedResponse(2));

    render(<SourcePrioritySection scope="project" projectName="demo" />);

    // 第 0 行是当前启用行（按钮文案「启用中」），第 1、2 行是「待启用」；点第 2 行的按钮即第三行。
    const pendingButtons = await screen.findAllByRole("button", { name: "待启用" });
    expect(pendingButtons).toHaveLength(2);
    await userEvent.click(pendingButtons[1]);

    // 先落本项目配置（enabled=true + 当前列表），再切行到第 3 行。
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1));
    const patchArg = patchSpy.mock.calls[0][1];
    expect(patchArg.video?.enabled).toBe(true);
    expect(patchArg.video?.entries).toHaveLength(3);
    await waitFor(() => expect(selectSpy).toHaveBeenCalledWith("demo", "video", 2));
  });

  it("已由本项目拥有时点行，直接切行，不重复写配置", async () => {
    stubCommon();
    vi.spyOn(API, "getProjectSourcePriority").mockResolvedValue(ownedResponse(0));
    const patchSpy = vi.spyOn(API, "updateProjectSourcePriority").mockResolvedValue(ownedResponse(0));
    const selectSpy = vi.spyOn(API, "selectProjectSourcePriority").mockResolvedValue(ownedResponse(2));

    render(<SourcePrioritySection scope="project" projectName="demo" />);

    const pendingButtons = await screen.findAllByRole("button", { name: "待启用" });
    await userEvent.click(pendingButtons[1]);

    await waitFor(() => expect(selectSpy).toHaveBeenCalledWith("demo", "video", 2));
    expect(patchSpy).not.toHaveBeenCalled();
  });
});
