/**
 * 金额口径：字符串 → 整数分 → 展示。全程整数运算，不经过浮点。
 * 支持千分位、边界上的货币前缀/后缀、万元；最多两位小数；拒绝负数与科学计数法。
 * 币种标识只在字符串首尾识别，中间的「1USD2」这类写法按非法金额拒绝。
 */
import { CURRENCY_LABELS } from './types';
import type { Currency } from './types';

export type ParseAmountResult =
  | { ok: true; cents: number; currency?: Currency }
  | { ok: false; message: string };

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

const CURRENCY_SYMBOLS: Record<Currency, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
};

/** 仅匹配字符串开头的币种标识。 */
const PREFIXES: { re: RegExp; currency: Currency }[] = [
  { re: /^人民币/, currency: 'CNY' },
  { re: /^美元/, currency: 'USD' },
  { re: /^欧元/, currency: 'EUR' },
  { re: /^(?:RMB|CNY)/i, currency: 'CNY' },
  { re: /^USD/i, currency: 'USD' },
  { re: /^EUR/i, currency: 'EUR' },
  { re: /^[¥￥]/, currency: 'CNY' },
  { re: /^\$/, currency: 'USD' },
  { re: /^€/, currency: 'EUR' },
];

/** 仅匹配字符串结尾的币种标识；须先于「元」判断，避免把「美元」拆成「美」+「元」。 */
const SUFFIX_CURRENCIES: { re: RegExp; currency: Currency }[] = [
  { re: /人民币$/, currency: 'CNY' },
  { re: /美元$/, currency: 'USD' },
  { re: /欧元$/, currency: 'EUR' },
  { re: /(?:RMB|CNY)$/i, currency: 'CNY' },
  { re: /USD$/i, currency: 'USD' },
  { re: /EUR$/i, currency: 'EUR' },
];

/** 仅匹配字符串结尾的计量单位。 */
const SUFFIX_UNITS: { re: RegExp; multiplier: bigint }[] = [
  { re: /万元$/, multiplier: 10000n },
  { re: /万$/, multiplier: 10000n },
  { re: /元$/, multiplier: 1n },
];

/** 纯数字，或首段 1–3 位、其后每段恰好 3 位的千分位写法（逗号或空格分隔）。 */
const PLAIN_NUMBER = /^\d+(?:\.\d{1,2})?$/;
const GROUPED_NUMBER = /^\d{1,3}(?:[,，\s]\d{3})+(?:\.\d{1,2})?$/;

const INVALID_MESSAGE = '金额应为数字，最多两位小数（元），千分位只能每三位分隔，不能为负';

export function parseAmountToCents(input: string): ParseAmountResult {
  const raw = input.trim();
  if (!raw) return { ok: false, message: '请输入金额' };

  let body = raw;
  let currency: Currency | undefined;

  const prefix = PREFIXES.find((entry) => entry.re.test(body));
  if (prefix) {
    currency = prefix.currency;
    body = body.replace(prefix.re, '').trimStart();
  }

  let multiplier: bigint | undefined;
  // 结尾允许叠加「币种」与「万元/万/元」，最多两轮；不循环剥离以免「1万万」被当成 1 亿元。
  for (let step = 0; step < 2; step += 1) {
    const trimmed = body.trimEnd();
    const suffixCurrency = SUFFIX_CURRENCIES.find((entry) => entry.re.test(trimmed));
    if (suffixCurrency) {
      if (currency && currency !== suffixCurrency.currency) {
        return { ok: false, message: '金额同时标注了两种币种，请只保留一种' };
      }
      currency = suffixCurrency.currency;
      body = trimmed.replace(suffixCurrency.re, '').trimEnd();
      continue;
    }
    const suffixUnit = SUFFIX_UNITS.find((entry) => entry.re.test(trimmed));
    if (suffixUnit) {
      if (multiplier !== undefined) return { ok: false, message: INVALID_MESSAGE };
      multiplier = suffixUnit.multiplier;
      body = trimmed.replace(suffixUnit.re, '').trimEnd();
      continue;
    }
    break;
  }

  if (!body) return { ok: false, message: '请输入金额数字' };
  if (!PLAIN_NUMBER.test(body) && !GROUPED_NUMBER.test(body)) {
    return { ok: false, message: INVALID_MESSAGE };
  }

  const digits = body.replace(/[,，\s]/g, '');
  const [integerPart, fractionPart = ''] = digits.split('.');
  const cents = BigInt(integerPart) * 100n + BigInt(`${fractionPart}00`.slice(0, 2));
  const total = cents * (multiplier ?? 1n);
  if (total > MAX_SAFE) {
    return { ok: false, message: '金额超出可精确表示的范围（约 90 万亿元）' };
  }
  return { ok: true, cents: Number(total), currency };
}

/**
 * 金额自带币种与本组币种冲突时的统一提示；未标注或一致时返回 null。
 * 只提示，不自动换算或改币种。
 */
export function currencyMismatchMessage(amountCurrency: Currency | undefined,
                                        groupCurrency: Currency): string | null {
  if (!amountCurrency || amountCurrency === groupCurrency) return null;
  return `金额明确标注为${CURRENCY_LABELS[amountCurrency]}（${amountCurrency}），`
    + `与本组${CURRENCY_LABELS[groupCurrency]}（${groupCurrency}）不一致；`
    + '不会自动改币种，请改用本组币种金额，或在分组设置中调整币种';
}

function groupDigits(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 123456 → "1,234.56"；负数保留前置减号。 */
export function formatCents(cents: number): string {
  if (!Number.isSafeInteger(cents)) throw new Error('金额必须为安全整数分');
  const negative = cents < 0;
  const absolute = Math.abs(cents);
  const yuan = Math.floor(absolute / 100);
  const fen = absolute % 100;
  return `${negative ? '-' : ''}${groupDigits(String(yuan))}.${String(fen).padStart(2, '0')}`;
}

export function formatMoney(cents: number, currency: Currency): string {
  const text = formatCents(cents);
  const symbol = CURRENCY_SYMBOLS[currency];
  return text.startsWith('-') ? `-${symbol}${text.slice(1)}` : `${symbol}${text}`;
}

/** 抵扣项按减项计；其余类别加总。 */
export function signedCents(category: string, amountCents: number): number {
  return category === 'deduction' ? -amountCents : amountCents;
}

export function sumCents(values: number[]): number {
  let total = 0;
  for (const value of values) {
    if (!Number.isSafeInteger(value)) throw new Error('金额必须为安全整数分');
    total += value;
    if (!Number.isSafeInteger(total)) throw new Error('合计超出可精确表示的范围');
  }
  return total;
}

/**
 * 瀑布图坐标轴用的紧凑写法：128000000 分 → "128.00万"。
 * 只用于显示刻度；刻度值由绘图代码取整到分后再传入，业务金额始终是整数分。
 */
export function formatCompactCents(cents: number): string {
  const rounded = Math.round(cents);
  const negative = rounded < 0;
  const absolute = Math.abs(rounded);
  const sign = negative ? '-' : '';
  // 1 万元 = 1,000,000 分；除以 10,000 后仍是「分的百分之一」，可直接用 formatCents 保留两位。
  if (absolute >= 1_000_000) {
    return `${sign}${formatCents(Math.round(absolute / 10_000))}万`;
  }
  return formatCents(rounded);
}
