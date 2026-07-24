/**
 * entity.ts — 通用型資料項目與屬性載體型態定義
 * 
 * 提供純型別定義與屬性提取器介面，堅持嚴格泛型約束，全數消除 `any` 型別。
 * 遵循台灣繁體中文專業術語規範。
 */

/** 基礎屬性純量型態 */
export type AttributePrimitive = string | number | boolean | Date;

/** 可為 Null 或 Undefined 的安全屬性值型態 */
export type AttributeValue = AttributePrimitive | null | undefined;

/**
 * 屬性擷取函式型態
 * 用於從泛型資料項目中取得指定欄位之屬性值
 */
export type AttributeExtractor<T> = (item: T, fieldName: string) => AttributeValue;

/**
 * 泛型實體封裝介面
 */
export interface EntityRecord<T> {
  /** 原始數據資料項目 */
  readonly raw: T;
  /** 項目於原始陣列中之索引位置 */
  readonly originalIndex: number;
  /** 取得指定欄位屬性值 */
  getValue(fieldName: string): AttributeValue;
}

/**
 * 預設屬性提取器
 * 以反射方式自動存取物件屬性，支援屬性不存在與空值防護
 */
export function defaultAttributeExtractor<T extends object>(
  item: T,
  fieldName: string
): AttributeValue {
  if (item === null) {
    return undefined;
  }
  if (typeof item !== 'object') {
    return undefined;
  }

  const record = item as Record<string, unknown>;
  const val = record[fieldName];

  if (val === undefined || val === null) {
    return val;
  }
  if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') {
    return val;
  }
  if (val instanceof Date) {
    return val;
  }

  // 非純量屬性轉為字串表示
  return String(val);
}
