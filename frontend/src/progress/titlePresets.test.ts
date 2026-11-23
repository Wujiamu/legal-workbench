/**
 * 节点标题预设口径：新建工作/事件时标题给固定选项＋其他；
 * 选其他才填自定义标题。预设是配置，不改代码即可扩展（K2 同思路）。
 */
import { describe, expect, it } from 'vitest';
import { defaultRules, titlePresets } from './rules';

describe('标题预设', () => {
  it('工作与事件有预设，程序与阶段没有', () => {
    expect(titlePresets(defaultRules, 'work').length).toBeGreaterThan(3);
    expect(titlePresets(defaultRules, 'event').length).toBeGreaterThan(2);
    expect(titlePresets(defaultRules, 'program')).toEqual([]);
    expect(titlePresets(defaultRules, 'stage')).toEqual([]);
  });

  it('注入新种类预设不改代码即生效', () => {
    const rules = {
      ...defaultRules,
      kinds: {
        ...defaultRules.kinds,
        preservation: { label: '保全', states: [{ value: 'pending', label: '未完成' }], default_state: 'pending' },
      },
      title_presets: { ...defaultRules.title_presets, preservation: ['查封清单'] },
    };
    expect(titlePresets(rules, 'preservation')).toEqual(['查封清单']);
  });
});