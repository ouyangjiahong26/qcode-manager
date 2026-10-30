/**
 * 上游 /v1/models 目录相关的类型。
 *
 * 字段以 server/routers/models.py 的实际返回为准：`{upstream_id, models}`，
 * models 即上游 OpenAI 兼容清单原样透传——除 `id` 外的字段都可能缺，
 * 因此全部标成可选，界面按「有就显示、没有给占位」处理。
 */

/** 上游清单里的单个模型条目（OpenAI /v1/models 形状） */
export interface UpstreamModel {
  id: string;
  object?: string;
  owned_by?: string;
  created?: number;
}

/** GET /api/models、GET /api/playground/models 的返回体 */
export interface ModelCatalog {
  /** 拉取用的上游 id；null = 默认上游 */
  upstream_id: number | null;
  models: UpstreamModel[];
}
