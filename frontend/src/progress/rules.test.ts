/**
 * 规则配置口径测试。
 */
import { describe, expect, it } from 'vitest';
import { defaultRules } from './rules';

describe('首版规则口径', () => {
  it('默认片段不含结案归档——归档是案件级操作，不是进度节点（用户反馈第四轮）', () => {
    const titles = defaultRules.fragments['litigation-default'].roots.map((root) => root.title);
    expect(titles).toEqual(['诉前准备', '一审']);
    const stageTitles = defaultRules.fragments['litigation-default'].roots
      .flatMap((root) => root.children ?? []).map((child) => child.title);
    expect(stageTitles).toEqual(['立案', '庭前程序', '庭审', '裁判']);
  });

  it('片段草稿齐全且带版本', () => {
    for (const key of ['second-instance', 'remand', 'retrial', 'enforcement']) {
      expect(defaultRules.fragments[key].version).toBe(1);
      expect(defaultRules.fragments[key].roots[0].kind).toBe('program');
    }
  });
});