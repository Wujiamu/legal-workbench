/**
 * 节点编辑对话框：<dialog> + 原生表单；中文输入法组词不提交；
 * 冲突时保留草稿并提示，成功后关闭（总体计划 6.4）。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { NodeEditorDialog } from './NodeEditorDialog';
import { defaultRules } from './rules';
import type { CommandResult, ProgressNode } from './types';

function node(overrides: Partial<ProgressNode> = {}): ProgressNode {
  return {
    id: 'n-1', case_id: 'case-1', parent_id: null, position: 0,
    kind: 'stage', state: 'not_started', title: '庭前程序', note: null,
    date_start: null, date_end: null, template_ref: null,
    created_by: 'human', updated_by: 'human', request_key: null,
    removed_at: null, revision: 0,
    created_at: '2026-11-16T00:00:00Z', updated_at: '2026-11-16T00:00:00Z',
    ...overrides,
  };
}

const okResult: CommandResult = {
  ok: true,
  snapshot: { case_id: 'case-1', seq: 1, nodes: [], relations: [], links: [], pending_changes: [], changes: [] },
};

describe('NodeEditorDialog', () => {
  it('编辑模式预填节点值，状态选项来自规则', () => {
    render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={() => {}} onSubmit={() => okResult} />);
    expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('庭前程序');
    const state = screen.getByLabelText('状态') as HTMLSelectElement;
    expect([...state.options].map((option) => option.textContent))
      .toEqual(['未开始', '进行中', '已完成', '已跳过']);
  });

  it('提交发出 update_node 命令并带期望修订号，成功后关闭', async () => {
    const onSubmit = vi.fn(() => okResult);
    const onClose = vi.fn();
    render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={onClose} onSubmit={onSubmit} />);
    await userEvent.clear(screen.getByLabelText('标题'));
    await userEvent.type(screen.getByLabelText('标题'), '庭前程序（二次）');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      type: 'update_node', node_id: 'n-1', expected_revision: 0,
      changes: expect.objectContaining({ title: '庭前程序（二次）' }),
    }));
    expect(onClose).toHaveBeenCalled();
  });

  it('中文输入法组词中的回车不提交，完成输入后回车提交', () => {
    const onSubmit = vi.fn(() => okResult);
    render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={() => {}} onSubmit={onSubmit} />);
    const title = screen.getByLabelText('标题');
    fireEvent.compositionStart(title);
    fireEvent.change(title, { target: { value: 'tingshen' } });
    fireEvent.keyDown(title, { key: 'Enter', code: 'Enter', keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.compositionEnd(title);
    fireEvent.change(title, { target: { value: '庭审' } });
    fireEvent.keyDown(title, { key: 'Enter', code: 'Enter', keyCode: 13 });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('冲突时对话框不关闭、草稿保留，并显示冲突提示', async () => {
    const onSubmit = vi.fn((): CommandResult => ({ ok: false, code: 'conflict', message: '节点已被修改，请刷新后重试' }));
    const onClose = vi.fn();
    render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={onClose} onSubmit={onSubmit} />);
    await userEvent.clear(screen.getByLabelText('标题'));
    await userEvent.type(screen.getByLabelText('标题'), '我的草稿');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(onClose).not.toHaveBeenCalled();
    expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('我的草稿');
    expect(screen.getByRole('alert').textContent).toContain('节点已被修改');
  });

  it('冲突后点击重新加载才用服务端值替换草稿', async () => {
    const onSubmit = vi.fn((): CommandResult => ({ ok: false, code: 'conflict', message: '冲突' }));
    const view = render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={() => {}} onSubmit={onSubmit} />);
    await userEvent.clear(screen.getByLabelText('标题'));
    await userEvent.type(screen.getByLabelText('标题'), '我的草稿');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    view.rerender(<NodeEditorDialog mode="edit" node={node({ title: '服务端新值', revision: 1 })}
      rules={defaultRules} onClose={() => {}} onSubmit={onSubmit} />);
    expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('我的草稿'); // 不自动覆盖
    await userEvent.click(screen.getByRole('button', { name: '重新加载节点' }));
    expect((screen.getByLabelText('标题') as HTMLInputElement).value).toBe('服务端新值');
  });

  it('新建模式按固定种类提交 add_node（预设种类选其他后自定义）', async () => {
    const onSubmit = vi.fn(() => okResult);
    render(<NodeEditorDialog mode="add" kind="work" parentId="p-1" rules={defaultRules}
      onClose={() => {}} onSubmit={onSubmit} />);
    await userEvent.selectOptions(screen.getByLabelText('标题'), '__other__');
    await userEvent.type(screen.getByLabelText('自定义标题'), '核对证据清单');
    await userEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      type: 'add_node', parent_id: 'p-1', kind: 'work', title: '核对证据清单',
    }));
  });
});
describe('NodeEditorDialog · 标题预设', () => {
  it('新建工作：标题是下拉，含预设与其他；默认不预填自定义框', () => {
    render(<NodeEditorDialog mode="add" kind="work" parentId="p-1" rules={defaultRules}
      onClose={() => {}} onSubmit={() => okResult} />);
    const select = screen.getByLabelText('标题') as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    const labels = [...select.options].map((option) => option.textContent);
    expect(labels).toContain('证据整理');
    expect(labels[labels.length - 1]).toBe('其他…');
    expect(screen.queryByLabelText('自定义标题')).toBeNull();
  });

  it('选预设直接提交该标题；选其他出现输入框且必填', async () => {
    const onSubmit = vi.fn(() => okResult);
    render(<NodeEditorDialog mode="add" kind="work" parentId="p-1" rules={defaultRules}
      onClose={() => {}} onSubmit={onSubmit} />);
    const select = screen.getByLabelText('标题') as HTMLSelectElement;
    await userEvent.selectOptions(select, '证据整理');
    await userEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: '证据整理' }));
  });

  it('选其他后填写自定义标题提交；空值不能提交', async () => {
    const onSubmit = vi.fn(() => okResult);
    render(<NodeEditorDialog mode="add" kind="event" parentId="p-1" rules={defaultRules}
      onClose={() => {}} onSubmit={onSubmit} />);
    await userEvent.selectOptions(screen.getByLabelText('标题'), '__other__');
    const custom = screen.getByLabelText('自定义标题');
    expect((screen.getByRole('button', { name: '创建' }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(custom, '临时听证');
    await userEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: '临时听证', kind: 'event' }));
  });

  it('编辑模式与阶段种类仍是自由输入框', () => {
    render(<NodeEditorDialog mode="edit" node={node()} rules={defaultRules}
      onClose={() => {}} onSubmit={() => okResult} />);
    expect((screen.getByLabelText('标题') as HTMLElement).tagName).toBe('INPUT');
    render(<NodeEditorDialog mode="add" kind="stage" parentId={null} rules={defaultRules}
      onClose={() => {}} onSubmit={() => okResult} />);
    const titles = screen.getAllByLabelText('标题');
    expect((titles[titles.length - 1] as HTMLElement).tagName).toBe('INPUT');
  });
});
