import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";

import { API } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDisplayNames } from "@/hooks/useDisplayNames";
import type { CustomProviderInfo } from "@/types/custom-provider";
import type { ProviderInfo } from "@/types/provider";
import type { SourcePoolTask, VideoSourcePool } from "@/types/system";
import { errMsg } from "@/utils/async";
import { getCustomProviderModels, getProviderModels } from "@/utils/provider-models";

const PAGE_SIZE = 50;

/** 任务状态 → 徽章样式；未知状态回退中性。 */
function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  switch (status) {
    case "succeeded":
      return "default";
    case "failed":
      return "destructive";
    case "running":
      return "secondary";
    default:
      return "outline";
  }
}

function formatDuration(startedAt: string | null, finishedAt: string | null): string | null {
  if (!startedAt || !finishedAt) return null;
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

/**
 * 全局设置「汇总源监控」：列出经汇总源分配的任务，回答「哪个任务分到了哪个池、落到哪个真实源、
 * 调了几次、成没成、耗时多少」。数据来自任务行的 ``source_pool_id`` 与按 ``task_id`` 聚合的调用记录。
 */
export function SourcePoolMonitorSection() {
  const { t } = useTranslation("dashboard");
  const [items, setItems] = useState<SourcePoolTask[] | null>(null);
  const [pools, setPools] = useState<VideoSourcePool[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [poolFilter, setPoolFilter] = useState<number | "">("");
  const [statusFilter, setStatusFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [customProviders, setCustomProviders] = useState<CustomProviderInfo[]>([]);
  const { providerNames, modelNames } = useDisplayNames(providers, customProviders, null, null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await API.listSourcePoolTasks({
        poolId: poolFilter === "" ? undefined : poolFilter,
        status: statusFilter || undefined,
        page,
        pageSize: PAGE_SIZE,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setLoading(false);
    }
  }, [poolFilter, statusFilter, page]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载与筛选变化时异步拉取后回写
    void load();
  }, [load]);

  useEffect(() => {
    void API.listSourcePools()
      .then(setPools)
      .catch(() => setPools([]));
    void Promise.all([
      getProviderModels().catch(() => [] as ProviderInfo[]),
      getCustomProviderModels().catch(() => [] as CustomProviderInfo[]),
    ]).then(([nextProviders, nextCustom]) => {
      setProviders(nextProviders);
      setCustomProviders(nextCustom);
    });
  }, []);

  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const hasPrev = page > 1;
  const hasNext = to < total;

  const providerLabel = (providerId: string | null) =>
    providerId ? providerNames[providerId] || providerId : "—";
  const modelLabel = (providerId: string | null, modelId: string | null) =>
    providerId && modelId ? modelNames[`${providerId}/${modelId}`] || modelId : "—";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">{t("source_pool_monitor_title")}</h2>
        <p className="text-sm text-muted-foreground">{t("source_pool_monitor_desc")}</p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="h-9 rounded-md border border-border bg-background px-2 text-sm"
          value={poolFilter}
          onChange={(e) => {
            setPage(1);
            setPoolFilter(e.target.value === "" ? "" : Number(e.target.value));
          }}
          aria-label={t("source_pool_monitor_filter_pool")}
        >
          <option value="">{t("source_pool_monitor_filter_all_pools")}</option>
          {pools.map((pool) => (
            <option key={pool.id} value={pool.id}>
              {pool.name}
            </option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-2 text-sm"
          value={statusFilter}
          onChange={(e) => {
            setPage(1);
            setStatusFilter(e.target.value);
          }}
          aria-label={t("source_pool_monitor_filter_status")}
        >
          <option value="">{t("source_pool_monitor_filter_all_status")}</option>
          <option value="queued">{t("source_pool_monitor_status_queued")}</option>
          <option value="running">{t("source_pool_monitor_status_running")}</option>
          <option value="succeeded">{t("source_pool_monitor_status_succeeded")}</option>
          <option value="failed">{t("source_pool_monitor_status_failed")}</option>
          <option value="cancelled">{t("source_pool_monitor_status_cancelled")}</option>
        </select>
        <Button variant="ghost" size="icon-sm" aria-label={t("source_pool_monitor_refresh")} onClick={() => void load()}>
          <RefreshCw aria-hidden />
        </Button>
      </div>

      {error ? <p className="text-sm text-destructive">{error}</p> : null}

      {items === null ? (
        <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          {t("common:loading", { defaultValue: "Loading…" })}
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("source_pool_monitor_empty")}</p>
      ) : (
        <div className="relative overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("source_pool_monitor_col_task")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_pool")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_member")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_status")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_calls")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_duration")}</TableHead>
                <TableHead>{t("source_pool_monitor_col_error")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item) => {
                const duration = formatDuration(item.started_at, item.finished_at);
                return (
                  <TableRow key={item.task_id}>
                    <TableCell>
                      <div className="flex flex-col">
                        <span className="text-sm">{item.project_name}</span>
                        <span className="text-xs text-muted-foreground">
                          {item.task_type} · {item.resource_id}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm">{item.pool_name || `pool-${item.source_pool_id}`}</span>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col">
                        <span className="text-sm">{providerLabel(item.provider_id)}</span>
                        <span className="text-xs text-muted-foreground">
                          {modelLabel(item.provider_id, item.model_id)}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(item.status)}>{item.status}</Badge>
                    </TableCell>
                    <TableCell>
                      <span className="num text-sm">
                        <span className={item.failed_count > 0 ? "text-destructive" : undefined}>
                          {item.success_count}/{item.call_count}
                        </span>
                        {item.failed_count > 0 ? (
                          <span className="ml-1 text-xs text-destructive">
                            {t("source_pool_monitor_failed_suffix", { count: item.failed_count })}
                          </span>
                        ) : null}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className="num text-sm">{duration ?? "—"}</span>
                    </TableCell>
                    <TableCell>
                      <span
                        className="block max-w-64 truncate text-xs text-destructive"
                        title={item.error_message ?? undefined}
                      >
                        {item.error_message ?? "—"}
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <footer className="flex items-center justify-between border-t border-border px-1 py-2">
        <span className="num text-xs text-muted-foreground">
          {t("source_pool_monitor_page_position", { from, to, total })}
        </span>
        <div className="flex items-center gap-1">
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t("usage_prev_page")}
            disabled={!hasPrev || loading}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            <ChevronLeft aria-hidden />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={t("usage_next_page")}
            disabled={!hasNext || loading}
            onClick={() => setPage((p) => p + 1)}
          >
            <ChevronRight aria-hidden />
          </Button>
        </div>
      </footer>
    </div>
  );
}
