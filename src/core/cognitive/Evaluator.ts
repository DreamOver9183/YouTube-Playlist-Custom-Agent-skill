/**
 * Evaluator.ts — 認知邏輯自我檢驗器 (Sub-Agent Loop)
 *
 * 排序完成後自動掃描陣列，執行兩種檢查：
 *
 * 1. 連續性斷層檢查 (Continuity Check)：**每一個**分群維度的相同鍵值項目
 *    是否處於連續索引區間。注意最高權重的維度必然連續（比較器第一順位就是
 *    它），真正會出現斷層的是次要維度——例如「先按專輯排、同歌手因此被打散」。
 *    只檢查 groupDimensions[0] 等於永遠回報成功，是沒有意義的保證。
 * 2. 群內排序單調性檢查 (Ordering Check)：群內是否確實依 sortCriteria 單調，
 *    以及空值是否落在 nullHandling 指定的一側。當計畫或自訂提取器不一致
 *    （例如同一欄位混用字串與數字，導致比較器不具遞移性）時會被抓出來。
 *
 * 修復迴圈最多疊代 3 次，並保留「斷層最少」的版本作為結果，避免在兩個互斥的
 * 分群目標之間來回震盪。
 * 遵循 Early Return 範式與嚴格型別安全。
 */

import type { EntityRecord } from '../types/entity.js';
import type { CognitivePlan, ContinuityGap, EvaluationResult, GroupDimension } from '../types/schema.js';
import { ComparatorBuilder } from '../engine/ComparatorBuilder.js';
import type { Planner } from './Planner.js';

/**
 * 認知邏輯自我檢驗器
 */
export class Evaluator {
  private readonly comparatorBuilder = new ComparatorBuilder();

  /**
   * 評估排序後之陣列是否符合高階實體連續性與群內排序單調性
   */
  public evaluate<T>(
    sortedRecords: readonly EntityRecord<T>[],
    currentPlan: CognitivePlan
  ): EvaluationResult {
    if (sortedRecords.length <= 1) {
      return this.buildSuccessResult(currentPlan, 0);
    }

    const gaps = this.detectGaps(sortedRecords, currentPlan);

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
   *
   * 每次疊代提升「第一個出現分群斷層」之欄位權重並重排；若最後仍無法完全收斂，
   * 回傳過程中斷層最少的版本，並如實回報剩餘斷層。
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
    const maxIterations = Math.max(0, Math.min(currentPlan.maxRepairIterations, 3));

    let activePlan = currentPlan;
    let activeRecords = sortedRecords;
    let activeGaps = this.detectGaps(activeRecords, activePlan);

    let bestPlan = activePlan;
    let bestRecords = activeRecords;
    let bestGaps = activeGaps;
    let bestIteration = 0;

    for (let iteration = 1; iteration <= maxIterations; iteration++) {
      if (activeGaps.length === 0) {
        return {
          finalRecords: activeRecords,
          evaluation: this.buildSuccessResult(activePlan, iteration - 1),
        };
      }

      // 只有分群斷層可以用提升權重修復；排序單調性斷層代表計畫本身不一致。
      const repairable = activeGaps.find((gap) => (gap.kind ?? 'grouping') === 'grouping');
      if (!repairable) {
        break;
      }

      const targetField = repairable.field ?? repairable.groupKey.split(':')[0] ?? '';
      if (!targetField) {
        break;
      }

      activePlan = planner.strengthenPlan(activePlan, targetField);
      activeRecords = resortFn(activePlan);
      activeGaps = this.detectGaps(activeRecords, activePlan);

      if (activeGaps.length < bestGaps.length) {
        bestPlan = activePlan;
        bestRecords = activeRecords;
        bestGaps = activeGaps;
        bestIteration = iteration;
      }

      if (activeGaps.length === 0) {
        return {
          finalRecords: activeRecords,
          evaluation: this.buildSuccessResult(activePlan, iteration),
        };
      }
    }

    return {
      finalRecords: bestRecords,
      evaluation: {
        isContinuous: bestGaps.length === 0,
        gapCount: bestGaps.length,
        gaps: bestGaps,
        repairedPlan: bestPlan,
        iterationsUsed: bestIteration,
      },
    };
  }

  /**
   * 掃描所有斷層（分群連續性 + 群內排序單調性）
   */
  public detectGaps<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan
  ): readonly ContinuityGap[] {
    return [
      ...this.detectContinuityGaps(records, plan),
      ...this.detectOrderingGaps(records, plan),
    ];
  }

  /**
   * 偵測陣列中相同分群鍵之連續性斷層（掃描全部分群維度）
   */
  private detectContinuityGaps<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan
  ): readonly ContinuityGap[] {
    const gaps: ContinuityGap[] = [];

    for (const dimension of plan.groupDimensions) {
      const groupIndicesMap = this.collectGroupIndices(records, dimension);

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
            kind: 'grouping',
            field: dimension.field,
          });
        }
      }
    }

    return gaps;
  }

  /**
   * 偵測群內排序單調性與空值位置違規
   */
  private detectOrderingGaps<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan
  ): readonly ContinuityGap[] {
    if (plan.sortCriteria.length === 0) {
      return [];
    }

    const gaps: ContinuityGap[] = [];
    const primary = plan.groupDimensions[0];

    // 以最高權重分群維度切出區塊；沒有分群維度時整個陣列即為單一區塊。
    let blockStart = 0;
    while (blockStart < records.length) {
      const blockKey = primary ? this.groupKeyOf(records[blockStart], primary) : '__all__';
      let blockEnd = blockStart + 1;
      if (primary) {
        while (
          blockEnd < records.length &&
          this.groupKeyOf(records[blockEnd], primary) === blockKey
        ) {
          blockEnd++;
        }
      } else {
        blockEnd = records.length;
      }

      const gap = this.findOrderingViolation(records, plan, blockStart, blockEnd, blockKey);
      if (gap) {
        gaps.push(gap);
      }

      blockStart = blockEnd;
    }

    return gaps;
  }

  /**
   * 在單一區塊內尋找第一個排序違規
   *
   * 多重排序鍵是字典序關係：第 k 個鍵只在前 k-1 個鍵相等的子區段內才需要單調。
   * 因此每檢查完一個鍵，就把區段細分成「該鍵值相同」的子區段再往下檢查。
   */
  private findOrderingViolation<T>(
    records: readonly EntityRecord<T>[],
    plan: CognitivePlan,
    start: number,
    end: number,
    blockKey: string
  ): ContinuityGap | null {
    let segments: Array<[number, number]> = [[start, end]];

    for (const criterion of plan.sortCriteria) {
      const nextSegments: Array<[number, number]> = [];

      for (const [segStart, segEnd] of segments) {
        let previous: unknown = undefined;
        let previousIndex = -1;
        let seenNull = false;
        let runStart = segStart;

        for (let idx = segStart; idx < segEnd; idx++) {
          const value = records[idx]?.getValue(criterion.field);
          const isNull = value === null || value === undefined;

          if (isNull) {
            if (criterion.nullHandling === 'nulls_first' && previousIndex >= 0) {
              return {
                groupKey: `${criterion.field}:${blockKey}`,
                actualIndices: [previousIndex, idx],
                kind: 'ordering',
                field: criterion.field,
              };
            }
            seenNull = true;
            continue;
          }

          if (seenNull && criterion.nullHandling === 'nulls_last') {
            return {
              groupKey: `${criterion.field}:${blockKey}`,
              actualIndices: [idx - 1, idx],
              kind: 'ordering',
              field: criterion.field,
            };
          }

          if (previousIndex >= 0) {
            if (!this.isMonotonic(previous, value, criterion.direction)) {
              return {
                groupKey: `${criterion.field}:${blockKey}`,
                actualIndices: [previousIndex, idx],
                kind: 'ordering',
                field: criterion.field,
              };
            }
            if (previous !== value) {
              nextSegments.push([runStart, idx]);
              runStart = idx;
            }
          }

          previous = value;
          previousIndex = idx;
        }

        if (previousIndex >= 0 && previousIndex + 1 - runStart > 1) {
          nextSegments.push([runStart, previousIndex + 1]);
        }
      }

      segments = nextSegments.filter(([s, e]) => e - s > 1);
      if (segments.length === 0) {
        break;
      }
    }

    return null;
  }

  /**
   * 收集每個分群鍵在陣列中出現的所有索引位置
   */
  private collectGroupIndices<T>(
    records: readonly EntityRecord<T>[],
    dimension: GroupDimension
  ): Map<string, number[]> {
    const groupIndicesMap = new Map<string, number[]>();

    for (let idx = 0; idx < records.length; idx++) {
      const record = records[idx];
      if (!record) {
        continue;
      }

      const compositeKey = this.groupKeyOf(record, dimension);
      const existing = groupIndicesMap.get(compositeKey);
      if (existing) {
        existing.push(idx);
      } else {
        groupIndicesMap.set(compositeKey, [idx]);
      }
    }

    return groupIndicesMap;
  }

  /**
   * 產生與 ComparatorBuilder 完全一致的分群鍵
   */
  private groupKeyOf<T>(
    record: EntityRecord<T> | undefined,
    dimension: GroupDimension
  ): string {
    if (!record) {
      return `${dimension.field}:__null__`;
    }
    const normalized = this.comparatorBuilder.normalizeGroupValue(
      record.getValue(dimension.field),
      dimension
    );
    const keyStr = normalized !== null && normalized !== undefined ? String(normalized) : '__null__';
    return `${dimension.field}:${keyStr}`;
  }

  /**
   * 判斷兩個相鄰值是否符合指定方向的單調性
   */
  private isMonotonic(previous: unknown, current: unknown, direction: 'asc' | 'desc'): boolean {
    let comparison: number;

    if (previous instanceof Date && current instanceof Date) {
      comparison = previous.getTime() - current.getTime();
    } else if (typeof previous === 'number' && typeof current === 'number') {
      comparison = previous - current;
    } else if (typeof previous === 'string' && typeof current === 'string') {
      comparison = previous.localeCompare(current);
    } else if (previous === current) {
      comparison = 0;
    } else {
      // 型別不一致代表提取器或計畫本身有問題，直接判定違規。
      return false;
    }

    return direction === 'desc' ? comparison >= 0 : comparison <= 0;
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
