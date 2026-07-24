/**
 * Planner.ts — 階層排序思考樹產生器
 * 
 * 負責建構多層級排序思考樹 (Grouping Dimensions -> Sort Within Group Criteria)，
 * 建立具備權重排序與例外降級策略的結構化 CognitivePlan。
 * 遵循 Early Return 範式與嚴格型別安全。
 */

import type { ExtractedFeatures } from './Extractor.js';
import type { CognitivePlan, FallbackStrategy, GroupDimension, SortCriterion } from '../types/schema.js';

/** Planner 自訂設定選項 */
export interface PlannerOptions {
  readonly groupDimensions?: readonly GroupDimension[];
  readonly sortCriteria?: readonly SortCriterion[];
  readonly fallbackStrategy?: FallbackStrategy;
  readonly maxRepairIterations?: number;
}

/**
 * 階層排序思考樹產生器
 */
export class Planner {
  /**
   * 根據語意提取特徵與設定選項建構 CognitivePlan
   */
  public createPlan(
    extractedFeatures: ExtractedFeatures,
    options?: PlannerOptions
  ): CognitivePlan {
    const rawGroups = options?.groupDimensions ?? extractedFeatures.groupDimensions;
    const rawSorts = options?.sortCriteria ?? extractedFeatures.sortCriteria;
    const fallbackStrategy = options?.fallbackStrategy ?? 'move_to_end';
    const maxRepairIterations = options?.maxRepairIterations ?? 3;

    // 將分群維度按權重降冪排序（權重越高者越早作為高階分群鍵）
    const sortedGroups = this.normalizeGroupDimensions(rawGroups);
    const sortedSorts = this.normalizeSortCriteria(rawSorts);

    return {
      groupDimensions: sortedGroups,
      sortCriteria: sortedSorts,
      fallbackStrategy,
      maxRepairIterations,
    };
  }

  /**
   * 強化或修復現有 Plan（由 Evaluator 呼叫）
   * 當發現離散斷層時，提升目標分群維度之權重
   */
  public strengthenPlan(
    existingPlan: CognitivePlan,
    targetGroupField: string
  ): CognitivePlan {
    const updatedDimensions = existingPlan.groupDimensions.map((dim) => {
      if (dim.field !== targetGroupField) {
        return dim;
      }
      // 增加權重以強化該分群鍵之優先權
      return {
        ...dim,
        weight: dim.weight + 50,
      };
    });

    const sortedGroups = this.normalizeGroupDimensions(updatedDimensions);

    return {
      ...existingPlan,
      groupDimensions: sortedGroups,
    };
  }

  /**
   * 正規化分群維度並按權重降冪排列
   */
  private normalizeGroupDimensions(
    dimensions: readonly GroupDimension[]
  ): readonly GroupDimension[] {
    if (dimensions.length === 0) {
      return [];
    }

    // 複製陣列並依權重降冪排序
    const sorted = [...dimensions].sort((a, b) => b.weight - a.weight);
    return sorted;
  }

  /**
   * 正規化群內排序標準
   */
  private normalizeSortCriteria(
    criteria: readonly SortCriterion[]
  ): readonly SortCriterion[] {
    if (criteria.length === 0) {
      return [];
    }

    return [...criteria];
  }
}
