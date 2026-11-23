/**
 * 大纲视图（横向树）：节点即按钮；单击选中、双击编辑、右键菜单；
 * 悬浮加号附着在鼠标靠近的边上；收起/展开；规则驱动种类菜单。
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { OutlineView } from './OutlineView';
import { defaultRules } from './rules';
import type { ProgressCommand, ProgressRules } from './types';

function setup(rules: ProgressRules = defaultRules, collapsedTitles: string[] = []) {
  const executor = buildLitigationFixture();
  const collapsedIds = new Set(collapsedTitles.map((title) =>
    executor.getSnapshot().nodes.find((node) => node.title === title)!.id));
  const onSelect = vi.fn();
  const onAddRequest = vi.fn();
  const onEditRequest = vi.fn();
  const onMoveRequest = vi.fn();
  const onToggleCollapse = vi.fn();
  const onCommand = vi.fn((command: ProgressCommand) => executor.human(command));
  const view = render(
    <OutlineView snapshot={executor.getSnapshot()} rules={rules} selectedId={null}
      collapsedIds={collapsedIds} onToggleCollapse={onToggleCollapse}
      onSelect={onSelect} onCommand={onCommand} onAddRequest={onAddRequest}
      onEditRequest={onEditRequest} onMoveRequest={onMoveRequest} />,
  );
  return { executor, onSelect, onAddRequest, onEditRequest, onMoveRequest, onToggleCollapse, onCommand, view };
}

function nodeButton(title: string): HTMLElement {
  const item = screen.getByRole('treeitem', { name: title });
  return item.querySelector('.node-btn') as HTMLElement;
}

describe('OutlineView · 横向树结构', () => {
  it('按包含关系渲染嵌套树，层级用 aria-level 表达', () => {
    setup();
    expect(screen.getByRole('treeitem', { name: '一审' }).getAttribute('aria-level')).toBe('1');
    expect(screen.getByRole('treeitem', { name: '庭前程序' }).getAttribute('aria-level')).toBe('2');
    expect(screen.getByRole('treeitem', { name: '证据交换（第一次）' }).getAttribute('aria-level')).toBe('3');
    expect(screen.getByRole('treeitem', { name: '补充证据清单' }).getAttribute('aria-level')).toBe('4');
    expect(screen.getByRole('treeitem', { name: '庭前程序' }).contains(screen.getByRole('treeitem', { name: '证据交换（第一次）' }))).toBe(true);
  });

  it('节点本身是按钮，含种类、状态、待确认与关联文字', () => {
    setup();
    const hearing = nodeButton('庭审');
    expect(hearing.tagName).toBe('BUTTON');
    expect(hearing.textContent).toContain('进行中');
    expect(hearing.querySelector('.agent-badge.agent-pending')?.textContent).toBe('Agent');
    const exchange = nodeButton('证据交换（第一次）');
    expect(exchange.textContent).toContain('材料：TEST-ONLY 购销合同.pdf');
    // 未被触及的节点没有待确认标识
    expect(nodeButton('立案').querySelector('.agent-badge')).toBeNull();
  });

  it('单击选中，双击编辑', () => {
    const { onSelect, onEditRequest } = setup();
    fireEvent.click(nodeButton('庭前程序'));
    expect(onSelect).toHaveBeenCalledWith(expect.any(String));
    fireEvent.doubleClick(nodeButton('庭前程序'));
    expect(onEditRequest).toHaveBeenCalledWith(expect.objectContaining({ title: '庭前程序' }));
  });

  it('已删除节点不再显示', () => {
    const { executor, view } = setup();
    const work = executor.getSnapshot().nodes.find((node) => node.title === '质证意见')!;
    executor.human({ type: 'remove_node', node_id: work.id, expected_revision: work.revision });
    view.rerender(
      <OutlineView snapshot={executor.getSnapshot()} rules={defaultRules} selectedId={null}
        collapsedIds={new Set()} onToggleCollapse={() => {}}
        onSelect={() => {}} onCommand={(command) => executor.human(command)}
        onAddRequest={() => {}} onEditRequest={() => {}} onMoveRequest={() => {}} />,
    );
    expect(screen.queryByRole('treeitem', { name: '质证意见' })).toBeNull();
  });
});

describe('OutlineView · 右键菜单', () => {
  it('右键打开菜单：设为进行中、编辑、移至、删除', () => {
    const { executor, onCommand, onEditRequest, onMoveRequest } = setup();
    const judgment = executor.getSnapshot().nodes.find((node) => node.title === '裁判')!;
    fireEvent.contextMenu(nodeButton('裁判'), { clientX: 50, clientY: 50 });
    const menu = screen.getByRole('menu', { name: '节点操作' });
    expect(within(menu).getByRole('menuitem', { name: '设为进行中' })).toBeTruthy();
    expect(within(menu).getByRole('menuitem', { name: '编辑' })).toBeTruthy();
    expect(within(menu).getByRole('menuitem', { name: '移至…' })).toBeTruthy();
    expect(within(menu).getByRole('menuitem', { name: '删除' })).toBeTruthy();
    fireEvent.click(within(menu).getByRole('menuitem', { name: '设为进行中' }));
    expect(onCommand).toHaveBeenCalledWith({
      type: 'set_active_stage', stage_id: judgment.id, expected_revision: judgment.revision,
    });
    fireEvent.contextMenu(nodeButton('裁判'), { clientX: 50, clientY: 50 });
    fireEvent.click(screen.getByRole('menuitem', { name: '编辑' }));
    expect(onEditRequest).toHaveBeenCalledWith(expect.objectContaining({ title: '裁判' }));
    fireEvent.contextMenu(nodeButton('裁判'), { clientX: 50, clientY: 50 });
    fireEvent.click(screen.getByRole('menuitem', { name: '移至…' }));
    expect(onMoveRequest).toHaveBeenCalledWith(expect.objectContaining({ title: '裁判' }));
    fireEvent.contextMenu(nodeButton('裁判'), { clientX: 50, clientY: 50 });
    fireEvent.click(screen.getByRole('menuitem', { name: '删除' }));
    expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ type: 'remove_node', node_id: judgment.id }));
  });

  it('非阶段节点菜单没有设为进行中；有子节点的菜单含收起', () => {
    setup();
    fireEvent.contextMenu(nodeButton('证据交换（第一次）'), { clientX: 10, clientY: 10 });
    expect(screen.queryByRole('menuitem', { name: '设为进行中' })).toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.contextMenu(nodeButton('一审'), { clientX: 10, clientY: 10 });
    expect(screen.getByRole('menuitem', { name: '收起子节点' })).toBeTruthy();
  });
});

describe('OutlineView · 悬浮加号与种类菜单', () => {
  it('鼠标悬浮出现加号，点击按规则列出子节点种类', () => {
    const { onAddRequest } = setup();
    const button = nodeButton('庭前程序');
    fireEvent.mouseMove(button, { clientX: 5, clientY: 5 });
    const plus = button.querySelector('.edge-plus') as HTMLElement;
    expect(plus).toBeTruthy();
    fireEvent.click(plus);
    const menu = screen.getByRole('menu', { name: '添加子节点' });
    expect(within(menu).getByRole('menuitem', { name: '工作' })).toBeTruthy();
    expect(within(menu).getByRole('menuitem', { name: '事件' })).toBeTruthy();
    expect(within(menu).queryByRole('menuitem', { name: '程序' })).toBeNull();
    fireEvent.click(within(menu).getByRole('menuitem', { name: '工作' }));
    expect(onAddRequest).toHaveBeenCalledWith(expect.any(String), 'work');
  });

  it('换一份规则即出现新种类，不改组件代码', () => {
    const rules: ProgressRules = {
      ...defaultRules,
      containment: { ...defaultRules.containment, stage: ['work', 'event', 'program'] },
    };
    setup(rules);
    const button = nodeButton('庭前程序');
    fireEvent.mouseMove(button, { clientX: 5, clientY: 5 });
    fireEvent.click(button.querySelector('.edge-plus') as HTMLElement);
    expect(screen.getByRole('menuitem', { name: '程序' })).toBeTruthy();
  });

  it('无允许子种类时不显示加号', () => {
    const rules: ProgressRules = { ...defaultRules, containment: { ...defaultRules.containment, work: [] } };
    setup(rules);
    const button = nodeButton('补充证据清单');
    fireEvent.mouseMove(button, { clientX: 5, clientY: 5 });
    expect(button.querySelector('.edge-plus')).toBeNull();
  });
});

describe('OutlineView · 收起子节点', () => {
  it('收起的节点隐藏子树、显示子项数，aria-expanded 为 false', () => {
    setup(defaultRules, ['一审']);
    expect(screen.queryByRole('treeitem', { name: '立案' })).toBeNull();
    const row = screen.getByRole('treeitem', { name: '一审' });
    expect(row.getAttribute('aria-expanded')).toBe('false');
    expect(nodeButton('一审').textContent).toContain('4 项');
  });

  it('节点上的收起指示点击切换；键盘左收起', () => {
    const { executor, onToggleCollapse } = setup();
    const program = executor.getSnapshot().nodes.find((node) => node.title === '一审')!;
    const item = screen.getByRole('treeitem', { name: '一审' });
    fireEvent.click(item.querySelector('.node-collapse') as HTMLElement);
    expect(onToggleCollapse).toHaveBeenCalledWith(program.id);
    (item as HTMLElement).focus();
    fireEvent.keyDown(item, { key: 'ArrowLeft' });
    expect(onToggleCollapse).toHaveBeenCalledTimes(2);
  });

  it('无子节点的节点不显示收起指示', () => {
    setup();
    expect(screen.getByRole('treeitem', { name: '立案' }).querySelector('.node-collapse')).toBeNull();
  });
});

describe('OutlineView · 工具条与键盘', () => {
  it('根级添加菜单含规则允许的种类与可插入片段', () => {
    const { onAddRequest, onCommand } = setup();
    fireEvent.click(screen.getByRole('button', { name: '＋ 添加' }));
    const menu = screen.getByRole('menu', { name: '根级添加' });
    fireEvent.click(within(menu).getByRole('menuitem', { name: '程序' }));
    expect(onAddRequest).toHaveBeenCalledWith(null, 'program');
    fireEvent.click(screen.getByRole('button', { name: '＋ 添加' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /插入片段：二审/ }));
    expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({
      type: 'insert_fragment', parent_id: null, fragment_key: 'second-instance',
    }));
  });

  it('键盘上下在同级间移动焦点', () => {
    setup();
    const filing = screen.getByRole('treeitem', { name: '立案' });
    const pretrial = screen.getByRole('treeitem', { name: '庭前程序' });
    (filing as HTMLElement).focus();
    fireEvent.keyDown(filing, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(pretrial);
    fireEvent.keyDown(pretrial, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(filing);
  });
});

describe('OutlineView · 回收站/关系/解除关联入口（审查 R3-2/R3-5）', () => {
  function setupWithRestore() {
    const executor = buildLitigationFixture();
    const onCommand = vi.fn((command: ProgressCommand) => executor.human(command));
    const onRestoreRequest = vi.fn((node: import('./types').ProgressNode) =>
      executor.human({ type: 'restore_node', node_id: node.id, expected_revision: node.revision }));
    const view = render(
      <OutlineView snapshot={executor.getSnapshot()} rules={defaultRules} selectedId={null}
        collapsedIds={new Set()} onToggleCollapse={() => {}} onSelect={() => {}}
        onCommand={onCommand} onAddRequest={() => {}} onEditRequest={() => {}}
        onMoveRequest={() => {}} onRestoreRequest={onRestoreRequest} />,
    );
    return { executor, onCommand, onRestoreRequest, view };
  }

  it('删除节点后进入回收站，可恢复回树', () => {
    const { executor, onRestoreRequest, view } = setupWithRestore();
    const work = executor.getSnapshot().nodes.find((node) => node.title === '质证意见')!;
    executor.human({ type: 'remove_node', node_id: work.id, expected_revision: work.revision });
    view.rerender(
      <OutlineView snapshot={executor.getSnapshot()} rules={defaultRules} selectedId={null}
        collapsedIds={new Set()} onToggleCollapse={() => {}} onSelect={() => {}}
        onCommand={(command) => executor.human(command)} onAddRequest={() => {}}
        onEditRequest={() => {}} onMoveRequest={() => {}} onRestoreRequest={onRestoreRequest} />,
    );
    // 树中不再显示，回收站按钮出现
    expect(screen.queryByRole('treeitem', { name: '质证意见' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /已删除节点（1）/ }));
    fireEvent.click(screen.getByRole('button', { name: '恢复' }));
    expect(onRestoreRequest).toHaveBeenCalledWith(expect.objectContaining({ id: work.id }));
  });

  it('关系折叠列表可移除关系', () => {
    const executor = buildLitigationFixture();
    const snap0 = executor.getSnapshot();
    const a = snap0.nodes.find((node) => node.title === '一审')!;
    const b = snap0.nodes.find((node) => node.title === '庭审')!;
    executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: b.id, relation_type: 'causes' });
    const onCommand = vi.fn((command: ProgressCommand) => executor.human(command));
    render(
      <OutlineView snapshot={executor.getSnapshot()} rules={defaultRules} selectedId={null}
        collapsedIds={new Set()} onToggleCollapse={() => {}} onSelect={() => {}}
        onCommand={onCommand} onAddRequest={() => {}} onEditRequest={() => {}} onMoveRequest={() => {}} />,
    );
    fireEvent.click(screen.getByText(/关系（1）/));
    fireEvent.click(screen.getByRole('button', { name: '移除' }));
    expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ type: 'remove_relation' }));
  });

  it('解除关联按钮在节点按钮外（不嵌套），点击发 unlink', () => {
    const { executor, onCommand } = setupWithRestore();
    // 夹具中「补充证据清单」关联了 mat-2
    const work = executor.getSnapshot().nodes.find((node) => node.title === '补充证据清单')!;
    const link = executor.getSnapshot().links.find((item) => item.node_id === work.id)!;
    const removeBtn = screen.getByRole('button', { name: `解除关联 ${link.display_name}` });
    // R3-2：解除按钮不在节点 <button> 内部
    const nodeBtn = nodeButton('补充证据清单');
    expect(nodeBtn.contains(removeBtn)).toBe(false);
    fireEvent.click(removeBtn);
    expect(onCommand).toHaveBeenCalledWith({ type: 'unlink', link_id: link.id });
  });
});