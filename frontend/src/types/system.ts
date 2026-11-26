import type { CapabilityOverrides } from "./custom-provider";
import type { ProviderModelCapabilities } from "./provider";

export interface SystemConfigSettings {
  default_video_backend: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple: string;
  text_backend_complex: string;
  video_generate_audio: boolean;
  video_poll_timeout_seconds: number;
  anthropic_api_key: { is_set: boolean; masked: string | null };
  anthropic_base_url: string;
  anthropic_model: string;
  anthropic_default_haiku_model: string;
  anthropic_default_opus_model: string;
  anthropic_default_sonnet_model: string;
  claude_code_subagent_model: string;
  agent_session_cleanup_delay_seconds: number;
  agent_max_concurrent_sessions: number;
  /** 市场源 GitHub raw 代理前缀；空串即直连。 */
  market_github_proxy_prefix?: string;
}

export interface SystemConfigOptions {
  video_backends: string[];
  image_backends: string[];
  text_backends: string[];
  audio_backends?: string[];
  provider_names?: Record<string, string>;
  /** "provider_id/model_id" → 按 Accept-Language 成文的模型名；缺键即退回 model id。 */
  model_names?: Record<string, string>;
}

/** 新建 TTS 项目的预填值；未配置任何音频供应商时 audio_backend 为 null。 */
export interface NarrationDefaultsResponse {
  audio_backend: string | null;
  narration_voice: string;
  narration_speed: number | null;
}

/** TTS 音色目录条目：id 是合成请求的 voice 取值，label 是按界面语言渲染的展示名。 */
export interface TtsVoiceOption {
  id: string;
  label: string;
}

export interface TtsModelCapabilitiesResponse {
  supports_speed: boolean;
  /** 该模型所用调用端点的音色表；同一供应商的不同端点音色不同。 */
  voices: TtsVoiceOption[];
}

export interface GetSystemConfigResponse {
  settings: SystemConfigSettings;
  options: SystemConfigOptions;
}

/** 任务类型桶键（docs/adr/0054）。代码内部术语，界面文案不直接呈现。 */
export type GenerationTypeBucket = "t2i" | "i2i" | "i2v" | "r2v";

/**
 * 各用途在项目与全局各层都留空时后端自动选中的模型（provider/model），与执行期解析同口径；
 * 没有可用供应商时为 null。文本各档位的自动推断相同，合为一项。
 */
export type AutoBackendsResponse = Record<GenerationTypeBucket | "text", string | null>;

/**
 * 一处推荐变化：内置供应商在这个媒体类型下现在推荐 `recommended`（provider/model）。
 * `current` 是这个媒体类型的全局默认模型，没有设置时为 null。只提示，改不改由用户决定。
 */
export interface RecommendationChange {
  media_type: "text" | "image" | "video" | "audio";
  provider_id: string;
  recommended: string;
  current: string | null;
}

export interface RecommendationChangesResponse {
  changes: RecommendationChange[];
}

/** 任务类型桶滤掉模型时不满足的能力维度，键与能力接口同名。 */
export type BucketDimension = "text_to_image" | "image_to_image" | "max_reference_images" | "first_frame";

/** 默认层里有、某个任务类型桶因能力不满足而没有列出的模型。 */
export interface FilteredCandidate {
  option: string;
  /** 不满足的能力维度；为空表示判定不出（调用端点已下线、调用通道没有声明能力）。 */
  unmet: BucketDimension[];
}

/** 单一 media_type 的候选：默认层全量 + 各任务类型桶按能力过滤后的子集，以及各桶滤掉的模型与原因。 */
export interface MediaCandidates {
  default: string[];
  buckets: Partial<Record<GenerationTypeBucket, string[]>>;
  /** 停用、已下线或低于最低应用版本的模型不在默认层，也不在这里。 */
  filtered: Partial<Record<GenerationTypeBucket, FilteredCandidate[]>>;
}

/** 引用位置：`default` 为默认层（文本档位与配音模型也取它），其余为该媒体类型的任务类型桶。 */
export type ModelReferenceLayer = "default" | GenerationTypeBucket;

/**
 * 引用处的下线提示：即将下线或已下线的供应商模型，连同官方后继与各引用位置的一键切换目标。
 * `unavailable`：引用的模型不在供应商清单里，本地也没有它已下线的条目，只提示重新选择。
 */
export interface ModelLifecycleNotice {
  status: "retiring" | "discontinued" | "unavailable";
  /** ISO 日期（YYYY-MM-DD）；已下线但没有登记日期时为 null。 */
  shutdown_date: string | null;
  /** 官方后继的「模型厂商/官方模型名」与条目名；没有登记后继时为 null。 */
  successor: string | null;
  successor_name: string | null;
  /**
   * 引用位置 → 一键切换的目标（provider/model）：同一供应商下对应到后继、且是该位置候选的供应商模型。
   * 没有这样的模型时为 null，只展示后继名称。键只含该媒体类型存在的位置。
   */
  switch_targets: Partial<Record<ModelReferenceLayer, string | null>>;
}

export interface ModelCandidatesResponse {
  image: MediaCandidates;
  video: MediaCandidates;
  /** 供应商 id → 按 Accept-Language 成文的名字；内置与自定义同一口径。 */
  provider_names: Record<string, string>;
  /** "provider_id/model_id" → 按 Accept-Language 成文的模型名；覆盖全部媒体类型的候选。 */
  model_names: Record<string, string>;
  /**
   * "provider_id/model_id" → 下线提示；收即将下线与已下线的模型（含不在候选里的），以及设置与项目里引用的
   * 清单之外的模型，覆盖全部媒体类型。
   */
  lifecycle: Record<string, ModelLifecycleNotice>;
}

export interface SystemVersionReleaseInfo {
  version: string;
  tag_name: string;
  name: string;
  body: string;
  html_url: string;
  published_at: string;
}

export interface GetSystemVersionResponse {
  current: { version: string };
  latest: SystemVersionReleaseInfo | null;
  has_update: boolean;
  checked_at: string;
  update_check_error: string | null;
}

/** 首次使用引导的「已看过」状态 —— 实例级，未设置视为未看过。 */
export interface OnboardingStatus {
  seen: boolean;
}

export interface SystemConfigPatch {
  default_video_backend?: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend?: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend?: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple?: string;
  text_backend_complex?: string;
  video_generate_audio?: boolean;
  video_poll_timeout_seconds?: number;
  anthropic_api_key?: string;
  anthropic_base_url?: string;
  anthropic_model?: string;
  anthropic_default_haiku_model?: string;
  anthropic_default_opus_model?: string;
  anthropic_default_sonnet_model?: string;
  claude_code_subagent_model?: string;
  agent_session_cleanup_delay_seconds?: number;
  agent_max_concurrent_sessions?: number;
  market_github_proxy_prefix?: string;
}

// ==================== 多源优先级 ====================

/** 一条通道里的一行：一对 `(provider_id, model_id)`。 */
export interface SourcePriorityEntry {
  provider_id: string;
  model_id: string;
}

/** 一条通道的配置：开关 + 有序列表。 */
export interface SourcePriorityChannel {
  enabled: boolean;
  entries: SourcePriorityEntry[];
}

/** 一条通道的读视图：配置 + 当前生效行（已夹取到合法范围）+ 该层是否生效。 */
export interface SourcePriorityChannelView {
  enabled: boolean;
  entries: SourcePriorityEntry[];
  active_index: number;
  /** 开关打开且有至少一行时为真，即当前由本层配置驱动生成。 */
  active: boolean;
  /** 这份数据来自哪一层。项目层未配置该通道而回落全局时为 `global`。 */
  source: "project" | "global";
}

/** 三类通道的配置与运行态。`scope` 标明这份数据来自哪一层；项目层部分通道回落全局时为 `mixed`。 */
export interface SourcePriorityResponse {
  scope: "global" | "project" | "mixed";
  video: SourcePriorityChannelView;
  image: SourcePriorityChannelView;
  text: SourcePriorityChannelView;
}

/** 配置补丁：只更新出现的通道，未出现的保持原样。 */
export interface SourcePriorityPatch {
  video?: SourcePriorityChannel;
  image?: SourcePriorityChannel;
  text?: SourcePriorityChannel;
}

export type SourcePriorityMediaType = "video" | "image" | "text";

// ==================== 视频汇总源 ====================

/** 汇总源成员：指向一个已配置的视频模型。 */
export interface VideoSourcePoolMember {
  position: number;
  provider_id: string;
  model_id: string;
}

/** 一个汇总源：对外是虚拟 provider `pool-<id>`，模型名用户自定。 */
export interface VideoSourcePool {
  id: number;
  /** 虚拟 provider id，形如 `pool-1`；可直接用于默认模型与多源优先级的 `provider_id`。 */
  provider_id: string;
  name: string;
  /** 统一模型名：候选下拉与默认模型页展示用；空则回退 name。虚拟 model_id 固定 aggregate。 */
  model_name: string | null;
  /** 能力参考成员：池的能力取此成员回填；空则取 position=0 的首个成员。 */
  capability_provider_id: string | null;
  capability_model_id: string | null;
  /** 池自己的能力覆盖（稀疏）：参考成员只提供原值，用户可在其上整维覆盖。 */
  capability_overrides: CapabilityOverrides | null;
  enabled: boolean;
  members: VideoSourcePoolMember[];
}

/** 新建/更新汇总源的请求体。成员顺序即分配时的 position 顺序。 */
export interface VideoSourcePoolInput {
  name: string;
  model_name: string | null;
  capability_provider_id: string | null;
  capability_model_id: string | null;
  /** 池自己的能力覆盖（稀疏）：参考成员只提供原值，用户可在其上整维覆盖。 */
  capability_overrides?: CapabilityOverrides | null;
  enabled: boolean;
  members: { provider_id: string; model_id: string }[];
}

/** 汇总源监控里的一条任务：池 → 真实成员 → 调用聚合。 */
export interface SourcePoolTask {
  task_id: string;
  project_name: string;
  task_type: string;
  resource_id: string;
  source_pool_id: number;
  pool_name: string | null;
  /** 实际执行的供应商（池展开后的真实成员）。 */
  provider_id: string | null;
  model_id: string | null;
  status: string;
  error_message: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  call_count: number;
  success_count: number;
  failed_count: number;
  last_call_finished_at: string | null;
}

export interface SourcePoolTaskPage {
  items: SourcePoolTask[];
  total: number;
  page: number;
  page_size: number;
}

/** 汇总源自己的能力视图（GET /system/source-pools/{id}/capabilities）：形状对齐自定义供应商模型的能力视图。 */
export type SourcePoolCapabilities = ProviderModelCapabilities;
