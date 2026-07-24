/**
 * ECommerceAdapter.ts — 電商商品資料適配器範例
 * 
 * 示範將電商商品資料清單輸入認知排序引擎，
 * 達成「商品分類與品牌高階實體歸類 $\rightarrow$ 價格/評分純量排序」。
 * 遵循 Early Return 範式、嚴格型別安全與台灣繁體中文專業術語。
 */

import type { CognitivePlan } from '../core/types/schema.js';
import { SortingEngine, type SortingResult } from '../core/engine/SortingEngine.js';

/** 電商商品數據介面 */
export interface ProductItem {
  readonly id: string;
  readonly name: string;
  readonly category: string;
  readonly brand: string;
  readonly price: number;
  readonly rating: number;
}

/**
 * 電商商品資料適配器
 */
export class ECommerceAdapter {
  private readonly engine = new SortingEngine();

  /**
   * 執行商品清單認知排序
   * 預設規則：按商品分類與品牌高階歸類，群內按價格降冪或評分排序
   */
  public sortProducts(
    products: readonly ProductItem[],
    customPlan?: CognitivePlan
  ): SortingResult<ProductItem> {
    if (products.length <= 1) {
      return {
        items: [...products],
        plan: customPlan ?? this.buildDefaultProductPlan(),
        evaluation: {
          isContinuous: true,
          gapCount: 0,
          gaps: [],
          repairedPlan: customPlan ?? this.buildDefaultProductPlan(),
          iterationsUsed: 0,
        },
      };
    }

    const plan = customPlan ?? this.buildDefaultProductPlan();
    return this.engine.sort(products, plan);
  }

  /**
   * 建立預設商品認知計畫
   */
  private buildDefaultProductPlan(): CognitivePlan {
    return {
      groupDimensions: [
        {
          field: 'category',
          weight: 100,
          nullHandling: 'nulls_last',
        },
        {
          field: 'brand',
          weight: 80,
          nullHandling: 'nulls_last',
        },
      ],
      sortCriteria: [
        {
          field: 'rating',
          direction: 'desc',
          nullHandling: 'nulls_last',
        },
        {
          field: 'price',
          direction: 'asc',
          nullHandling: 'nulls_last',
        },
      ],
      fallbackStrategy: 'move_to_end',
      maxRepairIterations: 3,
    };
  }
}
