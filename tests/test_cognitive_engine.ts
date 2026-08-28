/**
 * test_cognitive_engine.ts — 認知排序思考引擎完整單元與整合測試集
 * 
 * 涵蓋：
 * 1. 高階實體語意分群優先權測試
 * 2. Sub-Agent 連續性斷層掃描與修復迴圈測試 (Reflection Loop)
 * 3. 跨領域資料集通用性測試（Bug 報告管理系統）
 * 4. 空值與缺失屬性容錯與 Early Return 測試
 * 遵循台灣繁體中文專業術語與嚴格型別規範。
 */

import { cognitiveSort } from '../src/index.js';
import { TrackListAdapter, type MusicTrack } from '../src/adapters/TrackListAdapter.js';
import { SortingEngine } from '../src/core/engine/SortingEngine.js';
import { Extractor } from '../src/core/cognitive/Extractor.js';
import { Evaluator } from '../src/core/cognitive/Evaluator.js';
import { LEARNED_VOCABULARY } from '../src/core/cognitive/learned-vocabulary.js';

let passed = 0;
let failed = 0;

function assert(condition: boolean, message: string): void {
  if (condition) {
    console.log(`  ✓ ${message}`);
    passed++;
  } else {
    console.error(`  ✗ ${message}`);
    failed++;
  }
}

// ─────────────────────────────────────────────
// 測試 1：音樂曲目適配器與語意分群優先權
// ─────────────────────────────────────────────
function testMusicTrackSorting(): void {
  console.log('\n============================================================');
  console.log(' 測試 1：音樂曲目適配器與高階語意分群');
  console.log('============================================================');

  const tracks: MusicTrack[] = [
    { id: '1', title: 'Song B2', artist: 'Artist B', album: 'Album B', trackNumber: 2, durationSeconds: 200 },
    { id: '2', title: 'Song A2', artist: 'Artist A', album: 'Album A', trackNumber: 2, durationSeconds: 180 },
    { id: '3', title: 'Song B1', artist: 'Artist B', album: 'Album B', trackNumber: 1, durationSeconds: 210 },
    { id: '4', title: 'Song A1', artist: 'Artist A', album: 'Album A', trackNumber: 1, durationSeconds: 190 },
  ];

  const adapter = new TrackListAdapter();
  const result = adapter.sortTracks(tracks);

  assert(result.items.length === 4, '應包含所有 4 首曲目');
  assert(result.evaluation.isContinuous, '相同歌手與專輯項目應 100% 連續');

  const artistOrder = result.items.map((t) => t.artist);
  const trackNumOrder = result.items.map((t) => t.trackNumber);

  // 驗證同歌手項目是否連續
  const firstArtist = artistOrder[0];
  assert(artistOrder[0] === artistOrder[1], `前兩首應屬於同歌手 (${firstArtist})`);
  assert(trackNumOrder[0] === 1 && trackNumOrder[1] === 2, '同歌手群內應按軌號 1, 2 排序');
}

// ─────────────────────────────────────────────
// 測試 2：Sub-Agent 斷層掃描與 3 次修復迴圈
// ─────────────────────────────────────────────
function testEvaluatorRepairLoop(): void {
  console.log('\n============================================================');
  console.log(' 測試 2：Sub-Agent 連續性斷層掃描與修復迴圈');
  console.log('============================================================');

  const rawItems = [
    { id: '1', category: 'Electronics', name: 'Laptop A', price: 1000 },
    { id: '2', category: 'Books', name: 'Book A', price: 20 },
    { id: '3', category: 'Electronics', name: 'Phone B', price: 800 },
    { id: '4', category: 'Books', name: 'Book B', price: 15 },
  ];

  const plan = {
    groupDimensions: [
      { field: 'category', weight: 10, nullHandling: 'nulls_last' as const },
    ],
    sortCriteria: [
      { field: 'price', direction: 'asc' as const, nullHandling: 'nulls_last' as const },
    ],
    fallbackStrategy: 'move_to_end' as const,
    maxRepairIterations: 3,
  };

  const engine = new SortingEngine();
  const result = engine.sort(rawItems, plan);

  assert(result.evaluation.isContinuous, '修復後相同類別項目應達到連續狀態');
  assert(result.evaluation.iterationsUsed <= 3, '疊代次數必須小於或等於 3 次上限');

  const categories = result.items.map((i) => i.category);
  assert(
    (categories[0] === 'Electronics' && categories[1] === 'Electronics') ||
    (categories[0] === 'Books' && categories[1] === 'Books'),
    '相同類別項目已被完全收斂聚集'
  );
}

// ─────────────────────────────────────────────
// 測試 2b：修復迴圈必須真的能偵測到斷層並執行修復
// ─────────────────────────────────────────────
function testRepairLoopActuallyRuns(): void {
  console.log('\n============================================================');
  console.log(' 測試 2b：次要分群維度斷層之偵測與實際修復');
  console.log('============================================================');

  // 主鍵是 album，但同一位 artist 的曲目散落在不同專輯 → artist 被打散。
  // 只檢查 groupDimensions[0] 的偵測器永遠看不到這個斷層。
  const tracks = [
    { id: '1', artist: 'A', album: 'X', track: 1 },
    { id: '2', artist: 'B', album: 'Y', track: 1 },
    { id: '3', artist: 'A', album: 'Z', track: 1 },
  ];

  const plan = {
    groupDimensions: [
      { field: 'album', weight: 100, nullHandling: 'nulls_last' as const },
      { field: 'artist', weight: 90, nullHandling: 'nulls_last' as const },
    ],
    sortCriteria: [
      { field: 'track', direction: 'asc' as const, nullHandling: 'nulls_last' as const },
    ],
    fallbackStrategy: 'move_to_end' as const,
    maxRepairIterations: 3,
  };

  // 先確認「未修復」的排序確實存在 artist 斷層
  const evaluator = new Evaluator();
  const naive = [...tracks].sort((a, b) => a.album.localeCompare(b.album));
  const naiveRecords = naive.map((raw, originalIndex) => ({
    raw,
    originalIndex,
    getValue: (field: string) => (raw as Record<string, string | number>)[field] ?? null,
  }));
  const beforeGaps = evaluator.detectGaps(naiveRecords, plan);
  assert(beforeGaps.length > 0, '以 album 為主鍵時應偵測到 artist 分群斷層');
  assert(
    beforeGaps.some((g) => g.field === 'artist' && g.kind === 'grouping'),
    '斷層應指向被打散的 artist 欄位'
  );

  // 交給引擎：修復迴圈應提升 artist 權重並實際重排
  const result = new SortingEngine().sort(tracks, plan);
  assert(result.evaluation.iterationsUsed > 0, '修復迴圈必須實際執行過（而非恆為 0 次）');
  assert(result.evaluation.isContinuous, '修復後 artist 應完全聚集');

  const artists = result.items.map((t) => t.artist);
  assert(artists[0] === artists[1], `同一 artist 應相鄰，實際為 ${artists.join(',')}`);
}

// ─────────────────────────────────────────────
// 測試 2c：群內排序單調性檢查
// ─────────────────────────────────────────────
function testOrderingViolationDetected(): void {
  console.log('\n============================================================');
  console.log(' 測試 2c：群內排序單調性與空值位置檢查');
  console.log('============================================================');

  const plan = {
    groupDimensions: [
      { field: 'category', weight: 100, nullHandling: 'nulls_last' as const },
    ],
    sortCriteria: [
      { field: 'price', direction: 'asc' as const, nullHandling: 'nulls_last' as const },
    ],
    fallbackStrategy: 'move_to_end' as const,
    maxRepairIterations: 3,
  };

  // 手動組出一個「已排序但其實違規」的結果：群內價格不是遞增
  const broken = [
    { id: '1', category: 'A', price: 30 },
    { id: '2', category: 'A', price: 10 },
  ];
  const records = broken.map((raw, originalIndex) => ({
    raw,
    originalIndex,
    getValue: (field: string) => (raw as Record<string, string | number>)[field] ?? null,
  }));

  const gaps = new Evaluator().detectGaps(records, plan);
  assert(
    gaps.some((g) => g.kind === 'ordering' && g.field === 'price'),
    '群內價格未遞增應被判定為 ordering 斷層'
  );

  // 正常排序結果不得產生誤報
  const healthy = new SortingEngine().sort(broken, plan);
  assert(healthy.evaluation.gapCount === 0, '正確排序的結果不應出現任何斷層誤報');
}

// ─────────────────────────────────────────────
// 測試 2d：群組排列順序（first_appearance vs 字典序）
// ─────────────────────────────────────────────
function testGroupOrderStrategy(): void {
  console.log('\n============================================================');
  console.log(' 測試 2d：群組排列順序策略');
  console.log('============================================================');

  const items = [
    { id: '1', channel: 'Zebra', n: 1 },
    { id: '2', channel: 'Alpha', n: 2 },
    { id: '3', channel: 'Zebra', n: 3 },
    { id: '4', channel: 'Alpha', n: 4 },
  ];

  const basePlan = {
    sortCriteria: [{ field: 'n', direction: 'asc' as const, nullHandling: 'nulls_last' as const }],
    fallbackStrategy: 'move_to_end' as const,
    maxRepairIterations: 3,
  };

  const firstAppearance = new SortingEngine().sort(items, {
    ...basePlan,
    groupDimensions: [
      { field: 'channel', weight: 100, nullHandling: 'nulls_last' as const, groupOrder: 'first_appearance' as const },
    ],
  });
  assert(
    firstAppearance.items[0]?.channel === 'Zebra',
    'first_appearance 應保留原始群組出現順序（Zebra 在前）'
  );

  const lexical = new SortingEngine().sort(items, {
    ...basePlan,
    groupDimensions: [
      { field: 'channel', weight: 100, nullHandling: 'nulls_last' as const, groupOrder: 'lexical' as const },
    ],
  });
  assert(lexical.items[0]?.channel === 'Alpha', 'lexical 應改為字典序（Alpha 在前）');

  const countDesc = new SortingEngine().sort(
    [...items, { id: '5', channel: 'Alpha', n: 5 }],
    {
      ...basePlan,
      groupDimensions: [
        { field: 'channel', weight: 100, nullHandling: 'nulls_last' as const, groupOrder: 'count_desc' as const },
      ],
    }
  );
  assert(countDesc.items[0]?.channel === 'Alpha', 'count_desc 應把項目最多的群組排在前面');
}

// ─────────────────────────────────────────────
// 測試 2e：意圖文字不得污染欄位比對
// ─────────────────────────────────────────────
function testIntentDoesNotPolluteFields(): void {
  console.log('\n============================================================');
  console.log(' 測試 2e：意圖關鍵字不得讓不相關欄位入選');
  console.log('============================================================');

  const extractor = new Extractor();
  const fields = ['id', 'title', 'channel_title', 'view_count', 'duration_seconds'];

  const features = extractor.extractFromFields(fields, {
    text: '請把同一個 artist 的影片放在一起',
  });

  assert(
    features.groupDimensions.length === 1,
    `只有 channel_title 應被視為分群維度，實際 ${features.groupDimensions.length} 個`
  );
  assert(
    features.groupDimensions[0]?.field === 'channel_title',
    '分群維度應為 channel_title'
  );
  assert(
    !features.groupDimensions.some((d) => d.field === 'id' || d.field === 'title'),
    'id / title 不得因為意圖文字提到關鍵字而被誤選'
  );
}

// ─────────────────────────────────────────────
// 測試 2f：逐欄位的排序方向解析
// ─────────────────────────────────────────────
function testPerFieldSortDirection(): void {
  console.log('\n============================================================');
  console.log(' 測試 2f：中文需求的逐欄位排序方向');
  console.log('============================================================');

  const features = new Extractor().extractFromFields(
    ['title', 'view_count', 'duration_seconds'],
    { text: '觀看次數由高到低，時長由低到高' }
  );

  const viewCriterion = features.sortCriteria.find((c) => c.field === 'view_count');
  const durationCriterion = features.sortCriteria.find((c) => c.field === 'duration_seconds');

  assert(viewCriterion?.direction === 'desc', '觀看次數應解析為降冪');
  assert(durationCriterion?.direction === 'asc', '時長應解析為升冪');
}

// ─────────────────────────────────────────────
// 測試 3：通用領域擴展性測試 (Bug 報告管理系統)
// ─────────────────────────────────────────────
function testUniversalDomainBugReports(): void {
  console.log('\n============================================================');
  console.log(' 測試 3：通用領域擴展性測試 (Bug 報告管理)');
  console.log('============================================================');

  // 完全無關之資料集，測試核心引擎是否無需修補程式碼即可完美運作
  const bugReports = [
    { id: 'BUG-101', project: 'Alpha', module: 'Auth', priority: 2, title: 'Token timeout' },
    { id: 'BUG-102', project: 'Beta', module: 'Payment', priority: 1, title: 'Gateway error' },
    { id: 'BUG-103', project: 'Alpha', module: 'Auth', priority: 1, title: 'Login typo' },
    { id: 'BUG-104', project: 'Beta', module: 'UI', priority: 3, title: 'Color mismatch' },
  ];

  const result = cognitiveSort(bugReports, {
    text: '請將同專案的 Bug 報告放在一起，並按優先級排序',
    explicitGroupFields: ['project'],
    explicitSortFields: ['priority'],
  });

  assert(result.items.length === 4, '應回傳所有 4 筆 Bug 報告');
  assert(result.evaluation.isContinuous, '相同專案之 Bug 報告應處於連續索引區間');

  const projectOrder = result.items.map((b) => b.project);
  assert(projectOrder[0] === projectOrder[1], '同專案項目已被強制聚集');
}

// ─────────────────────────────────────────────
// 測試 4：空值 (Null/Undefined) 安全防護與降級
// ─────────────────────────────────────────────
function testNullSafetyAndFallback(): void {
  console.log('\n============================================================');
  console.log(' 測試 4：空值與缺漏屬性安全防護');
  console.log('============================================================');

  const itemsWithNulls = [
    { id: '1', album: 'Album A', track: 2 },
    { id: '2', album: null, track: 1 },
    { id: '3', album: 'Album A', track: 1 },
    { id: '4', album: undefined, track: 3 },
  ];

  const result = cognitiveSort(itemsWithNulls, {
    explicitGroupFields: ['album'],
    explicitSortFields: ['track'],
  });

  assert(result.items.length === 4, '無任何資料在排序過程中遺失');
  const albums = result.items.map((i) => i.album);

  // album 為 null/undefined 者應依預設策略放至陣列尾端
  assert(albums[0] === 'Album A' && albums[1] === 'Album A', '有效屬性項目排於前端');
  assert(albums[2] === null || albums[2] === undefined, '缺失值項目降級移至尾端');
}


// ─────────────────────────────────────────────
// 測試 2g：長度／大小的口語方向詞
// ─────────────────────────────────────────────
function testLengthDirectionMarkers(): void {
  console.log('');
  console.log('============================================================');
  console.log(' 測試 2g：「最長／最短」等口語方向詞');
  console.log('============================================================');

  const fields = ['title', 'view_count', 'duration_seconds'];

  const longest = new Extractor().extractFromFields(fields, { text: '把時間最長的排在最前面' });
  const longestCriterion = longest.sortCriteria.find((c) => c.field === 'duration_seconds');
  assert(longestCriterion?.direction === 'desc', '「時間最長」應解析為降冪');

  const shortest = new Extractor().extractFromFields(fields, { text: '時長最短的放前面' });
  const shortestCriterion = shortest.sortCriteria.find((c) => c.field === 'duration_seconds');
  assert(shortestCriterion?.direction === 'asc', '「時長最短」應解析為升冪');
}

// ─────────────────────────────────────────────
// 測試 2h：講了方向卻沒講欄位時，不得亂選欄位
// ─────────────────────────────────────────────
function testUnmatchedSortIntentSelectsNothing(): void {
  console.log('');
  console.log('============================================================');
  console.log(' 測試 2h：無法辨識的排序需求不得退化成「全選」');
  console.log('============================================================');

  const fields = ['position', 'title', 'channel_title', 'published_at', 'view_count'];

  // 「由新到舊」只講了方向，沒有任何欄位詞彙。舊版會把 position 等全部候選
  // 欄位選進來，讓 position 變成主鍵——群內順序等於原索引倒轉。
  const vague = new Extractor().extractFromFields(fields, {
    text: '同一個歌手的歌集中在一起，每組裡面由新到舊排列',
  });
  assert(
    vague.sortCriteria.length === 0,
    `辨識不出排序欄位時應回傳空集合，實際 ${JSON.stringify(vague.sortCriteria.map((c) => c.field))}`
  );

  // 明確講出「發布日期」就必須解析得出來。
  const explicit = new Extractor().extractFromFields(fields, {
    text: '同一個歌手的歌放一起，並依照發布日期由新到舊',
  });
  const dateCriterion = explicit.sortCriteria.find((c) => c.field === 'published_at');
  assert(dateCriterion?.direction === 'desc', '明確指名「發布日期由新到舊」應解析為 published_at:desc');
  assert(
    !explicit.sortCriteria.some((c) => c.field === 'position'),
    'position 不得混入排序條件'
  );

  // 完全沒有需求描述時（純欄位推導）仍要有降級行為，函式庫用法不受影響。
  const noIntent = new Extractor().extractFromFields(fields);
  assert(noIntent.sortCriteria.length > 0, '無意圖文字時仍應有降級排序條件');
}

// ─────────────────────────────────────────────
// 測試 N：滾動式學習所得的詞彙確實併入 Extractor
// ─────────────────────────────────────────────
function testLearnedVocabularyIsMerged(): void {
  console.log('\n============================================================');
  console.log(' 測試：滾動式學習詞彙的合併');
  console.log('============================================================');

  const fields = [
    'title', 'channel_title', 'published_at', 'duration_seconds',
    'view_count', 'like_count', 'added_at',
  ];
  const extractor = new Extractor();

  // 基礎詞彙不可因為合併而失效——學習只能加，不能改變既有解析結果。
  const baseline = extractor.extractFromFields(fields, { text: '依觀看次數由高到低' });
  assert(
    baseline.sortCriteria.some((c) => c.field === 'view_count' && c.direction === 'desc'),
    '合併學習詞彙後，基礎詞彙「觀看次數」仍須解析為 view_count:desc'
  );

  const sortEntries = Object.entries(LEARNED_VOCABULARY.sort);
  if (sortEntries.length === 0) {
    assert(true, '尚無學來的排序詞彙，略過（合併機制本身已由上一項涵蓋）');
    return;
  }

  // 每一個經人工核可的同義詞，都必須真的能挑得出欄位；挑不到就代表
  // learned-vocabulary.ts 沒有生效，滾動式學習等於空轉。
  for (const [canonical, synonyms] of sortEntries) {
    for (const synonym of synonyms) {
      const features = extractor.extractFromFields(fields, { text: `依${synonym}排序` });
      assert(
        features.sortCriteria.length > 0,
        `學來的「${synonym}」應能挑出排序欄位（正規詞 ${canonical}）`
      );
    }
  }
}

// ─────────────────────────────────────────────
// 測試總進入點
// ─────────────────────────────────────────────
function main(): void {
  testMusicTrackSorting();
  testEvaluatorRepairLoop();
  testRepairLoopActuallyRuns();
  testOrderingViolationDetected();
  testGroupOrderStrategy();
  testIntentDoesNotPolluteFields();
  testPerFieldSortDirection();
  testLengthDirectionMarkers();
  testUnmatchedSortIntentSelectsNothing();
  testUniversalDomainBugReports();
  testNullSafetyAndFallback();
  testLearnedVocabularyIsMerged();

  console.log('\n============================================================');
  console.log(` 測試結果總計: ${passed} 通過, ${failed} 失敗`);
  console.log('============================================================');

  if (failed > 0) {
    process.exit(1);
  }
}

main();
