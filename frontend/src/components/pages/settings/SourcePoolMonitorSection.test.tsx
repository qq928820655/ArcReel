import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { SourcePoolMonitorSection } from "./SourcePoolMonitorSection";

function stubCommon() {
  vi.spyOn(API, "listSourcePools").mockResolvedValue([
    {
      id: 1,
      provider_id: "pool-1",
      name: "双源池",
      model_name: null,
      capability_provider_id: null,
      capability_model_id: null,
      enabled: true,
      members: [],
    },
  ] as never);
  vi.spyOn(API, "getProviders").mockResolvedValue({ providers: [] } as never);
  vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [] } as never);
}

const TASK = {
  task_id: "t1",
  project_name: "demo",
  task_type: "video",
  resource_id: "E1S01",
  source_pool_id: 1,
  pool_name: "双源池",
  provider_id: "custom-5",
  model_id: "seedance-2.0",
  status: "succeeded",
  error_message: null,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:00Z",
  finished_at: "2026-01-01T00:01:30Z",
  call_count: 2,
  success_count: 1,
  failed_count: 1,
  last_call_finished_at: "2026-01-01T00:01:30Z",
};

beforeEach(() => {
  useAppStore.setState(useAppStore.getInitialState(), true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcePoolMonitorSection", () => {
  it("列出池任务：池名、真实成员、调用与耗时", async () => {
    stubCommon();
    vi.spyOn(API, "listSourcePoolTasks").mockResolvedValue({
      items: [TASK],
      total: 1,
      page: 1,
      page_size: 50,
    } as never);

    render(<SourcePoolMonitorSection />);

    // 「双源池」同时出现在筛选下拉与表格里，故用 getAllByText
    await waitFor(() => expect(screen.getAllByText("双源池").length).toBeGreaterThan(0));
    expect(screen.getByText("demo")).toBeInTheDocument();
    // 真实成员：custom-5（目录无译名时回退 id）
    expect(screen.getByText("custom-5")).toBeInTheDocument();
    // 调用 1 成功 / 2 次，耗时 1m 30s
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getByText("1m 30s")).toBeInTheDocument();
  });

  it("无池任务时显示空态", async () => {
    stubCommon();
    vi.spyOn(API, "listSourcePoolTasks").mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 50,
    } as never);

    render(<SourcePoolMonitorSection />);

    await waitFor(() => expect(screen.getByText("还没有任务经汇总源分配。")).toBeInTheDocument());
  });
});
