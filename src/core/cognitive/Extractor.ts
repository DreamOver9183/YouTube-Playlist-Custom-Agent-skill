/**
 * Extractor.ts — 語意與屬性提取器
 * 
 * 負責分析數據特徵與自然語言需求意圖，自動識別高階實體欄位與次要純量欄位。
 * 遵循 Early Return 範式、嚴格型別安全與台灣繁體中文專業術語。
 */

import type { GroupDimension, NullHandlingStrategy, SortCriterion, SortDirection } from '../types/schema.js';

/** 自然語言意圖或需求結構 */
export interface NaturalLanguageIntent {
  /** 描述文字（例如：「將同專輯的曲目放在一起，按軌號排序」） */
  readonly text?: string;
  /** 顯式指定之高階分群欄位列表 */
  readonly explicitGroupFields?: readonly string[];
  /** 顯式指定之群內排序欄位列表 */
  readonly explicitSortFields?: readonly string[];
}

/** 語意特徵提取結果 */
export interface ExtractedFeatures {
  readonly groupDimensions: readonly GroupDimension[];
  readonly sortCriteria: readonly SortCriterion[];
}

/**
 * 語意屬性提取器
 */
export class Extractor {
  /**
   * 根據自然語言意圖與樣本資料屬性解析排序特徵
   * 採用 Early Return 模式，消弭巢狀結構
   */
  public extract(
    sampleItem: Record<string, unknown> | null | undefined,
    intent?: NaturalLanguageIntent
  ): ExtractedFeatures {
    if (!sampleItem) {
      return this.buildDefaultFeatures();
    }

    const availableFields = Object.keys(sampleItem);
    if (availableFields.length === 0) {
      return this.buildDefaultFeatures();
    }

    const explicitGroups = intent?.explicitGroupFields;
    const explicitSorts = intent?.explicitSortFields;
    const text = intent?.text ? intent.text.toLowerCase() : '';

    const groupDimensions = this.deriveGroupDimensions(availableFields, explicitGroups, text);
    const sortCriteria = this.deriveSortCriteria(availableFields, explicitSorts, text);

    return {
      groupDimensions,
      sortCriteria,
    };
  }

  /**
   * 推導高階分群維度
   */
  private deriveGroupDimensions(
    fields: readonly string[],
    explicitFields?: readonly string[],
    intentText: string = ''
  ): readonly GroupDimension[] {
    if (explicitFields && explicitFields.length > 0) {
      return explicitFields.map((field, index) => ({
        field,
        weight: 100 - index * 10,
        nullHandling: 'nulls_last' as NullHandlingStrategy,
      }));
    }

    // 常見高階實體語意欄位關鍵字（通用抽象，非單一領域）
    const highOrderKeywords = ['album', 'category', 'group', 'artist', 'channel', 'series', 'brand', 'project'];
    const matchedFields: GroupDimension[] = [];

    for (const field of fields) {
      const lowerField = field.toLowerCase();
      const isMatch = highOrderKeywords.some(
        (kw) => lowerField.includes(kw) || (intentText.length > 0 && intentText.includes(kw))
      );

      if (!isMatch) {
        continue;
      }

      matchedFields.push({
        field,
        weight: 100 - matchedFields.length * 10,
        nullHandling: 'nulls_last',
      });
    }

    if (matchedFields.length > 0) {
      return matchedFields;
    }

    // 預設降級：若無比對成功的高階欄位，回傳空陣列
    return [];
  }

  /**
   * 推導群內純量排序標準
   */
  private deriveSortCriteria(
    fields: readonly string[],
    explicitFields?: readonly string[],
    intentText: string = ''
  ): readonly SortCriterion[] {
    if (explicitFields && explicitFields.length > 0) {
      return explicitFields.map((field) => ({
        field,
        direction: 'asc' as SortDirection,
        nullHandling: 'nulls_last' as NullHandlingStrategy,
      }));
    }

    // 常見群內純量排序欄位關鍵字（通用抽象，非單一領域）
    const scalarKeywords = ['track', 'number', 'index', 'position', 'order', 'date', 'time', 'price', 'view', 'score'];
    const isDesc = intentText.includes('降冪') || intentText.includes('高到低') || intentText.includes('desc');
    const direction: SortDirection = isDesc ? 'desc' : 'asc';
    const criteria: SortCriterion[] = [];

    for (const field of fields) {
      const lowerField = field.toLowerCase();
      const isMatch = scalarKeywords.some((kw) => lowerField.includes(kw));

      if (!isMatch) {
        continue;
      }

      criteria.push({
        field,
        direction,
        nullHandling: 'nulls_last',
      });
    }

    if (criteria.length > 0) {
      return criteria;
    }

    // 預設降級：使用第一個可用欄位作為次要排序
    const defaultField = fields[0];
    if (!defaultField) {
      return [];
    }

    return [
      {
        field: defaultField,
        direction: 'asc',
        nullHandling: 'nulls_last',
      },
    ];
  }

  /**
   * 預設特徵建構
   */
  private buildDefaultFeatures(): ExtractedFeatures {
    return {
      groupDimensions: [],
      sortCriteria: [],
    };
  }
}
