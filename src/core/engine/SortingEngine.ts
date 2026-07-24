/**
 * SortingEngine.ts — 核心排序執行引擎
 * 
 * 負責接受泛型資料載體陣列與 CognitivePlan，執行動態比較器建構、穩定排序
 * 與 Sub-Agent 斷層修復迴圈。
 * 遵循 Early Return 範式、嚴格型別安全與台灣繁體中文專業術語。
 */

import { defaultAttributeExtractor, type AttributeExtractor, type EntityRecord } from '../types/entity.js';
import type { CognitivePlan, EvaluationResult } from '../types/schema.js';
import { Evaluator } from '../cognitive/Evaluator.js';
import { Planner } from '../cognitive/Planner.js';
import { ComparatorBuilder } from './ComparatorBuilder.js';

/** 排序執行結果封裝 */
export interface SortingResult<T> {
  /** 排序完成之原始資料陣列 */
  readonly items: readonly T[];
  /** 最終使用之 CognitivePlan */
  readonly plan: CognitivePlan;
  /** Sub-Agent 認知自我檢驗結果 */
  readonly evaluation: EvaluationResult;
}

/**
 * 核心排序執行引擎
 */
export class SortingEngine {
  private readonly comparatorBuilder = new ComparatorBuilder();
  private readonly evaluator = new Evaluator();
  private readonly planner = new Planner();

  /**
   * 執行通用認知分群與排序管道
   */
  public sort<T>(
    items: readonly T[],
    plan: CognitivePlan,
    customExtractor?: AttributeExtractor<T>
  ): SortingResult<T> {
    if (items.length <= 1) {
      return {
        items: [...items],
        plan,
        evaluation: {
          isContinuous: true,
          gapCount: 0,
          gaps: [],
          repairedPlan: plan,
          iterationsUsed: 0,
        },
      };
    }

    // 1. 包裝原始資料為 EntityRecord 陣列
    const extractor = customExtractor ?? (defaultAttributeExtractor as unknown as AttributeExtractor<T>);
    const records = this.wrapRecords(items, extractor);

    // 2. 執行單次初次排序
    const sortedRecords = this.executeSingleSort(records, plan);

    // 3. 執行 Sub-Agent 斷層修復與自我校驗迴圈 (Reflection Loop)
    const { finalRecords, evaluation } = this.evaluator.repairLoop(
      sortedRecords,
      plan,
      this.planner,
      (repairedPlan) => this.executeSingleSort(records, repairedPlan)
    );

    // 4. 解包 EntityRecord 回傳原始數據陣列
    const finalItems = finalRecords.map((r) => r.raw);

    return {
      items: finalItems,
      plan: evaluation.repairedPlan,
      evaluation,
    };
  }

  /**
   * 執行單次比較排序
   */
  private executeSingleSort<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan
  ): readonly EntityRecord<T>[] {
    const comparator = this.comparatorBuilder.buildComparator<T>(plan);
    const sorted = [...records].sort(comparator);
    return sorted;
  }

  /**
   * 將原始資料物件陣列包裝為 EntityRecord 陣列
   */
  private wrapRecords<T>(
    items: readonly T[],
    extractor: AttributeExtractor<T>
  ): readonly EntityRecord<T>[] {
    return items.map((raw, originalIndex) => ({
      raw,
      originalIndex,
      getValue: (fieldName: string) => extractor(raw, fieldName),
    }));
  }
}
