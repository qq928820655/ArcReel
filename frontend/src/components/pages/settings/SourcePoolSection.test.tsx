import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { SourcePoolSection } from "./SourcePoolSection";

function stubCommon() {
  vi.spyOn(API, "getModelCandidates").mockResolvedValue({
    image: { default: [], buckets: { t2i: [], i2i: [] }, filtered: { t2i: [], i2i: [] } },
    video: {
      default: ["custom-2/seedance-2.0"],
      buckets: { i2v: ["custom-2/seedance-2.0"], r2v: [] },
      filtered: { i2v: [], r2v: [] },
    },
    provider_names: {},
    model_names: {},
    lifecycle: {},
  } as never);
  vi.spyOn(API, "getProviders").mockResolvedValue({ providers: [] } as never);
}

const EXISTING_POOL = {
  id: 1,
  provider_id: "pool-1",
  name: "双源池",
  model_name: null,
  capability_provider_id: null,
  capability_model_id: null,
  enabled: true,
  members: [{ position: 0, provider_id: "custom-2", model_id: "seedance-2.0" }],
};

beforeEach(() => {
  useAppStore.setState(useAppStore.getInitialState(), true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcePoolSection 保存反馈", () => {
  it("保存成功弹出成功提示", async () => {
    stubCommon();
    vi.spyOn(API, "listSourcePools").mockResolvedValue([EXISTING_POOL] as never);
    vi.spyOn(API, "updateSourcePool").mockResolvedValue(EXISTING_POOL as never);

    render(<SourcePoolSection />);

    const saveButton = await screen.findByRole("button", { name: "保存" });
    await userEvent.click(saveButton);

    await waitFor(() => expect(useAppStore.getState().toast?.text).toBe("汇总源已保存"));
    expect(useAppStore.getState().toast?.tone).toBe("success");
  });

  it("保存失败弹出错误提示", async () => {
    stubCommon();
    vi.spyOn(API, "listSourcePools").mockResolvedValue([EXISTING_POOL] as never);
    vi.spyOn(API, "updateSourcePool").mockRejectedValue(new Error("boom"));

    render(<SourcePoolSection />);

    const saveButton = await screen.findByRole("button", { name: "保存" });
    await userEvent.click(saveButton);

    await waitFor(() => expect(useAppStore.getState().toast?.tone).toBe("error"));
    expect(useAppStore.getState().toast?.text).toContain("汇总源保存失败");
  });
});
