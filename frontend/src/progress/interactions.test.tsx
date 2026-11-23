/**
 * 节点交互共用件：最近边计算、右键菜单、种类菜单。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ContextMenu, nearestEdge } from './interactions';

describe('nearestEdge', () => {
  const rect = { left: 100, top: 100, right: 200, bottom: 150, width: 100, height: 50 } as DOMRect;
  it('靠近哪条边返回哪条边', () => {
    expect(nearestEdge(150, 102, rect)).toBe('top');
    expect(nearestEdge(150, 148, rect)).toBe('bottom');
    expect(nearestEdge(102, 125, rect)).toBe('left');
    expect(nearestEdge(198, 125, rect)).toBe('right');
  });
});

describe('ContextMenu', () => {
  it('渲染菜单项，点击后执行并关闭', () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<ContextMenu x={10} y={10} label="节点操作" onClose={onClose}
      items={[{ label: '设为进行中', onSelect }, { label: '删除', onSelect, danger: true }]} />);
    const menu = screen.getByRole('menu', { name: '节点操作' });
    expect(menu.querySelectorAll('[role=menuitem]')).toHaveLength(2);
    fireEvent.click(screen.getByRole('menuitem', { name: '设为进行中' }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
  });

  it('Escape 关闭；禁用项不执行', () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<ContextMenu x={10} y={10} label="节点操作" onClose={onClose}
      items={[{ label: '编辑', onSelect, disabled: true }]} />);
    fireEvent.click(screen.getByRole('menuitem', { name: '编辑' }));
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('点击菜单外关闭', () => {
    const onClose = vi.fn();
    render(<ContextMenu x={10} y={10} label="节点操作" onClose={onClose}
      items={[{ label: '编辑', onSelect: () => {} }]} />);
    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalled();
  });
});