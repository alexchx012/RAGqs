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
    home: {
      composerPlaceholder: '输入你的问题…', // 措辞后定
      openDrawerAria: '打开设置抽屉', // 措辞后定
    },
    drawer: {
      personalTitle: '设置', // 措辞后定：个人段页级标题
      personalSegmentLabel: '个人', // 措辞后定
      adminSegmentLabel: '管理', // 措辞后定
      backAria: (layerName: string) => `返回${layerName}`, // 措辞后定
      closeAria: '关闭', // 措辞后定
      placeholderBody: '该模块内容将在后续版本逐步到来。', // 措辞后定
      topPlaceholderBody: '从左侧选择要查看的模块。', // 措辞后定
      modules: {
        profile: '个人资料', // 措辞后定
        security: '安全', // 措辞后定
        appearance: '外观', // 措辞后定
        knowledge: '知识库', // 措辞后定
        dashboard: '总览', // 措辞后定
        approvals: '审批中心', // 措辞后定
        spaces: '知识空间', // 措辞后定
        evaluation: '评测与校准', // 措辞后定
        operations: '系统运维', // 措辞后定
        usersOps: '用户管理', // 措辞后定（运维端 §7）
        usersAdmin: '人员与权限', // 措辞后定（超管端 §7）
        uploads: '上传结果', // 措辞后定
        submissions: '我的投稿', // 措辞后定
        publicSpace: '公共库', // 措辞后定
      },
    },
  },
  a11y: {
    dialogClose: '关闭', // 措辞后定
  },
  notifications: {
    title: '提醒', // 措辞后定
    bellAria: '提醒', // 措辞后定
    unreadBadgeAria: (count: number) => `未读提醒 ${count} 条`, // 措辞后定
    readAll: '全部已读', // 措辞后定
    empty: '暂无提醒', // 措辞后定
    error: '提醒加载失败，请稍后重试', // 措辞后定
    retry: '重试', // 措辞后定
    relative: {
      justNow: '刚刚', // 措辞后定
      minutes: (n: number) => `${n} 分钟前`, // 措辞后定
      hours: (n: number) => `${n} 小时前`, // 措辞后定
      days: (n: number) => `${n} 天前`, // 措辞后定
    },
  },
  states: {
    empty: '暂无内容', // 措辞后定
    error: '内容加载失败', // 措辞后定
    retry: '重试', // 措辞后定
  },
  controls: {
    cancel: '取消', // 措辞后定
    confirm: '确认', // 措辞后定
    paginatorPrev: '上一页', // 措辞后定
    paginatorNext: '下一页', // 措辞后定
    pageIndicator: (current: number, total: number) => `第 ${current} / ${total} 页`, // 措辞后定
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
