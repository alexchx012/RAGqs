/*
 * Esc 逐层向上关闭的全局事件模型（规格 §5；共用基座 §3：
 * 菜单 → 面板 → 对话框 → 下钻层 → 抽屉，Esc 只作用于当前最上层）。
 *
 * 本模块只建立事件模型：应用层（下钻层、抽屉等）通过 push 登记，Esc 派发给栈顶一层。
 * Radix 无头原语（菜单、对话框）内部处理自身 Esc 与焦点圈定，不经此栈；
 * 具体抽屉状态机在 fe-shared-shell 实现并在此栈登记。
 */

export interface EscLayer {
  readonly onEscape: () => void;
}

export interface EscStack {
  /** 登记一层，返回注销函数（逐层向上时先注销弹出的层）。 */
  push(layer: EscLayer): () => void;
  /** 当前登记层数。 */
  depth(): number;
  dispose(): void;
}

export interface EscEventTarget {
  addEventListener(type: 'keydown', listener: (event: Event) => void): void;
  removeEventListener(type: 'keydown', listener: (event: Event) => void): void;
}

export function createEscStack(target: EscEventTarget): EscStack {
  const layers: EscLayer[] = [];

  const onKeydown = (event: Event) => {
    if ((event as KeyboardEvent).key !== 'Escape' || layers.length === 0) {
      return;
    }
    layers[layers.length - 1].onEscape();
  };

  target.addEventListener('keydown', onKeydown);

  return {
    push(layer) {
      layers.push(layer);
      let active = true;
      return () => {
        if (!active) {
          return;
        }
        active = false;
        const index = layers.lastIndexOf(layer);
        if (index >= 0) {
          layers.splice(index, 1);
        }
      };
    },
    depth() {
      return layers.length;
    },
    dispose() {
      target.removeEventListener('keydown', onKeydown);
      layers.length = 0;
    },
  };
}
