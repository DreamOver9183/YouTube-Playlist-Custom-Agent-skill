/**
 * learned-vocabulary.ts — 滾動式學習累積下來的詞彙（由工具產生，但進版控）
 *
 * **請不要手改這個檔案。** 它由 `tests/dev/learn_scenarios.py --promote` 產生：
 * 開發者在測試中餵入不完整的 Prompt，把引擎辨識不出來的說法記成待審情境，
 * 判定為 `vocabulary`（只是沒收錄的講法，不是本質模糊）之後才會落到這裡。
 *
 * 合併語意由 `Extractor.ts` 負責：
 * - `group` / `sort` 以「正規詞」為鍵，同義詞併入既有條目（既有條目優先，去重）。
 * - `descMarkers` / `ascMarkers` 直接併入既有標記陣列。
 *
 * 學來的詞彙**不享有任何特權**：它一樣只能用來「挑選實際存在的欄位」，
 * 永遠不能自己成為比對條件（見 Extractor.ts 檔頭記載的舊 bug）。
 */

/** 學習所得的詞彙增量 */
export interface LearnedVocabulary {
  /** 高階分群語彙增量（正規詞 → 追加的同義詞） */
  readonly group: Readonly<Record<string, readonly string[]>>;
  /** 群內排序語彙增量（正規詞 → 追加的同義詞） */
  readonly sort: Readonly<Record<string, readonly string[]>>;
  /** 追加的降冪語意標記 */
  readonly descMarkers: readonly string[];
  /** 追加的升冪語意標記 */
  readonly ascMarkers: readonly string[];
}

export const LEARNED_VOCABULARY: LearnedVocabulary = {
  "group": {},
  "sort": {
    "view": [
      "熱門",
      "熱門程度"
    ]
  },
  "descMarkers": [],
  "ascMarkers": []
};
