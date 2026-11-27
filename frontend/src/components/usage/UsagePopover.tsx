import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "wouter";
import { cn } from "cn";

import { settingsSectionPath } from "@/app-routes";
import { useShallow } from "zustand/react/shallow";

import { API } from "@/api";
import { TruncatedText } from "@/components/shared/TruncatedText";
import { Button, buttonVariants } from "@/components/ui/button";
import { PopoverContent } from "@/components/ui/popover";
import { Dialog, DialogBody, DialogContent } from "@/components/ui/dialog";
import { useAppStore } from "@/stores/app-store";
import { isOccupyingStatus, useTasksStore } from "@/stores/tasks-store";
import { useUsageHeaderStore } from "@/stores/usage-header-store";
import type { TaskItem, UsageSummary } from "@/types";
import { voidPromise } from "@/utils/async";
import { formatCurrencyAmount } from "@/utils/cost-format";
import { CancelConfirmDialog } from "./CancelConfirmDialog";
import { RecordRow } from "./RecordRow";
import { UsageActiveRow } from "./UsageActiveRow";
import { UsageRecordDetailModal } from "./UsageRecordDetailModal";
import {
  formatCount,
  formatRatio,
  projectTitleResolver,
  providerLabelResolver,
  usageProjectLabel,
} from "./usage-record-format";
import {
  sortByStartedDesc,
  taskToUsageRecordView,
  usageRecordToView,
} from "./usage-record-view";
import type { UsageRecordView } from "./usage-record-view";
import { useTaskCancellation } from "./use-task-cancellation";

interface UsagePopoverProps {
  projectName: string;
}

/** 下载失败的调用可以就地重试；其余失败码没有可就地补救的动作。 */
const DOWNLOAD_FAILED_CODE = "download_failed";

function KpiCell({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-3 py-2">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="num text-base text-foreground">{value}</dd>
      <dd className="truncate text-xs text-muted-foreground">{sub}</dd>
    </div>
  );
}

/** KPI 一行 4 格，口径与入口按钮一致：本项目全部时间。 */
function KpiStrip({ summary }: { summary: UsageSummary | null }) {
  const { t, i18n } = useTranslation("dashboard");
  const kpi = summary?.kpi ?? null;
  const primary = summary?.primary_currency ?? null;
  const otherCosts = Object.entries(kpi?.cost ?? {}).filter(
    ([currency, amount]) => currency !== primary && amount > 0,
  );
  return (
    <dl className="grid grid-cols-4 divide-x divide-border rounded-lg border border-border">
      <KpiCell
        label={t("usage_kpi_calls")}
        value={kpi ? formatCount(kpi.calls, i18n.language) : "—"}
        sub={kpi ? t("usage_kpi_project_all") : "—"}
      />
      <KpiCell
        label={t("usage_kpi_success_rate")}
        value={kpi ? formatRatio(kpi.success_rate, i18n.language) : "—"}
        sub={kpi ? t("usage_kpi_success_count", { count: kpi.success }) : "—"}
      />
      <KpiCell
        label={t("usage_kpi_failed")}
        value={kpi ? formatCount(kpi.failed, i18n.language) : "—"}
        sub={
          kpi && kpi.cancelled > 0
            ? t("usage_kpi_cancelled_count", { count: kpi.cancelled })
            : "—"
        }
      />
      <KpiCell
        label={t("usage_kpi_cost")}
        value={
          kpi && primary ? formatCurrencyAmount(primary, kpi.cost[primary] ?? 0) : "—"
        }
        sub={
          [
            ...otherCosts.map(([currency, amount]) => `+ ${formatCurrencyAmount(currency, amount)}`),
            ...(kpi && kpi.unpriced_calls > 0
              ? [t("usage_kpi_unpriced_count", { count: kpi.unpriced_calls })]
              : []),
          ].join("  ") || "—"
        }
      />
    </dl>
  );
}

/**
 * 使用记录面板的内容本体：不含 Popover / Sheet 外壳，桌面端由 {@link UsagePopover} 套
 * PopoverContent、移动端由 {@link UsageSheet} 套 SheetContent，共用同一份计算与排版。
 * 左栏本项目进行中的调用、右栏最近已结束的调用，顶部是与入口按钮同口径的 KPI，
 * 底部通往设置页的全量记录。
 */
export function UsagePanelContent({
  projectName,
  open,
  onClose,
  layout = "popover",
}: UsagePopoverProps & { open: boolean; onClose: () => void; layout?: "popover" | "sheet" }) {
  const { t, i18n } = useTranslation("dashboard");
  const setOpen = onClose;
  const stacked = layout === "sheet";

  const summary = useUsageHeaderStore((s) => s.summary);
  const recent = useUsageHeaderStore((s) => s.recent);
  const pendingRecords = useUsageHeaderStore((s) => s.pending);
  const detailId = useUsageHeaderStore((s) => s.detailId);
  const detail = useUsageHeaderStore((s) => s.detail);
  const detailLoading = useUsageHeaderStore((s) => s.detailLoading);
  const detailFailed = useUsageHeaderStore((s) => s.detailFailed);
  const loadFailed = useUsageHeaderStore((s) => s.loadFailed);
  const openDetail = useUsageHeaderStore((s) => s.openDetail);
  const closeDetail = useUsageHeaderStore((s) => s.closeDetail);
  const refresh = useUsageHeaderStore((s) => s.refresh);

  // 进行中区按占用谓词取：与后端去重口径一致的排队中 / 执行中任务。
  const activeTasks = useTasksStore(
    useShallow((s) =>
      s.tasks.filter(
        (task) => task.project_name === projectName && isOccupyingStatus(task.status),
      ),
    ),
  );
  // 只数列表里显示的排队任务：本地渲染任务不列出，服务端批量取消也不含它们。
  const queuedCount = activeTasks.filter(
    (task) => task.status === "queued" && taskToUsageRecordView(task) !== null,
  ).length;

  const cancellation = useTaskCancellation(
    projectName,
    useCallback(
      () => Promise.all([refresh(), useTasksStore.getState().refreshTasks()]).then(() => undefined),
      [refresh],
    ),
  );
  const [retryingIds, setRetryingIds] = useState<ReadonlySet<string>>(new Set());

  // 悬浮层常驻挂载：收起后不留确认条，下次打开不该看到上一次没做完的取消确认。
  const dismissCancellation = cancellation.dismiss;
  useEffect(() => {
    if (!open) dismissCancellation();
  }, [open, dismissCancellation]);

  const providerLabel = providerLabelResolver(summary);
  const projectLabel = usageProjectLabel(projectName, t, i18n.language, projectTitleResolver(summary)(projectName));

  const activeRows = useMemo(() => {
    const byKey = new Map<string, TaskItem>();
    const views: UsageRecordView[] = [];
    for (const task of activeTasks) {
      const view = taskToUsageRecordView(task);
      if (view === null) continue;
      byKey.set(view.key, task);
      views.push(view);
    }
    for (const record of pendingRecords) views.push(usageRecordToView(record));
    return sortByStartedDesc(views).map((view) => ({
      view,
      task: byKey.get(view.key) ?? null,
    }));
  }, [activeTasks, pendingRecords]);

  const finishedRows = useMemo(() => recent.map(usageRecordToView), [recent]);

  // 「没有任何调用」以 summary 为准：它是本项目全部时间的口径，与入口按钮一致。
  const empty =
    summary !== null &&
    summary.kpi.calls === 0 &&
    activeRows.length === 0 &&
    finishedRows.length === 0;

  // 按 task_id 分别记在途：多条下载失败的行可以同时重试，用单个 id 会让先返回的那条
  // 把后点的那条的加载态一并清掉。
  const markRetrying = useCallback((taskId: string, retrying: boolean) => {
    setRetryingIds((prev) => {
      const next = new Set(prev);
      if (retrying) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }, []);

  const handleRetryDownload = useCallback(
    async (taskId: string) => {
      markRetrying(taskId, true);
      try {
        await API.retryTaskDownload(taskId);
        await Promise.all([useTasksStore.getState().refreshTasks(), refresh()]);
      } catch {
        // 这一行看不出任何变化（仍是失败态、按钮恢复可用），不给回执用户无从判断究竟是
        // 没点上还是被拒了。属「用户在场、可立即重试的同步失败」，按 store 的分工走 toast。
        useAppStore.getState().pushToast(t("retry_download_failed"), "error");
      } finally {
        markRetrying(taskId, false);
      }
    },
    [markRetrying, refresh, t],
  );

  // 跳到设置页是导航，渲染为链接而不是 button；工作台在嵌套路由里，`~` 取绝对路径。
  const viewAllButton = (
    <Link
      href={`~${settingsSectionPath("usage", { u_project: projectName })}`}
      className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "w-full")}
      onClick={() => setOpen()}
    >
      {t("usage_view_all_records")}
    </Link>
  );

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-sm">
          <h3 className="font-medium">{t("usage_records_title")}</h3>
          <p className="min-w-0 text-muted-foreground">
            <TruncatedText text={projectLabel} />
          </p>
        </div>
        <Button variant="ghost" size="icon-xs" aria-label={t("usage_close_panel")} onClick={() => setOpen()}>
          <X aria-hidden="true" />
        </Button>
      </div>

      {loadFailed && (
        <div role="status" className="flex items-center gap-2 text-sm text-destructive">
          <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
          <span className="min-w-0 flex-1">{t("usage_popover_load_failed")}</span>
          <Button variant="link" size="xs" onClick={voidPromise(() => refresh())}>
            {t("usage_refresh")}
          </Button>
        </div>
      )}

      {empty ? (
        <p className="py-3 text-sm text-muted-foreground">{t("usage_popover_empty")}</p>
      ) : (
        <>
          <KpiStrip summary={summary} />
          {/* 桌面两栏各自滚动、高度封顶落在视口内；移动抽屉改成上下堆叠、全宽，
              否则固定宽左栏 + flex-1 右栏在窄屏会把「最近已结束」压成竖排单字。 */}
          <div className={cn("min-h-0", stacked ? "flex flex-col gap-3" : "flex max-h-96")}>
            <section
              aria-label={t("usage_in_progress")}
              className={cn("flex flex-col", stacked ? "w-full" : "w-72 shrink-0 border-r border-border")}
            >
              <div className="flex items-center gap-2 px-2 py-1.5">
                <h4 className="text-xs font-medium text-muted-foreground">{t("usage_in_progress")}</h4>
                <span className="num text-xs text-muted-foreground">{activeRows.length}</span>
                {queuedCount > 0 && (
                  <Button
                    variant="ghost"
                    size="xs"
                    className="ml-auto"
                    onClick={voidPromise(() => cancellation.requestAll(projectName))}
                    aria-label={t("cancel_all_queued_aria")}
                  >
                    {t("cancel_all")}
                  </Button>
                )}
              </div>
              <div className={cn(stacked ? "" : "relative min-h-0 flex-1 overflow-y-auto")}>
                {activeRows.length === 0 ? (
                  <p className="px-2 pb-2 text-xs text-muted-foreground">{t("usage_no_in_progress")}</p>
                ) : (
                  activeRows.map(({ view, task }) => (
                    <UsageActiveRow
                      key={view.key}
                      view={view}
                      task={task}
                      providerLabel={providerLabel}
                      onCancel={voidPromise(cancellation.requestSingle)}
                      cancelling={task !== null && cancellation.cancellingTaskIds.has(task.task_id)}
                    />
                  ))
                )}
              </div>
            </section>

            <section
              aria-label={t("usage_recently_finished")}
              className={cn("flex flex-col", stacked ? "w-full" : "min-w-0 flex-1")}
            >
              <div className="px-2 py-1.5">
                <h4 className="text-xs font-medium text-muted-foreground">{t("usage_recently_finished")}</h4>
              </div>
              <div className={cn(stacked ? "" : "relative min-h-0 flex-1 overflow-y-auto")}>
                {finishedRows.length === 0 ? (
                  <p className="px-2 pb-2 text-xs text-muted-foreground">{t("usage_no_finished")}</p>
                ) : (
                  finishedRows.map((record) => (
                    <RecordRow
                      key={record.key}
                      record={record}
                      layout="compact"
                      providerLabel={providerLabel}
                      onOpenDetail={voidPromise(openDetail)}
                      trailing={
                        record.status === "failed" &&
                        record.errorCode === DOWNLOAD_FAILED_CODE &&
                        record.taskId !== null ? (
                          <RetryDownloadButton
                            taskId={record.taskId}
                            retrying={retryingIds.has(record.taskId)}
                            onRetry={handleRetryDownload}
                          />
                        ) : undefined
                      }
                    />
                  ))
                )}
              </div>
            </section>
          </div>
        </>
      )}

      {cancellation.request && (
        <CancelConfirmDialog
          request={cancellation.request}
          cancelling={cancellation.cancelling}
          failed={cancellation.failed}
          failureDetail={cancellation.failureDetail}
          onConfirm={cancellation.confirm}
          onDismiss={cancellation.dismiss}
        />
      )}

      <div className="border-t border-border pt-1.5">{viewAllButton}</div>

      {detailId !== null && (
        <UsageRecordDetailModal
          recordId={detailId}
          detail={detail}
          loading={detailLoading}
          failed={detailFailed}
          providerLabel={providerLabel}
          onClose={closeDetail}
        />
      )}
    </div>
  );
}

/**
 * 桌面端顶栏用量入口的悬浮层：把 {@link UsagePanelContent} 套进贴着顶栏向下展开的 Popover。
 * 空间不够时不翻到侧面盖住顶栏，而是收矮、两栏内部滚动。
 */
export function UsagePopover({ projectName }: UsagePopoverProps) {
  const open = useAppStore((s) => s.usagePanelOpen);
  const setOpen = useAppStore((s) => s.setUsagePanelOpen);
  return (
    <PopoverContent align="end" sideOffset={6} collisionAvoidance={{ side: "none" }} className="w-160">
      <UsagePanelContent projectName={projectName} open={open} onClose={() => setOpen(false)} />
    </PopoverContent>
  );
}

/**
 * 移动端用量入口：三点菜单里点「使用记录」打开，全屏覆盖当前页面（关闭即回到原页面）。
 * 移动档顶栏放不下常驻的费用按钮与悬浮层，故收进菜单 + 全屏对话框；内容上下堆叠、全宽。
 */
export function UsageDialog({ projectName }: UsagePopoverProps) {
  const open = useAppStore((s) => s.usagePanelOpen);
  const setOpen = useAppStore((s) => s.setUsagePanelOpen);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent size="fullscreen" showCloseButton={false}>
        <DialogBody>
          <UsagePanelContent projectName={projectName} open={open} onClose={() => setOpen(false)} layout="sheet" />
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

function RetryDownloadButton({
  taskId,
  retrying,
  onRetry,
}: {
  taskId: string;
  retrying: boolean;
  onRetry: (taskId: string) => Promise<void>;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <Button variant="link" size="xs" disabled={retrying} onClick={voidPromise(() => onRetry(taskId))}>
      {retrying && <Loader2 aria-hidden="true" data-icon="inline-start" className="animate-spin" />}
      {retrying ? t("retrying_download") : t("retry_download")}
    </Button>
  );
}
