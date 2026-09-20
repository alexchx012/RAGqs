/*
 * 抽屉作用域 context（drawer-visual-system 规格 §抽屉作用域不外溢，裁决 R3）。
 * CSS 自定义属性按 DOM 祖先继承，而抽屉内的 Radix 浮层（确认框、「⋯」菜单、提醒面板、
 * 管理段筛选 Popover）经 portal 挂到 document.body，落在抽屉子树之外——它们读不到
 * [data-drawer-scope] 上的作用域变量，会静默回落到全局按钮圆角与旧危险色。
 * 这些浮层在 React 树中仍是抽屉的后代，故用 context 表达「当前位于抽屉内」，
 * 由浮层自己给 portal 内容根节点补挂 data-drawer-scope。
 * 不采用 Radix container 把 portal 挂进抽屉子树：抽屉用 translateY 做滑入动画，
 * 该变换会成为 position: fixed 后代的包含块，导致浮层错位。
 */

import { createContext, useContext } from 'react';

/** 是否位于抽屉作用域内；抽屉外（聊天主页、原文预览等）为 false。 */
export const DrawerScopeContext = createContext(false);

/** 读取当前是否位于抽屉作用域内（浮层据此决定是否给 portal 内容根节点挂作用域属性）。 */
export function useDrawerScope(): boolean {
  return useContext(DrawerScopeContext);
}
