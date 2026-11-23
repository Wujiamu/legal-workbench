import { describe, expect, it } from 'vitest';
import {
  currencyMismatchMessage, formatCents, formatCompactCents, formatMoney, parseAmountToCents,
  signedCents, sumCents,
} from './money';

describe('金额字符串 → 整数分', () => {
  it('接受千分位、货币前缀后缀与万元', () => {
    expect(parseAmountToCents('1,280,000')).toEqual({ ok: true, cents: 128000000 });
    expect(parseAmountToCents('¥1,234.56')).toEqual({ ok: true, cents: 123456, currency: 'CNY' });
    expect(parseAmountToCents('人民币 1,280,000 元'))
      .toEqual({ ok: true, cents: 128000000, currency: 'CNY' });
    expect(parseAmountToCents('1,234.56元')).toEqual({ ok: true, cents: 123456 });
    expect(parseAmountToCents('1 234.56')).toEqual({ ok: true, cents: 123456 });
    expect(parseAmountToCents('1.5万')).toEqual({ ok: true, cents: 1500000 });
    expect(parseAmountToCents('1.5万元')).toEqual({ ok: true, cents: 1500000 });
    expect(parseAmountToCents('0.1')).toEqual({ ok: true, cents: 10 });
  });

  it('货币标识只在首尾识别，并回传币种', () => {
    expect(parseAmountToCents('美元12,345.67元'))
      .toEqual({ ok: true, cents: 1234567, currency: 'USD' });
    expect(parseAmountToCents('12,345.67 USD'))
      .toEqual({ ok: true, cents: 1234567, currency: 'USD' });
    expect(parseAmountToCents('€1.00')).toEqual({ ok: true, cents: 100, currency: 'EUR' });
    // 中间出现币种标识不是合法金额，不能把标识当分隔符删掉后拼成数字
    expect(parseAmountToCents('1USD2').ok).toBe(false);
    expect(parseAmountToCents('12元34').ok).toBe(false);
    expect(parseAmountToCents('1人民币2').ok).toBe(false);
    expect(parseAmountToCents('人民币美元1').ok).toBe(false);
  });

  it('拒绝位置错误的千分位分隔符', () => {
    expect(parseAmountToCents('1,2').ok).toBe(false);
    expect(parseAmountToCents('1,23').ok).toBe(false);
    expect(parseAmountToCents('1,2345').ok).toBe(false);
    expect(parseAmountToCents('1234,567').ok).toBe(false);
    expect(parseAmountToCents('1,,234').ok).toBe(false);
    expect(parseAmountToCents(',123').ok).toBe(false);
    expect(parseAmountToCents('1,234,').ok).toBe(false);
    expect(parseAmountToCents('1 2').ok).toBe(false);
    // 正常千分位仍然接受
    expect(parseAmountToCents('1,234,567.89')).toEqual({ ok: true, cents: 123456789 });
  });

  it('0.1 + 0.2 精确为 0.30 元', () => {
    const first = parseAmountToCents('0.1');
    const second = parseAmountToCents('0.2');
    if (!first.ok || !second.ok) throw new Error('解析失败');
    expect(sumCents([first.cents, second.cents])).toBe(30);
    expect(formatCents(30)).toBe('0.30');
  });

  it('亿元级高精度字符串逐分保留', () => {
    expect(parseAmountToCents('123,456,789,012.34')).toEqual({ ok: true, cents: 12345678901234 });
    expect(formatCents(12345678901234)).toBe('123,456,789,012.34');
    expect(sumCents([12345678901234, 1])).toBe(12345678901235);
  });

  it('拒绝负数、超过两位小数、非数字与溢出', () => {
    expect(parseAmountToCents('-5').ok).toBe(false);
    expect(parseAmountToCents('12.345').ok).toBe(false);
    expect(parseAmountToCents('abc').ok).toBe(false);
    expect(parseAmountToCents('1e5').ok).toBe(false);
    expect(parseAmountToCents('1万万').ok).toBe(false);
    expect(parseAmountToCents('').ok).toBe(false);
    expect(parseAmountToCents('¥').ok).toBe(false);
    expect(parseAmountToCents('99999999999999999').ok).toBe(false);
  });

  it('拒绝非安全整数分的求和', () => {
    expect(() => sumCents([Number.MAX_SAFE_INTEGER, 1])).toThrow();
    expect(() => formatCents(1.5)).toThrow();
  });
});

describe('币种冲突提示', () => {
  it('未标注或一致时无提示，不一致时说明不会自动改币种', () => {
    expect(currencyMismatchMessage(undefined, 'CNY')).toBeNull();
    expect(currencyMismatchMessage('CNY', 'CNY')).toBeNull();
    const message = currencyMismatchMessage('USD', 'CNY')!;
    expect(message).toContain('美元');
    expect(message).toContain('人民币');
    expect(message).toContain('不会自动改币种');
  });
});

describe('展示与方向', () => {
  it('按币种格式化，负数符号在货币符号前', () => {
    expect(formatMoney(123456, 'CNY')).toBe('¥1,234.56');
    expect(formatMoney(123456, 'USD')).toBe('$1,234.56');
    expect(formatMoney(-30000000, 'CNY')).toBe('-¥300,000.00');
  });

  it('抵扣为减项，其余为加项', () => {
    expect(signedCents('deduction', 30000000)).toBe(-30000000);
    expect(signedCents('principal', 30000000)).toBe(30000000);
    expect(signedCents('other', 0)).toBe(0);
  });

  it('坐标轴紧凑写法', () => {
    expect(formatCompactCents(140240000)).toBe('140.24万');
    expect(formatCompactCents(123456)).toBe('1,234.56');
    expect(formatCompactCents(-5000000)).toBe('-5.00万');
  });
});
