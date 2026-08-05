/**
 * 单一文案常量文件（规格 §6；共用基座 §1）。
 * 全部「措辞后定」文案集中占位于此，不散落在组件里；组件内不允许硬编码中文文案。
 * 后续所有 change 的「措辞后定」文案一律先加入本文件，再在组件中经 copy 引用。
 */
export const zhCN = {
  appName: 'RAGqs',
  shell: {
    skipToContent: '跳到主要内容', // 措辞后定
    placeholderTitle: '知识问答', // 措辞后定
    placeholderBody: '前端工程基座已就绪，业务页面将在后续版本逐步到来。', // 措辞后定
    notFoundTitle: '页面不存在', // 措辞后定
    notFoundBack: '返回首页', // 措辞后定
  },
  a11y: {
    dialogClose: '关闭', // 措辞后定
  },
} as const;

export type Copy = typeof zhCN;
