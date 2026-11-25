import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { cn } from "cn";

import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ProviderModelSelect } from "@/components/shared/ProviderModelSelect";
import { ModelLifecycleNotice } from "@/components/shared/ModelLifecycleNotice";
import { useDisplayNames } from "@/hooks/useDisplayNames";
import { useModelCandidates } from "@/hooks/useModelCandidates";
import type { CustomProviderInfo } from "@/types/custom-provider";
import type { ProviderInfo } from "@/types/provider";
import type {
  ModelCandidatesResponse,
  SourcePriorityChannel,
  SourcePriorityChannelView,
  SourcePriorityEntry,
  SourcePriorityMediaType,
  SourcePriorityResponse,
} from "@/types/system";
import { errMsg } from "@/utils/async";
import { getCustomProviderModels, getProviderModels } from "@/utils/provider-models";

const CHANNELS: { media: SourcePriorityMediaType; labelKey: string; hintKey?: string }[] = [
  { media: "video", labelKey: "source_priority_channel_video" },
  { media: "image", labelKey: "source_priority_channel_image", hintKey: "source_priority_image_not_wired" },
  { media: "text", labelKey: "source_priority_channel_text", hintKey: "source_priority_text_not_wired" },
];

/** 编辑态里的一行：`pair` 是 `provider/model`，空串表示尚未选择。 */
interface Row {
  pair: string;
}

type Drafts = Record<SourcePriorityMediaType, { enabled: boolean; rows: Row[] }>;

function splitPair(pair: string): { provider_id: string; model_id: string } | null {
  const idx = pair.indexOf("/");
  if (idx <= 0 || idx === pair.length - 1) return null;
  return { provider_id: pair.slice(0, idx), model_id: pair.slice(idx + 1) };
}

function draftFrom(view: SourcePriorityChannelView): { enabled: boolean; rows: Row[] } {
  return {
    enabled: view.enabled,
    rows: view.entries.map((entry) => ({ pair: `${entry.provider_id}/${entry.model_id}` })),
  };
}

function draftsFrom(response: SourcePriorityResponse): Drafts {
  return {
    video: draftFrom(response.video),
    image: draftFrom(response.image),
    text: draftFrom(response.text),
  };
}

function channelFromDraft(draft: { enabled: boolean; rows: Row[] }): SourcePriorityChannel {
  return {
    enabled: draft.enabled,
    entries: draft.rows
      .map((row) => splitPair(row.pair))
      .filter((entry): entry is { provider_id: string; model_id: string } => entry !== null),
  };
}

/** 只把发生变化的通道写进补丁，未动的通道后端保持原样。 */
function buildPatch(drafts: Drafts, baseline: Drafts): Partial<Record<SourcePriorityMediaType, SourcePriorityChannel>> {
  const patch: Partial<Record<SourcePriorityMediaType, SourcePriorityChannel>> = {};
  for (const { media } of CHANNELS) {
    if (JSON.stringify(drafts[media]) !== JSON.stringify(baseline[media])) {
      patch[media] = channelFromDraft(drafts[media]);
    }
  }
  return patch;
}

/** 一行来源的展示名：`供应商名 · 模型名`，缺译名时退回裸 id。 */
function describeEntry(
  entry: SourcePriorityEntry,
  providerNames: Record<string, string>,
  modelNames: Record<string, string>,
): string {
  const provider = providerNames[entry.provider_id] || entry.provider_id;
  const model = modelNames[`${entry.provider_id}/${entry.model_id}`] || entry.model_id;
  return `${provider} · ${model}`;
}

/** 某通道可选模型：视频取两个任务桶的并集，图片取两个桶并集，文本取默认层。 */
function channelOptions(candidates: ModelCandidatesResponse | null, media: SourcePriorityMediaType): string[] {
  if (!candidates) return [];
  if (media === "video") {
    const { i2v = [], r2v = [] } = candidates.video.buckets;
    return [...new Set([...i2v, ...r2v])];
  }
  if (media === "image") {
    const { t2i = [], i2i = [] } = candidates.image.buckets;
    return [...new Set([...t2i, ...i2i])];
  }
  return [];
}

export interface SourcePrioritySectionProps {
  /** 全局（`/system/source-priority`）或项目（`/projects/{name}/source-priority`）。 */
  scope: "global" | "project";
  /** 项目作用域时必填。 */
  projectName?: string;
}

/**
 * 多源优先级编辑器：三条通道各自一份开关 + 有序来源列表，可增删行、上下移动、点选当前行。
 * 保存走独立的 PATCH，与「默认模型」的编辑单元互不影响。
 *
 * 运行态（当前第几行）不在本地暂存：手动切行是即时动作，单独调 select 接口后整体重取。
 */
export function SourcePrioritySection({ scope, projectName }: SourcePrioritySectionProps) {
  const { t } = useTranslation("dashboard");
  const [response, setResponse] = useState<SourcePriorityResponse | null>(null);
  const [drafts, setDrafts] = useState<Drafts | null>(null);
  const [baseline, setBaseline] = useState<Drafts | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [customProviders, setCustomProviders] = useState<CustomProviderInfo[]>([]);
  const { candidates, reload: reloadCandidates } = useModelCandidates();
  const { providerNames, modelNames } = useDisplayNames(providers, customProviders, null, candidates);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res =
        scope === "global"
          ? await API.getGlobalSourcePriority()
          : await API.getProjectSourcePriority(projectName ?? "");
      setResponse(res);
      const next = draftsFrom(res);
      setDrafts(next);
      setBaseline(next);
    } catch (error) {
      setLoadError(errMsg(error));
    }
  }, [scope, projectName]);

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

  const dirty = drafts !== null && baseline !== null && JSON.stringify(drafts) !== JSON.stringify(baseline);

  const mutate = (media: SourcePriorityMediaType, fn: (draft: Drafts[SourcePriorityMediaType]) => Drafts[SourcePriorityMediaType]) => {
    setDrafts((prev) => (prev ? { ...prev, [media]: fn(prev[media]) } : prev));
  };

  const save = async () => {
    if (!drafts || !baseline) return;
    setSaving(true);
    setSaveError(null);
    try {
      const patch = buildPatch(drafts, baseline);
      const res =
        scope === "global"
          ? await API.updateGlobalSourcePriority(patch)
          : await API.updateProjectSourcePriority(projectName ?? "", patch);
      setResponse(res);
      const next = draftsFrom(res);
      setDrafts(next);
      setBaseline(next);
    } catch (error) {
      setSaveError(t("source_priority_save_failed", { message: errMsg(error) }));
    } finally {
      setSaving(false);
    }
  };

  const selectRow = async (media: SourcePriorityMediaType, index: number) => {
    try {
      const res =
        scope === "global"
          ? await API.selectGlobalSourcePriority(media, index)
          : await API.selectProjectSourcePriority(projectName ?? "", media, index);
      setResponse(res);
      // 切行只改运行态，不动编辑中的列表：保留用户未保存的改动。
    } catch (error) {
      setSaveError(errMsg(error));
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

  if (!drafts || !response) {
    return <p className="text-sm text-muted-foreground">{t("common:loading", { defaultValue: "Loading…" })}</p>;
  }

  const lifecycle = candidates?.lifecycle;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">{t("source_priority_title")}</h2>
        <p className="text-sm text-muted-foreground">{t("source_priority_desc")}</p>
      </div>

      {CHANNELS.map(({ media, labelKey, hintKey }) => {
        const draft = drafts[media];
        const view = response[media];
        const options = channelOptions(candidates, media);
        const showFallbackHint = scope === "project" && !view.active && response.scope !== "project";
        const activeEntry = view.active ? view.entries[view.active_index] : undefined;
        return (
          <section key={media} className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 flex-col gap-1">
                <h3 className="text-sm font-medium">{t(labelKey)}</h3>
                <p className="text-xs text-muted-foreground">
                  {activeEntry
                    ? t("source_priority_current_source", {
                        source: describeEntry(activeEntry, providerNames, modelNames),
                      })
                    : view.entries.length > 0
                      ? t("source_priority_not_enabled")
                      : t("source_priority_empty")}
                </p>
                {hintKey ? <p className="text-xs text-muted-foreground">{t(hintKey)}</p> : null}
                {showFallbackHint ? (
                  <p className="text-xs text-muted-foreground">{t("source_priority_falls_back_global")}</p>
                ) : null}
              </div>
              <label className="flex shrink-0 items-center gap-2 text-sm">
                <Switch
                  checked={draft.enabled}
                  onCheckedChange={(checked) => mutate(media, (d) => ({ ...d, enabled: checked }))}
                />
                <span>{t("source_priority_enable")}</span>
              </label>
            </div>

            <div className="flex flex-col gap-2">
              {draft.rows.map((row, index) => {
                const isActive = view.active && view.active_index === index;
                return (
                  <div key={index} className="flex flex-col gap-1">
                    <div className="flex items-center gap-2">
                      <span className="w-5 shrink-0 text-center text-xs text-muted-foreground">{index + 1}</span>
                      <div className="min-w-0 flex-1">
                        <ProviderModelSelect
                          value={row.pair}
                          options={options}
                          providerNames={providerNames}
                          modelNames={modelNames}
                          onChange={(value) =>
                            mutate(media, (d) => ({
                              ...d,
                              rows: d.rows.map((r, i) => (i === index ? { pair: value } : r)),
                            }))
                          }
                          placeholder={t("source_priority_select_model")}
                          aria-label={`${t(labelKey)} ${index + 1}`}
                        />
                      </div>
                      <button
                        type="button"
                        aria-pressed={isActive}
                        disabled={!draft.enabled || !splitPair(row.pair) || dirty}
                        onClick={() => void selectRow(media, index)}
                        className={cn(
                          "inline-flex h-8 shrink-0 items-center justify-center rounded-lg border px-2.5 text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50",
                          isActive
                            ? "border-good/40 bg-good/12 text-good"
                            : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground",
                        )}
                      >
                        {isActive ? t("source_priority_row_active") : t("source_priority_row_pending")}
                      </button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t("source_priority_move_up")}
                        disabled={index === 0}
                        onClick={() =>
                          mutate(media, (d) => {
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
                        aria-label={t("source_priority_move_down")}
                        disabled={index === draft.rows.length - 1}
                        onClick={() =>
                          mutate(media, (d) => {
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
                        aria-label={t("source_priority_remove_row")}
                        onClick={() => mutate(media, (d) => ({ ...d, rows: d.rows.filter((_, i) => i !== index) }))}
                      >
                        <Trash2 aria-hidden />
                      </Button>
                    </div>
                    <ModelLifecycleNotice
                      value={row.pair}
                      notices={lifecycle}
                      modelNames={modelNames}
                      onSwitch={(target) =>
                        mutate(media, (d) => ({
                          ...d,
                          rows: d.rows.map((r, i) => (i === index ? { pair: target } : r)),
                        }))
                      }
                    />
                  </div>
                );
              })}
              <div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => mutate(media, (d) => ({ ...d, rows: [...d.rows, { pair: "" }] }))}
                >
                  <Plus aria-hidden data-icon="inline-start" />
                  {t("source_priority_add_row")}
                </Button>
              </div>
            </div>
          </section>
        );
      })}

      <p className="text-xs text-muted-foreground">{t("source_priority_quota_note")}</p>

      {saveError ? <p className="text-sm text-destructive">{saveError}</p> : null}
      <div className="flex items-center justify-end gap-2">
        <Button
          variant="outline"
          disabled={!dirty || saving}
          onClick={() => {
            setDrafts(baseline);
            setSaveError(null);
          }}
        >
          {t("common:discard_changes", { defaultValue: "Discard" })}
        </Button>
        <Button disabled={!dirty || saving} onClick={() => void save()}>
          {t("source_priority_save")}
        </Button>
      </div>
    </div>
  );
}
