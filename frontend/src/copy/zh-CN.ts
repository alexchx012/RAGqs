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
    loading: '加载中', // 措辞后定
  },
  a11y: {
    dialogClose: '关闭', // 措辞后定
  },
  login: {
    title: '登录', // 措辞后定
    tagline: '企业内部知识问答', // 措辞后定
    brandFooter: '', // 措辞后定：版本 / 公司名，可为空
    usernameLabel: '用户名', // 措辞后定
    passwordLabel: '密码', // 措辞后定
    submit: '登录', // 措辞后定
    submitting: '登录中', // 措辞后定
    guide: '账号由管理员开通，忘记密码请联系管理员', // 措辞后定
    showPassword: '显示密码', // 措辞后定
    hidePassword: '隐藏密码', // 措辞后定
    errorInvalidCredentials: '用户名或密码不正确', // 措辞后定
    errorTooManyAttempts: '尝试次数过多，请稍后再试', // 措辞后定
    errorServiceUnavailable: '服务暂时不可用，请稍后重试', // 措辞后定
    retryCountdown: (seconds: number) => `${seconds} 秒后可重试`, // 措辞后定
  },
} as const;

export type Copy = typeof zhCN;
