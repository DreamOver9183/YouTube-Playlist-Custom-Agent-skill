/**
 * cli.ts — 認知排序引擎與 Python 工具鏈之間的橋接
 *
 * 讀取 `yt_tool fetch` 產出的 current.json，以認知排序引擎計算新順序，
 * 寫出格式完全相同的 new.json，再交由 `yt_tool diff` 計算最小變更集。
 *
 * 用法：
 *   node dist/cli.js --input data/current.json --output data/new.json \
 *     --intent "把同一個頻道的影片放在一起，觀看次數由高到低"
 *   node dist/cli.js -i data/current.json -o data/new.json \
 *     --group-by channel_title --sort-by view_count:desc
 *
 * stdout 只輸出一行 JSON 供 Agent 解析（與 Python 工具一致）。
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import { Extractor, type NaturalLanguageIntent } from './core/cognitive/Extractor.js';
import { Planner } from './core/cognitive/Planner.js';
import { SortingEngine } from './core/engine/SortingEngine.js';
import type { CognitivePlan, SortDirection } from './core/types/schema.js';

/** 播放清單項目（保持 fetch 輸出的原始結構，不做任何欄位刪改） */
type PlaylistRecord = Record<string, unknown>;

interface CliOptions {
  readonly input: string;
  readonly output: string;
  readonly intentText: string;
  readonly groupBy: readonly string[];
  readonly sortBy: readonly string[];
}

function emit(payload: Record<string, unknown>): void {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function failWith(code: string, message: string): never {
  emit({ status: 'error', code, message });
  process.exit(1);
}

function parseArgs(argv: readonly string[]): CliOptions {
  let input = '';
  let output = '';
  let intentText = '';
  const groupBy: string[] = [];
  const sortBy: string[] = [];

  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const value = argv[i + 1] ?? '';

    switch (flag) {
      case '--input':
      case '-i':
        input = value;
        i++;
        break;
      case '--output':
      case '-o':
        output = value;
        i++;
        break;
      case '--intent':
        intentText = value;
        i++;
        break;
      case '--group-by':
        groupBy.push(...value.split(',').map((s) => s.trim()).filter(Boolean));
        i++;
        break;
      case '--sort-by':
        sortBy.push(...value.split(',').map((s) => s.trim()).filter(Boolean));
        i++;
        break;
      default:
        if (flag && flag.startsWith('-')) {
          failWith('UNKNOWN_FLAG', `不認得的參數：${flag}`);
        }
    }
  }

  if (!input || !output) {
    failWith('MISSING_ARGUMENT', '必須提供 --input <current.json> 與 --output <new.json>。');
  }

  return { input, output, intentText, groupBy, sortBy };
}

/** 解析 `field:desc` 形式的排序指定 */
function parseSortSpec(spec: string): { field: string; direction: SortDirection } {
  const [field = '', rawDirection = 'asc'] = spec.split(':');
  const direction: SortDirection = rawDirection.trim().toLowerCase() === 'desc' ? 'desc' : 'asc';
  return { field: field.trim(), direction };
}

function buildPlan(records: readonly PlaylistRecord[], options: CliOptions): CognitivePlan {
  const planner = new Planner();

  // 顯式指定優先；否則交由 Extractor 從欄位與意圖文字推導。
  if (options.groupBy.length > 0 || options.sortBy.length > 0) {
    return planner.createPlan(
      { groupDimensions: [], sortCriteria: [] },
      {
        groupDimensions: options.groupBy.map((field, index) => ({
          field,
          weight: 100 - index * 10,
          nullHandling: 'nulls_last' as const,
          groupOrder: 'first_appearance' as const,
        })),
        sortCriteria: options.sortBy.map((spec) => {
          const { field, direction } = parseSortSpec(spec);
          return { field, direction, nullHandling: 'nulls_last' as const };
        }),
      }
    );
  }

  const fieldUnion = new Set<string>();
  for (const record of records.slice(0, 20)) {
    for (const field of Object.keys(record)) {
      fieldUnion.add(field);
    }
  }

  const intent: NaturalLanguageIntent = { text: options.intentText };
  const features = new Extractor().extractFromFields([...fieldUnion], intent);

  // 重排既有清單時，群組一律依首次出現順序排列：字典序會把所有群組洗牌，
  // 需要移動的項目數（也就是配額）會高出好幾倍。
  return planner.createPlan({
    groupDimensions: features.groupDimensions.map((dim) => ({
      ...dim,
      groupOrder: 'first_appearance' as const,
    })),
    sortCriteria: features.sortCriteria,
  });
}

function main(): void {
  const options = parseArgs(process.argv.slice(2));

  let records: PlaylistRecord[];
  try {
    const raw = readFileSync(options.input, 'utf-8');
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      failWith('PARSE_ERROR', `${options.input} 必須是 fetch 產出的 JSON 陣列。`);
    }
    records = parsed as PlaylistRecord[];
  } catch (error) {
    failWith('PARSE_ERROR', `無法讀取 ${options.input}：${(error as Error).message}`);
  }

  if (records.length === 0) {
    failWith('EMPTY_PLAYLIST', '播放清單是空的。');
  }

  const plan = buildPlan(records, options);
  if (plan.groupDimensions.length === 0 && plan.sortCriteria.length === 0) {
    failWith(
      'NO_CRITERIA',
      '無法從需求推導出任何分群或排序條件。請改用 --group-by / --sort-by 明確指定欄位。'
    );
  }

  // 不可移動的項目（私人／已刪除影片）必須留在原位，否則 Python 端算出的
  // 寫回位置會全部偏移；這裡先抽掉，排序後再插回原索引。
  const pinned: Array<{ index: number; record: PlaylistRecord }> = [];
  const movable: PlaylistRecord[] = [];
  records.forEach((record, index) => {
    if (record['is_available'] === false) {
      pinned.push({ index, record });
    } else {
      movable.push(record);
    }
  });

  const result = new SortingEngine().sort(movable, plan);

  const merged: PlaylistRecord[] = new Array<PlaylistRecord>(records.length);
  for (const { index, record } of pinned) {
    merged[index] = record;
  }
  let cursor = 0;
  for (let slot = 0; slot < merged.length; slot++) {
    if (merged[slot] === undefined) {
      merged[slot] = result.items[cursor] as PlaylistRecord;
      cursor++;
    }
  }

  mkdirSync(dirname(options.output), { recursive: true });
  writeFileSync(options.output, `${JSON.stringify(merged, null, 2)}\n`, 'utf-8');

  emit({
    status: 'success',
    item_count: merged.length,
    pinned_count: pinned.length,
    group_dimensions: result.plan.groupDimensions.map((d) => d.field),
    sort_criteria: result.plan.sortCriteria.map((c) => `${c.field}:${c.direction}`),
    is_continuous: result.evaluation.isContinuous,
    gap_count: result.evaluation.gapCount,
    repair_iterations: result.evaluation.iterationsUsed,
    output_file: options.output,
    next_step: `python -m scripts.yt_tool diff ${options.input} ${options.output} --out data/changes.json`,
  });
}

main();
