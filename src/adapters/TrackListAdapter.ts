/**
 * TrackListAdapter.ts — 音樂曲目資料適配器範例
 * 
 * 示範將音樂播放清單項目（如 YouTube 音樂曲目）輸入認知排序引擎，
 * 達成「同歌手/專輯高階實體歸類 $\rightarrow$ 曲目軌號/標題純量排序」。
 * 遵循 Early Return 範式、嚴格型別安全與台灣繁體中文專業術語。
 */

import type { CognitivePlan } from '../core/types/schema.js';
import { SortingEngine, type SortingResult } from '../core/engine/SortingEngine.js';

/** 音樂曲目數據介面 */
export interface MusicTrack {
  readonly id: string;
  readonly title: string;
  readonly artist: string;
  readonly album: string;
  readonly trackNumber: number;
  readonly durationSeconds: number;
}

/**
 * 音樂曲目資料適配器
 */
export class TrackListAdapter {
  private readonly engine = new SortingEngine();

  /**
   * 執行曲目清單認知排序
   * 預設規則：先按專輯與歌手高階歸類，群內按軌號升冪排列
   */
  public sortTracks(
    tracks: readonly MusicTrack[],
    customPlan?: CognitivePlan
  ): SortingResult<MusicTrack> {
    if (tracks.length <= 1) {
      return {
        items: [...tracks],
        plan: customPlan ?? this.buildDefaultTrackPlan(),
        evaluation: {
          isContinuous: true,
          gapCount: 0,
          gaps: [],
          repairedPlan: customPlan ?? this.buildDefaultTrackPlan(),
          iterationsUsed: 0,
        },
      };
    }

    const plan = customPlan ?? this.buildDefaultTrackPlan();
    return this.engine.sort(tracks, plan);
  }

  /**
   * 建立預設曲目認知計畫
   */
  private buildDefaultTrackPlan(): CognitivePlan {
    return {
      groupDimensions: [
        {
          field: 'artist',
          weight: 100,
          nullHandling: 'nulls_last',
        },
        {
          field: 'album',
          weight: 90,
          nullHandling: 'nulls_last',
        },
      ],
      sortCriteria: [
        {
          field: 'trackNumber',
          direction: 'asc',
          nullHandling: 'nulls_last',
        },
        {
          field: 'title',
          direction: 'asc',
          nullHandling: 'nulls_last',
        },
      ],
      fallbackStrategy: 'move_to_end',
      maxRepairIterations: 3,
    };
  }
}
