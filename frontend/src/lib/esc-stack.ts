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
  /**
   * 挂接 keydown 监听（幂等）。构造时已挂接一次；本方法为 React StrictMode
   * 双调用 effect 准备：cleanup 里的 dispose 摘除监听后，第二轮 setup 必须重新挂接，
   * 否则整个会话 Esc 静默失效（生产 dev 环境实测复现）。
   */
  attach(): void;
  dispose(): void;
}

export interface EscEventTarget {
  addEventListener(type: 'keydown', listener: (event: Event) => void): void;
  removeEventListener(type: 'keydown', listener: (event: Event) => void): void;
}

export function createEscStack(target: EscEventTarget): EscStack {
  const layers: EscLayer[] = [];
  let listening = false;

  const onKeydown = (event: Event) => {
    if ((event as KeyboardEvent).key !== 'Escape' || layers.length === 0) {
      return;
    }
    layers[layers.length - 1].onEscape();
  };

  const stack: EscStack = {
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
    attach() {
      if (listening) {
        return;
      }
      listening = true;
      target.addEventListener('keydown', onKeydown);
    },
    dispose() {
      if (listening) {
        listening = false;
        target.removeEventListener('keydown', onKeydown);
      }
      layers.length = 0;
    },
  };
  // 构造即挂接，渲染期创建的实例立即可用；StrictMode 第二轮 setup 由 provider 再 attach（幂等）
  stack.attach();
  return stack;
}
