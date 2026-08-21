/**
 * index.ts — 通用型認知分群與多維度排序思考引擎 統一對外進入點
 * 
 * 匯出核心型態、認知元件、引擎模組與適配器範例。
 * 提供通用便利函式 `cognitiveSort<T>()`。
 * 遵循台灣繁體中文專業術語與嚴格型別安全。
 */

// 型態定義匯出
export type { AttributePrimitive, AttributeValue, AttributeExtractor, EntityRecord } from './core/types/entity.js';
export type {
  SortDirection,
  NullHandlingStrategy,
  FallbackStrategy,
  GroupOrderStrategy,
  GapKind,
  GroupDimension,
  SortCriterion,
  CognitivePlan,
  ContinuityGap,
  EvaluationResult,
} from './core/types/schema.js';

// 認知思考管道元件匯出
export { Extractor, type NaturalLanguageIntent, type ExtractedFeatures } from './core/cognitive/Extractor.js';
export { Planner, type PlannerOptions } from './core/cognitive/Planner.js';
export { Evaluator } from './core/cognitive/Evaluator.js';

// 核心排序引擎匯出
export { ComparatorBuilder, type RecordComparator } from './core/engine/ComparatorBuilder.js';
export { SortingEngine, type SortingResult } from './core/engine/SortingEngine.js';

// 適配器範例匯出
export { TrackListAdapter, type MusicTrack } from './adapters/TrackListAdapter.js';
export { ECommerceAdapter, type ProductItem } from './adapters/ECommerceAdapter.js';

import { Extractor, type NaturalLanguageIntent } from './core/cognitive/Extractor.js';
import { Planner } from './core/cognitive/Planner.js';
import { SortingEngine, type SortingResult } from './core/engine/SortingEngine.js';

/** 推導欄位宇宙時取樣的筆數上限 */
const FIELD_SAMPLE_SIZE = 20;

/**
 * 通用認知分群與排序便利函式
 * 
 * 接受任意泛型資料物件陣列與自然語言需求意圖，
 * 自動執行「特徵提取 -> 思考樹建構 -> 比較器合成 -> 穩定排序 -> Sub-Agent 斷層校驗」全管道。
 * 
 * @example
 * ```ts
 * const sortedResult = cognitiveSort(myItems, {
 *   text: "請將同專輯曲目歸類，並按軌號排序"
 * });
 * ```
 */
export function cognitiveSort<T extends Record<string, unknown>>(
  items: readonly T[],
  intent?: NaturalLanguageIntent
): SortingResult<T> {
  if (items.length <= 1) {
    const defaultPlan = new Planner().createPlan({ groupDimensions: [], sortCriteria: [] });
    return {
      items: [...items],
      plan: defaultPlan,
      evaluation: {
        isContinuous: true,
        gapCount: 0,
        gaps: [],
        repairedPlan: defaultPlan,
        iterationsUsed: 0,
      },
    };
  }

  // 欄位宇宙取前 N 筆的聯集：只看 items[0] 會在第一筆剛好缺欄位時整組失準。
  const fieldUnion = new Set<string>();
  for (const item of items.slice(0, FIELD_SAMPLE_SIZE)) {
    for (const field of Object.keys(item)) {
      fieldUnion.add(field);
    }
  }

  const extractor = new Extractor();
  const features = extractor.extractFromFields([...fieldUnion], intent);

  const planner = new Planner();
  const plan = planner.createPlan(features);

  const engine = new SortingEngine();
  return engine.sort(items, plan);
}
