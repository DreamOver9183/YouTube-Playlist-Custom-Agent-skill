/**
 * ComparatorBuilder.ts — 動態比較器建構管道
 * 
 * 採用組合模式 (Composite Pattern) 動態合成多重比較函式。
 * 第一優先級：比較高階分群鍵 (Grouping Key) 權重，確保同實體數據強制相鄰。
 * 第二優先級：比較群內純量排序鍵 (In-Group Order Key)，處理細部次序。
 * 遵循 Early Return 範式與嚴格型別安全。
 */

import type { AttributeValue, EntityRecord } from '../types/entity.js';
import type { CognitivePlan, GroupDimension, NullHandlingStrategy, SortCriterion } from '../types/schema.js';

/** 比較器函式型態 */
export type RecordComparator<T> = (a: EntityRecord<T>, b: EntityRecord<T>) => number;

/**
 * 動態比較器建構管道
 */
export class ComparatorBuilder {
  /**
   * 根據 CognitivePlan 動態合成組合比較函式
   */
  public buildComparator<T>(plan: CognitivePlan): RecordComparator<T> {
    const groupComparators = plan.groupDimensions.map((dim) => this.buildGroupComparator<T>(dim));
    const sortComparators = plan.sortCriteria.map((crit) => this.buildSortComparator<T>(crit));

    return (a: EntityRecord<T>, b: EntityRecord<T>): number => {
      // 1. 優先比對高階實體分群維度
      for (const groupComp of groupComparators) {
        const result = groupComp(a, b);
        if (result !== 0) {
          return result;
        }
      }

      // 2. 次要比對群內純量排序標準
      for (const sortComp of sortComparators) {
        const result = sortComp(a, b);
        if (result !== 0) {
          return result;
        }
      }

      // 3. 穩定排序降級：若完全相同，保持原始索引次序
      return a.originalIndex - b.originalIndex;
    };
  }

  /**
   * 建構單一分群維度比較函式
   */
  private buildGroupComparator<T>(dimension: GroupDimension): RecordComparator<T> {
    return (a: EntityRecord<T>, b: EntityRecord<T>): number => {
      const valA = this.normalizeGroupValue(a.getValue(dimension.field), dimension);
      const valB = this.normalizeGroupValue(b.getValue(dimension.field), dimension);

      const nullComparison = this.compareNulls(valA, valB, dimension.nullHandling);
      if (nullComparison !== null) {
        return nullComparison;
      }

      // 非 Null 純量值比對
      if (valA === valB) {
        return 0;
      }
      if (valA! < valB!) {
        return -1;
      }
      return 1;
    };
  }

  /**
   * 建構單一排序標準比較函式
   */
  private buildSortComparator<T>(criterion: SortCriterion): RecordComparator<T> {
    return (a: EntityRecord<T>, b: EntityRecord<T>): number => {
      const valA = a.getValue(criterion.field);
      const valB = b.getValue(criterion.field);

      const nullComparison = this.compareNulls(valA, valB, criterion.nullHandling);
      if (nullComparison !== null) {
        return nullComparison;
      }

      let diff = 0;
      if (valA instanceof Date && valB instanceof Date) {
        diff = valA.getTime() - valB.getTime();
      } else if (typeof valA === 'number' && typeof valB === 'number') {
        diff = valA - valB;
      } else if (typeof valA === 'string' && typeof valB === 'string') {
        diff = valA.localeCompare(valB);
      } else if (valA === valB) {
        diff = 0;
      } else {
        diff = valA! < valB! ? -1 : 1;
      }

      if (criterion.direction === 'desc') {
        return -diff;
      }
      return diff;
    };
  }

  /**
   * 正規化分群屬性值（處理別名對照與小寫轉換）
   */
  private normalizeGroupValue(
    value: AttributeValue,
    dimension: GroupDimension
  ): AttributeValue {
    if (value === null || value === undefined) {
      return value;
    }

    const strVal = String(value).trim();
    if (dimension.aliasMap && dimension.aliasMap[strVal]) {
      return dimension.aliasMap[strVal]!.toLowerCase();
    }

    return strVal.toLowerCase();
  }

  /**
   * 空值 (Null/Undefined) 安全防護比較
   * 若雙方皆非 Null 則回傳 null 代表需進一步純量比較
   */
  private compareNulls(
    a: AttributeValue,
    b: AttributeValue,
    nullHandling: NullHandlingStrategy
  ): number | null {
    const isANull = a === null || a === undefined;
    const isBNull = b === null || b === undefined;

    if (!isANull && !isBNull) {
      return null;
    }
    if (isANull && isBNull) {
      return 0;
    }

    const nullValue = nullHandling === 'nulls_first' ? -1 : 1;
    return isANull ? nullValue : -nullValue;
  }
}
