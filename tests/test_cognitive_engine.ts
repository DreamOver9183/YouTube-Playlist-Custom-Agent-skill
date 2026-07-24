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

import { cognitiveSort } from '../dist/index.js';
import { TrackListAdapter, type MusicTrack } from '../dist/adapters/TrackListAdapter.js';
import { SortingEngine } from '../dist/core/engine/SortingEngine.js';

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

  // 模擬初期分群權重不足導致斷層之計畫
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
// 測試總進入點
// ─────────────────────────────────────────────
function main(): void {
  testMusicTrackSorting();
  testEvaluatorRepairLoop();
  testUniversalDomainBugReports();
  testNullSafetyAndFallback();

  console.log('\n============================================================');
  console.log(` 測試結果總計: ${passed} 通過, ${failed} 失敗`);
  console.log('============================================================');

  if (failed > 0) {
    process.exit(1);
  }
}

main();
