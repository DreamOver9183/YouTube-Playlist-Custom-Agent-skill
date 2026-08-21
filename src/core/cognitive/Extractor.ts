/**
 * Extractor.ts — 語意與屬性提取器
 *
 * 負責分析數據特徵與自然語言需求意圖，自動識別高階實體欄位與次要純量欄位。
 *
 * 核心規則：**意圖文字只能用來「挑選」實際存在的欄位，永遠不能自己成為比對條件**。
 * （舊版寫成 `field.includes(kw) || intentText.includes(kw)`，只要需求描述裡出現
 * 任一關鍵字，樣本物件的每一個欄位都會被當成分群維度。）
 *
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

/** 高階實體語意詞彙表（正規詞 → 中英同義詞），通用抽象、非單一領域 */
const GROUP_VOCABULARY: Readonly<Record<string, readonly string[]>> = {
  artist: ['artist', '歌手', '藝人', '演出者'],
  album: ['album', '專輯'],
  channel: ['channel', '頻道'],
  category: ['category', '分類', '類別'],
  group: ['group', '群組', '團體'],
  series: ['series', '系列'],
  brand: ['brand', '品牌'],
  project: ['project', '專案', '專題'],
  owner: ['owner', 'author', '作者', '擁有者'],
};

/** 群內純量排序語彙表（正規詞 → 中英同義詞） */
const SORT_VOCABULARY: Readonly<Record<string, readonly string[]>> = {
  track: ['track', '軌號', '曲序'],
  number: ['number', '編號'],
  index: ['index', '索引'],
  position: ['position', '位置'],
  order: ['order', '順序'],
  date: ['date', 'published', '日期', '發布', '發佈'],
  time: ['time', 'duration', '時長', '長度', '時間'],
  price: ['price', '價格', '售價'],
  view: ['view', '觀看', '播放', '點閱'],
  score: ['score', 'rating', '評分', '分數'],
  like: ['like', '按讚', '喜歡'],
};

/** 降冪語意標記 */
const DESC_MARKERS: readonly string[] = [
  'desc', 'descending', '降冪', '遞減', '倒序', '由高到低', '從高到低', '高到低',
  '由多到少', '從多到少', '多到少', '由新到舊', '從新到舊', '新到舊', '最多', '最高',
  // 長度／大小／新舊的口語說法：少了這些，「時間最長的排最前面」會被判成升冪。
  '由長到短', '從長到短', '長到短', '由大到小', '從大到小', '大到小',
  '最長', '最久', '最大', '最新',
];

/** 升冪語意標記 */
const ASC_MARKERS: readonly string[] = [
  'asc', 'ascending', '升冪', '遞增', '正序', '由低到高', '從低到高', '低到高',
  '由少到多', '從少到多', '少到多', '由舊到新', '從舊到新', '舊到新', '最少', '最低',
  '由短到長', '從短到長', '短到長', '由小到大', '從小到大', '小到大',
  '最短', '最小', '最舊', '最早',
];

/** 在關鍵字之後往前看多少字元來判斷該欄位的排序方向 */
const DIRECTION_LOOKAHEAD = 18;

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

    return this.extractFromFields(Object.keys(sampleItem), intent);
  }

  /**
   * 直接以欄位清單進行提取（供多筆樣本聯集使用）
   */
  public extractFromFields(
    availableFields: readonly string[],
    intent?: NaturalLanguageIntent
  ): ExtractedFeatures {
    if (availableFields.length === 0) {
      return this.buildDefaultFeatures();
    }

    const text = intent?.text ? intent.text.toLowerCase() : '';

    return {
      groupDimensions: this.deriveGroupDimensions(
        availableFields,
        intent?.explicitGroupFields,
        text
      ),
      sortCriteria: this.deriveSortCriteria(
        availableFields,
        intent?.explicitSortFields,
        text
      ),
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

    const matches = this.matchFields(fields, GROUP_VOCABULARY, intentText);

    return matches.map((match, index) => ({
      field: match.field,
      weight: 100 - index * 10,
      nullHandling: 'nulls_last' as NullHandlingStrategy,
    }));
  }

  /**
   * 推導群內純量排序標準
   */
  private deriveSortCriteria(
    fields: readonly string[],
    explicitFields?: readonly string[],
    intentText: string = ''
  ): readonly SortCriterion[] {
    const globalDirection = this.detectGlobalDirection(intentText);

    if (explicitFields && explicitFields.length > 0) {
      return explicitFields.map((field) => ({
        field,
        direction: this.detectFieldDirection(field, [field], intentText, globalDirection),
        nullHandling: 'nulls_last' as NullHandlingStrategy,
      }));
    }

    // 有需求描述時就必須真的講到某個欄位才算數。舊版在「講了方向但沒講欄位」
    // （例如「每組裡面由新到舊排列」）時會把所有候選欄位全部選進來，於是
    // position 變成主鍵——群內順序等於原索引倒轉，而不是使用者要的日期。
    const matches = this.matchFields(
      fields,
      SORT_VOCABULARY,
      intentText,
      intentText.length > 0
    );

    if (matches.length > 0) {
      return matches.map((match) => ({
        field: match.field,
        direction: this.detectFieldDirection(
          match.field,
          match.terms,
          intentText,
          globalDirection
        ),
        nullHandling: 'nulls_last' as NullHandlingStrategy,
      }));
    }

    // 沒有需求描述時（純欄位推導）才降級成第一個可用欄位。
    const defaultField = intentText.length === 0 ? fields[0] : undefined;
    if (!defaultField) {
      return [];
    }

    return [
      {
        field: defaultField,
        direction: globalDirection ?? 'asc',
        nullHandling: 'nulls_last',
      },
    ];
  }

  /**
   * 以詞彙表比對欄位，並用意圖文字縮小範圍
   *
   * 1. 先找出「欄位名稱本身」符合詞彙表的候選欄位。
   * 2. 若意圖文字有提到其中某些欄位（欄位名或同義詞），只保留那些。
   *    意圖文字永遠不會讓不相關的欄位入選。
   */
  private matchFields(
    fields: readonly string[],
    vocabulary: Readonly<Record<string, readonly string[]>>,
    intentText: string,
    requireMention: boolean = false
  ): ReadonlyArray<{ field: string; terms: readonly string[] }> {
    const candidates: Array<{ field: string; terms: readonly string[] }> = [];

    for (const field of fields) {
      const normalizedField = field.toLowerCase().replace(/[_\-\s]/g, '');
      const terms: string[] = [];

      for (const synonyms of Object.values(vocabulary)) {
        for (const synonym of synonyms) {
          const normalizedSynonym = synonym.toLowerCase().replace(/[_\-\s]/g, '');
          if (normalizedField.includes(normalizedSynonym)) {
            terms.push(...synonyms);
            break;
          }
        }
      }

      if (terms.length > 0) {
        candidates.push({ field, terms: [...new Set([field.toLowerCase(), ...terms])] });
      }
    }

    if (candidates.length === 0 || intentText.length === 0) {
      return candidates;
    }

    const mentioned = candidates.filter((candidate) =>
      candidate.terms.some((term) => intentText.includes(term.toLowerCase()))
    );

    if (mentioned.length > 0) {
      return mentioned;
    }
    // requireMention: 寧可什麼都不選，也不要在使用者沒指名欄位時全選。
    return requireMention ? [] : candidates;
  }

  /**
   * 偵測整段意圖文字的排序方向（無明確標記時回傳 undefined）
   */
  private detectGlobalDirection(intentText: string): SortDirection | undefined {
    if (intentText.length === 0) {
      return undefined;
    }
    const descAt = this.firstMarkerIndex(intentText, DESC_MARKERS);
    const ascAt = this.firstMarkerIndex(intentText, ASC_MARKERS);

    if (descAt < 0 && ascAt < 0) {
      return undefined;
    }
    if (ascAt < 0) {
      return 'desc';
    }
    if (descAt < 0) {
      return 'asc';
    }
    return descAt < ascAt ? 'desc' : 'asc';
  }

  /**
   * 偵測單一欄位的排序方向
   *
   * 在意圖文字中找到該欄位的提及位置，只看它後面一小段文字，
   * 讓「觀看次數由高到低、標題由 A 到 Z」這種混合方向的需求可以被表達。
   */
  private detectFieldDirection(
    field: string,
    terms: readonly string[],
    intentText: string,
    globalDirection: SortDirection | undefined
  ): SortDirection {
    if (intentText.length === 0) {
      return globalDirection ?? 'asc';
    }

    for (const term of [field.toLowerCase(), ...terms]) {
      const at = intentText.indexOf(term.toLowerCase());
      if (at < 0) {
        continue;
      }
      const window = intentText.slice(at, at + term.length + DIRECTION_LOOKAHEAD);
      const descAt = this.firstMarkerIndex(window, DESC_MARKERS);
      const ascAt = this.firstMarkerIndex(window, ASC_MARKERS);
      if (descAt >= 0 && (ascAt < 0 || descAt < ascAt)) {
        return 'desc';
      }
      if (ascAt >= 0) {
        return 'asc';
      }
    }

    return globalDirection ?? 'asc';
  }

  /**
   * 回傳最早出現之標記位置，找不到回傳 -1
   */
  private firstMarkerIndex(text: string, markers: readonly string[]): number {
    let best = -1;
    for (const marker of markers) {
      const at = text.indexOf(marker);
      if (at >= 0 && (best < 0 || at < best)) {
        best = at;
      }
    }
    return best;
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
