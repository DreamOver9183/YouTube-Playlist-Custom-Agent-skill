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

/** 群組序位對照表：正規化後的群組鍵 → 排列序位 */
type GroupRankTable = ReadonlyMap<string, number>;

/**
 * 動態比較器建構管道
 */
export class ComparatorBuilder {
  /**
   * 根據 CognitivePlan 動態合成組合比較函式
   *
   * @param plan 認知排序計畫
   * @param records 待排序資料（用於計算 `first_appearance` / `count_desc` 群組序位）。
   *   省略時所有分群維度一律退回字典序。
   */
  public buildComparator<T>(
    plan: CognitivePlan,
    records?: readonly EntityRecord<T>[]
  ): RecordComparator<T> {
    const groupComparators = plan.groupDimensions.map((dim) =>
      this.buildGroupComparator<T>(dim, this.buildGroupRankTable(dim, records))
    );
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
   * 建立群組序位對照表
   *
   * `first_appearance`（預設）依各群組首次出現的位置排列，`count_desc` 依群組
   * 大小排列（同大小則回到首次出現順序）。`lexical` 不需要對照表。
   */
  private buildGroupRankTable<T>(
    dimension: GroupDimension,
    records?: readonly EntityRecord<T>[]
  ): GroupRankTable | undefined {
    const strategy = dimension.groupOrder ?? 'first_appearance';
    if (strategy === 'lexical' || !records || records.length === 0) {
      return undefined;
    }

    const firstIndex = new Map<string, number>();
    const counts = new Map<string, number>();

    for (let idx = 0; idx < records.length; idx++) {
      const record = records[idx];
      if (!record) {
        continue;
      }
      const value = this.normalizeGroupValue(record.getValue(dimension.field), dimension);
      if (value === null || value === undefined) {
        continue;
      }
      const key = String(value);
      if (!firstIndex.has(key)) {
        firstIndex.set(key, idx);
      }
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }

    const keys = [...firstIndex.keys()];
    if (strategy === 'count_desc') {
      keys.sort((a, b) => {
        const diff = (counts.get(b) ?? 0) - (counts.get(a) ?? 0);
        return diff !== 0 ? diff : (firstIndex.get(a) ?? 0) - (firstIndex.get(b) ?? 0);
      });
    } else {
      keys.sort((a, b) => (firstIndex.get(a) ?? 0) - (firstIndex.get(b) ?? 0));
    }

    const table = new Map<string, number>();
    keys.forEach((key, rank) => table.set(key, rank));
    return table;
  }

  /**
   * 建構單一分群維度比較函式
   */
  private buildGroupComparator<T>(
    dimension: GroupDimension,
    rankTable?: GroupRankTable
  ): RecordComparator<T> {
    return (a: EntityRecord<T>, b: EntityRecord<T>): number => {
      const valA = this.normalizeGroupValue(a.getValue(dimension.field), dimension);
      const valB = this.normalizeGroupValue(b.getValue(dimension.field), dimension);

      const nullComparison = this.compareNulls(valA, valB, dimension.nullHandling);
      if (nullComparison !== null) {
        return nullComparison;
      }

      if (valA === valB) {
        return 0;
      }

      // 依群組序位排列（預設 first_appearance）
      if (rankTable) {
        const rankA = rankTable.get(String(valA));
        const rankB = rankTable.get(String(valB));
        if (rankA !== undefined && rankB !== undefined) {
          return rankA - rankB;
        }
      }

      // 字典序降級
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
   *
   * 公開給 Evaluator 使用：偵測器必須與比較器對「同一群」的定義完全一致，
   * 否則檢查出來的斷層只是兩套正規化規則的差異。
   */
  public normalizeGroupValue(
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
