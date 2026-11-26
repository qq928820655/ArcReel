import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Loader2, Plus, Trash2 } from "lucide-react";

import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ProviderModelSelect } from "@/components/shared/ProviderModelSelect";
import { useDisplayNames } from "@/hooks/useDisplayNames";
import { useModelCandidates } from "@/hooks/useModelCandidates";
import type { CustomProviderInfo } from "@/types/custom-provider";
import type { ProviderInfo } from "@/types/provider";
import type { ModelCandidatesResponse, VideoSourcePool } from "@/types/system";
import { errMsg } from "@/utils/async";
import { getCustomProviderModels, getProviderModels } from "@/utils/provider-models";

/** 编辑态里的一行成员：`pair` 是 `provider/model`，空串表示尚未选择。 */
interface MemberRow {
  pair: string;
}

/** 编辑态里的一个汇总源；`id` 为 null 表示尚未保存的新建。 */
interface PoolDraft {
  key: string;
  id: number | null;
  name: string;
  enabled: boolean;
  rows: MemberRow[];
}

function draftFromPool(pool: VideoSourcePool): PoolDraft {
  return {
    key: `pool-${pool.id}`,
    id: pool.id,
    name: pool.name,
    enabled: pool.enabled,
    rows: pool.members.map((m) => ({ pair: `${m.provider_id}/${m.model_id}` })),
  };
}

function newPoolDraft(): PoolDraft {
  return { key: `new-${Date.now()}`, id: null, name: "", enabled: true, rows: [{ pair: "" }] };
}

/** 汇总源成员只能选真实视频模型，不能把另一个汇总源当成员（避免池套池）。 */
function memberOptions(candidates: ModelCandidatesResponse | null): string[] {
  if (!candidates) return [];
  const { i2v = [], r2v = [] } = candidates.video.buckets;
  return [...new Set([...i2v, ...r2v])].filter((pair) => !pair.startsWith("pool-"));
}

function splitPair(pair: string): { provider_id: string; model_id: string } | null {
  const idx = pair.indexOf("/");
  if (idx <= 0 || idx === pair.length - 1) return null;
  return { provider_id: pair.slice(0, idx), model_id: pair.slice(idx + 1) };
}

/**
 * 全局设置「汇总源」：把多家已配置供应商的视频模型汇成一个可命名的池，对外是虚拟来源
 * `pool-<id>`，可像普通模型一样被默认模型与多源优先级引用。成员按顺序分配，最空的那个先跑
 * （见 ADR 0123）。
 */
export function SourcePoolSection() {
  const { t } = useTranslation("dashboard");
  const [pools, setPools] = useState<PoolDraft[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [customProviders, setCustomProviders] = useState<CustomProviderInfo[]>([]);
  const { candidates, reload: reloadCandidates } = useModelCandidates();
  const { providerNames, modelNames } = useDisplayNames(providers, customProviders, null, candidates);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res = await API.listSourcePools();
      setPools(res.map(draftFromPool));
    } catch (error) {
      setLoadError(errMsg(error));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时异步拉取配置后回写
    void load();
  }, [load]);

  useEffect(() => {
    void reloadCandidates();
  }, [reloadCandidates]);

  useEffect(() => {
    void Promise.all([
      getProviderModels().catch(() => [] as ProviderInfo[]),
      getCustomProviderModels().catch(() => [] as CustomProviderInfo[]),
    ]).then(([nextProviders, nextCustom]) => {
      setProviders(nextProviders);
      setCustomProviders(nextCustom);
    });
  }, []);

  const options = memberOptions(candidates);

  const mutate = (key: string, fn: (draft: PoolDraft) => PoolDraft) => {
    setPools((prev) => (prev ? prev.map((p) => (p.key === key ? fn(p) : p)) : prev));
  };

  const savePool = async (draft: PoolDraft) => {
    const members = draft.rows
      .map((row) => splitPair(row.pair))
      .filter((entry): entry is { provider_id: string; model_id: string } => entry !== null);
    if (!draft.name.trim()) {
      setActionError(t("source_pool_name_required"));
      return;
    }
    if (members.length === 0) {
      setActionError(t("source_pool_members_required"));
      return;
    }
    setBusyKey(draft.key);
    setActionError(null);
    try {
      const input = { name: draft.name.trim(), enabled: draft.enabled, members };
      const saved =
        draft.id === null ? await API.createSourcePool(input) : await API.updateSourcePool(draft.id, input);
      setPools((prev) =>
        prev
          ? prev.map((p) => (p.key === draft.key ? draftFromPool(saved) : p))
          : prev,
      );
      // 池会作为视频候选出现，保存后重取候选让默认模型/多源优先级下拉立即看到。
      void reloadCandidates();
    } catch (error) {
      setActionError(errMsg(error));
    } finally {
      setBusyKey(null);
    }
  };

  const deletePool = async (draft: PoolDraft) => {
    if (draft.id === null) {
      setPools((prev) => (prev ? prev.filter((p) => p.key !== draft.key) : prev));
      return;
    }
    setBusyKey(draft.key);
    setActionError(null);
    try {
      await API.deleteSourcePool(draft.id);
      setPools((prev) => (prev ? prev.filter((p) => p.key !== draft.key) : prev));
      void reloadCandidates();
    } catch (error) {
      setActionError(errMsg(error));
    } finally {
      setBusyKey(null);
    }
  };

  if (loadError) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-destructive">{loadError}</p>
        <Button variant="outline" onClick={() => void load()}>
          {t("common:retry", { defaultValue: "Retry" })}
        </Button>
      </div>
    );
  }

  if (!pools) {
    return (
      <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {t("common:loading", { defaultValue: "Loading…" })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">{t("source_pool_title")}</h2>
        <p className="text-sm text-muted-foreground">{t("source_pool_desc")}</p>
      </div>

      {actionError ? <p className="text-sm text-destructive">{actionError}</p> : null}

      {pools.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("source_pool_empty")}</p>
      ) : null}

      {pools.map((draft) => {
        const busy = busyKey === draft.key;
        return (
          <section key={draft.key} className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
            <div className="flex items-end gap-3">
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <Label htmlFor={`pool-name-${draft.key}`}>{t("source_pool_name_label")}</Label>
                <Input
                  id={`pool-name-${draft.key}`}
                  value={draft.name}
                  onChange={(e) => mutate(draft.key, (d) => ({ ...d, name: e.target.value }))}
                  placeholder={t("source_pool_name_placeholder")}
                  maxLength={128}
                />
              </div>
              <label className="flex shrink-0 items-center gap-2 pb-2 text-sm">
                <Switch
                  checked={draft.enabled}
                  onCheckedChange={(checked) => mutate(draft.key, (d) => ({ ...d, enabled: checked }))}
                />
                <span>{t("source_pool_enabled")}</span>
              </label>
            </div>

            <div className="flex flex-col gap-2">
              <div className="text-xs font-medium text-muted-foreground">{t("source_pool_members_label")}</div>
              {draft.rows.map((row, index) => (
                // 窄屏下三个图标按钮各有 44px 触控区，若与下拉挤一行会把下拉压到很窄。
                // flex-wrap + 下拉最小基准宽：放不下时按钮整组换到下一行右对齐，下拉独占整行；
                // 宽屏仍排一行。
                <div key={index} className="flex flex-wrap items-center gap-2">
                  <span className="w-5 shrink-0 text-center text-xs text-muted-foreground">{index + 1}</span>
                  <div className="min-w-0 grow basis-44">
                    <ProviderModelSelect
                      value={row.pair}
                      options={options}
                      providerNames={providerNames}
                      modelNames={modelNames}
                      onChange={(value) =>
                        mutate(draft.key, (d) => ({
                          ...d,
                          rows: d.rows.map((r, i) => (i === index ? { pair: value } : r)),
                        }))
                      }
                      placeholder={t("source_pool_select_model")}
                      aria-label={`${t("source_pool_members_label")} ${index + 1}`}
                    />
                  </div>
                  <div className="ml-auto flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t("source_pool_move_up")}
                      disabled={index === 0}
                      onClick={() =>
                        mutate(draft.key, (d) => {
                          const rows = [...d.rows];
                          [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]];
                          return { ...d, rows };
                        })
                      }
                    >
                      <ArrowUp aria-hidden />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t("source_pool_move_down")}
                      disabled={index === draft.rows.length - 1}
                      onClick={() =>
                        mutate(draft.key, (d) => {
                          const rows = [...d.rows];
                          [rows[index + 1], rows[index]] = [rows[index], rows[index + 1]];
                          return { ...d, rows };
                        })
                      }
                    >
                      <ArrowDown aria-hidden />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t("source_pool_remove_member")}
                      disabled={draft.rows.length === 1}
                      onClick={() =>
                        mutate(draft.key, (d) => ({ ...d, rows: d.rows.filter((_, i) => i !== index) }))
                      }
                    >
                      <Trash2 aria-hidden />
                    </Button>
                  </div>
                </div>
              ))}
              <div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => mutate(draft.key, (d) => ({ ...d, rows: [...d.rows, { pair: "" }] }))}
                >
                  <Plus aria-hidden data-icon="inline-start" />
                  {t("source_pool_add_member")}
                </Button>
              </div>
            </div>

            <div className="flex items-center justify-end gap-2">
              <Button variant="ghost" disabled={busy} onClick={() => void deletePool(draft)}>
                {t("source_pool_delete")}
              </Button>
              <Button disabled={busy} onClick={() => void savePool(draft)}>
                {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                {t("source_pool_save")}
              </Button>
            </div>
          </section>
        );
      })}

      <div>
        <Button
          variant="outline"
          onClick={() => setPools((prev) => (prev ? [...prev, newPoolDraft()] : prev))}
        >
          <Plus aria-hidden data-icon="inline-start" />
          {t("source_pool_add")}
        </Button>
      </div>
    </div>
  );
}
