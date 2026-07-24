/**
 * Evaluator.ts — 認知邏輯自我檢驗器 (Sub-Agent Loop)
 * 
 * 排序完成後自動掃描陣列，執行「連續性斷層檢查 (Continuity Check)」，
 * 驗證相同分群鍵項目是否 100% 處於連續索引區間。
 * 限制修復疊代最多 3 次，防止無窮迴圈。
 * 遵循 Early Return 範式與嚴格型別安全。
 */

import type { EntityRecord } from '../types/entity.js';
import type { CognitivePlan, ContinuityGap, EvaluationResult } from '../types/schema.js';
import type { Planner } from './Planner.js';

/**
 * 認知邏輯自我檢驗器
 */
export class Evaluator {
  /**
   * 評估排序後之陣列是否符合高階實體連續性
   */
  public evaluate<T>(
    sortedRecords: readonly EntityRecord<T>[],
    currentPlan: CognitivePlan
  ): EvaluationResult {
    if (sortedRecords.length <= 1) {
      return this.buildSuccessResult(currentPlan, 0);
    }

    if (currentPlan.groupDimensions.length === 0) {
      return this.buildSuccessResult(currentPlan, 0);
    }

    const gaps = this.detectContinuityGaps(sortedRecords, currentPlan);

    if (gaps.length === 0) {
      return this.buildSuccessResult(currentPlan, 0);
    }

    return {
      isContinuous: false,
      gapCount: gaps.length,
      gaps,
      repairedPlan: currentPlan,
      iterationsUsed: 0,
    };
  }

  /**
   * 執行修復與自我校驗迴圈 (Reflection Loop)
   * 限制最多疊代 3 次
   */
  public repairLoop<T>(
    sortedRecords: readonly EntityRecord<T>[],
    currentPlan: CognitivePlan,
    planner: Planner,
    resortFn: (plan: CognitivePlan) => readonly EntityRecord<T>[]
  ): {
    finalRecords: readonly EntityRecord<T>[];
    evaluation: EvaluationResult;
  } {
    let activePlan = currentPlan;
    let activeRecords = sortedRecords;
    const maxIterations = Math.min(currentPlan.maxRepairIterations, 3);

    for (let iteration = 1; iteration <= maxIterations; iteration++) {
      const gaps = this.detectContinuityGaps(activeRecords, activePlan);

      if (gaps.length === 0) {
        return {
          finalRecords: activeRecords,
          evaluation: this.buildSuccessResult(activePlan, iteration - 1),
        };
      }

      // 取得第一個出現斷層之欄位並進行強化
      const primaryGap = gaps[0];
      if (!primaryGap) {
        break;
      }

      const targetField = primaryGap.groupKey.split(':')[0] ?? '';
      activePlan = planner.strengthenPlan(activePlan, targetField);
      activeRecords = resortFn(activePlan);

      const reCheckGaps = this.detectContinuityGaps(activeRecords, activePlan);
      if (reCheckGaps.length === 0) {
        return {
          finalRecords: activeRecords,
          evaluation: this.buildSuccessResult(activePlan, iteration),
        };
      }
    }

    const finalGaps = this.detectContinuityGaps(activeRecords, activePlan);

    return {
      finalRecords: activeRecords,
      evaluation: {
        isContinuous: finalGaps.length === 0,
        gapCount: finalGaps.length,
        gaps: finalGaps,
        repairedPlan: activePlan,
        iterationsUsed: maxIterations,
      },
    };
  }

  /**
   * 偵測陣列中相同分群鍵之連續性斷層
   */
  private detectContinuityGaps<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan
  ): readonly ContinuityGap[] {
    const gaps: ContinuityGap[] = [];
    const primaryGroup = plan.groupDimensions[0];

    if (!primaryGroup) {
      return [];
    }

    const fieldName = primaryGroup.field;
    // 記錄每個 groupKey 在陣列中出現的所有索引位置
    const groupIndicesMap = new Map<string, number[]>();

    for (let idx = 0; idx < records.length; idx++) {
      const rec = records[idx];
      if (!rec) {
        continue;
      }

      const rawVal = rec.getValue(fieldName);
      const keyStr = rawVal !== null && rawVal !== undefined ? String(rawVal).toLowerCase() : '__null__';
      const compositeKey = `${fieldName}:${keyStr}`;

      const existing = groupIndicesMap.get(compositeKey);
      if (existing) {
        existing.push(idx);
      } else {
        groupIndicesMap.set(compositeKey, [idx]);
      }
    }

    // 檢查索引是否連續（即 maxIndex - minIndex + 1 === length）
    for (const [groupKey, indices] of groupIndicesMap.entries()) {
      if (indices.length <= 1) {
        continue;
      }

      const minIdx = indices[0] ?? 0;
      const maxIdx = indices[indices.length - 1] ?? 0;
      const expectedSpan = maxIdx - minIdx + 1;

      if (indices.length !== expectedSpan) {
        gaps.push({
          groupKey,
          actualIndices: indices,
        });
      }
    }

    return gaps;
  }

  /**
   * 建立成功評估結果
   */
  private buildSuccessResult(
    plan: CognitivePlan,
    iterationsUsed: number
  ): EvaluationResult {
    return {
      isContinuous: true,
      gapCount: 0,
      gaps: [],
      repairedPlan: plan,
      iterationsUsed,
    };
  }
}
