/**
 * schema.ts — 認知思考計畫與評估 Schema 定義
 * 
 * 定義 CognitivePlan、分群維度、排序標準與 Sub-Agent 評估結果。
 * 遵循台灣繁體中文專業術語規範與嚴格型別安全。
 */

/** 排序方向型態 */
export type SortDirection = 'asc' | 'desc';

/** 空值 (Null/Undefined) 排序防護策略 */
export type NullHandlingStrategy = 'nulls_first' | 'nulls_last';

/** 降級處理策略 */
export type FallbackStrategy = 'stable_keep' | 'move_to_end' | 'move_to_front';

/**
 * 高階實體分群維度定義
 */
export interface GroupDimension {
  /** 屬性欄位名稱（如 "album", "category", "channel_title"） */
  readonly field: string;
  /** 分群權重（數值越大代表越高優先級之高階分群） */
  readonly weight: number;
  /** 空值防護策略（預設 'nulls_last'） */
  readonly nullHandling: NullHandlingStrategy;
  /** 別名/規範化對照表（選填，用於同義詞歸類） */
  readonly aliasMap?: Readonly<Record<string, string>>;
}

/**
 * 群內純量排序標準定義
 */
export interface SortCriterion {
  /** 屬性欄位名稱（如 "track_number", "published_at", "view_count"） */
  readonly field: string;
  /** 排序方向（'asc' 升冪 / 'desc' 降冪） */
  readonly direction: SortDirection;
  /** 空值防護策略（預設 'nulls_last'） */
  readonly nullHandling: NullHandlingStrategy;
}

/**
 * 認知排序思考計畫 (CognitivePlan)
 * 為語意分析與階層思考樹輸出之結構化執行計畫
 */
export interface CognitivePlan {
  /** 高階實體分群維度陣列（依權重排序） */
  readonly groupDimensions: readonly GroupDimension[];
  /** 群內純量排序標準陣列（依優先級排序） */
  readonly sortCriteria: readonly SortCriterion[];
  /** 數據缺失或未匹配時之降級策略 */
  readonly fallbackStrategy: FallbackStrategy;
  /** Sub-Agent 允許修復之最大疊代次數（預設 3 次） */
  readonly maxRepairIterations: number;
}

/**
 * 離散斷層描述
 * 記錄 Sub-Agent 掃描出之非連續項目區間
 */
export interface ContinuityGap {
  /** 發生斷層之分群鍵值 */
  readonly groupKey: string;
  /** 項目於排序後陣列中出現之非連續索引列表 */
  readonly actualIndices: readonly number[];
}

/**
 * Sub-Agent 認知邏輯自我檢驗結果
 */
export interface EvaluationResult {
  /** 相同分群項目是否 100% 處於連續索引區間 */
  readonly isContinuous: boolean;
  /** 偵測出之離散斷層數量 */
  readonly gapCount: number;
  /** 斷層詳細資訊列表 */
  readonly gaps: readonly ContinuityGap[];
  /** 經過修復強化後之 CognitivePlan（若無斷層則與原 Plan 相同） */
  readonly repairedPlan: CognitivePlan;
  /** 已執行之修復疊代次數 */
  readonly iterationsUsed: number;
}
