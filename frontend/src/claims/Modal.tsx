/**
 * 原生 <dialog> 封装：沿用项目的 showModal 用法，不额外设置 role="dialog"。
 * Escape（cancel）与关闭按钮都交回调用方卸载组件。
 */
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

export function Modal({ labelledBy, className = 'work-dialog', onClose, children }: {
  labelledBy: string;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (element && !element.open) element.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className={className}
      aria-labelledby={labelledBy}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClose={onClose}
    >
      {children}
    </dialog>
  );
}
