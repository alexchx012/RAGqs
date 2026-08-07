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
  chat: {
    // 思考档阶段状态行（spec §4：只显示预定义状态，展示措辞前端定）
    stage: {
      retrieving: '正在检索', // 措辞后定
      retrievingAgain: '正在重新检索', // 措辞后定
      generating: '正在生成', // 措辞后定
    },
    // 深度研究步骤行展示措辞（spec §4：label 为机读名，展示措辞前端定；M10：已知 label 映射，
    // 未知 label 用通用措辞，不回显机读原串）
    stepLabel: (label: string) => {
      const retrieve = /^retrieve_round_(\d+)$/.exec(label);
      if (retrieve !== null) {
        return `第 ${retrieve[1]} 轮检索`;
      }
      const tool = /^tool_call_(\d+)$/.exec(label);
      if (tool !== null) {
        return `第 ${tool[1]} 次工具调用`;
      }
      const evidence = /^evidence_collect_(\d+)$/.exec(label);
      if (evidence !== null) {
        return `第 ${evidence[1]} 轮收集证据`;
      }
      return '研究步骤'; // 未知 label：通用兜底，不展示原始机读名
    },
    // 深度研究步骤折叠（spec §3.4）
    stepsDone: (count: number) => `已完成 ${count} 步`, // 措辞后定
    stepsExpandAria: '展开步骤', // 措辞后定
    stepsCollapseAria: '折叠步骤', // 措辞后定
    // 系统提示条已知 notice 映射 + 未知值通用提示（spec §4；契约 §3.3/§3.7）
    notice: {
      effortUpgraded: '已自动升级回答强度', // 措辞后定
      retrievalDegraded: '检索范围已降级', // 措辞后定
      rerankDegraded: '重排能力已降级', // 措辞后定
      generic: '系统提示', // 措辞后定
    },
    // stop_reason 固定映射（spec §4；契约 §3.3/§3.7）
    stopReason: {
      manualRequest: '已手动停止', // 措辞后定
      clientDisconnected: '连接未恢复，生成已停止', // 措辞后定
      authorizationRevoked: '会话已撤销，生成已停止', // 措辞后定
    },
    // 生成链路状态行（spec §7）
    connecting: '正在连接', // 措辞后定
    reconnecting: '正在重连', // 措辞后定
    reconnectFailed: '连接未恢复，等待生成结束', // 措辞后定
    stopping: '正在停止', // 措辞后定
    stopped: '已停止', // 措辞后定
    // 请求级错误行（spec §7：409 idempotency_key_conflict 等按请求错误处理）
    requestError: '请求失败，请稍后重试', // 措辞后定
    // 反馈 / A/B 冲突：刷新读模型保留服务端首次结果（spec §5/§6）
    feedbackConflict: '已按首次反馈结果处理', // 措辞后定
    abConflict: '已按首次投票结果处理', // 措辞后定
    feedbackSubmitted: '反馈已提交', // 措辞后定
    abVoted: '已投票', // 措辞后定
    feedbackNoGrounding: '这个答案没依据', // 措辞后定：👎 轻量选项
    feedbackWrongCitation: '引用错了', // 措辞后定：👎 轻量选项
    feedbackUpAria: '点赞', // 措辞后定
    feedbackDownAria: '点踩', // 措辞后定
    feedbackDownMenuAria: '反馈选项', // 措辞后定
    abChoiceFirst: '第一个回答', // 措辞后定
    abChoiceSecond: '第二个回答', // 措辞后定
    abChoiceNeither: '两个都不选，继续', // 措辞后定
    abPickThis: '选这条', // 措辞后定：A/B 对比列投票 ghost pill
    abCompareAria: '盲测回答对比', // 措辞后定
    abVoteOptionAria: (choice: string) => `选择回答 ${choice}`, // 措辞后定

    // 侧边栏（共用基座 §3.2）
    sidebar: {
      searchPlaceholder: '搜索会话', // 措辞后定
      newConversation: '新建会话', // 措辞后定
      emptyList: '暂无会话', // 措辞后定：无任何会话
      emptySearch: '没有匹配的会话', // 措辞后定：搜索过滤无结果（与 emptyList 独立，§3.2）
      listError: '会话加载失败', // 措辞后定
      sectionPinned: '置顶', // 措辞后定
      sectionToday: '今天', // 措辞后定
      sectionWeek: '本周', // 措辞后定
      sectionEarlier: '更早', // 措辞后定
      menuRename: '重命名', // 措辞后定
      menuPin: '置顶', // 措辞后定
      menuUnpin: '取消置顶', // 措辞后定
      menuMoveToGroup: '移入分组', // 措辞后定
      menuDelete: '删除', // 措辞后定
      renamePlaceholder: '会话标题', // 措辞后定
      newGroupPlaceholder: '分组名称', // 措辞后定
      groupSectionAria: (name: string) => `分组 ${name}`, // 措辞后定
      itemMenuAria: (title: string) => `会话 ${title} 操作`, // 措辞后定
      deleteDialogTitle: '删除会话？', // 措辞后定
      deleteDialogDesc: (title: string) => `将删除「${title}」，此操作不可撤销。`, // 措辞后定
      deleteConfirm: '删除', // 措辞后定
      openSidebarAria: '打开会话列表', // 措辞后定
      closeSidebarAria: '关闭会话列表', // 措辞后定
      avatarRowAria: '打开设置', // 措辞后定（头像区；抽屉去向按角色）
      emptyGreeting: '你好，今天想查点什么？', // 措辞后定（新会话空态问候语）
    },

    // 输入区（共用基座 §3.3）
    composer: {
      effortAria: '努力档位', // 措辞后定
      effortQuick: '快速', // 措辞后定
      effortThink: '思考', // 措辞后定
      effortDeep: '深度研究', // 措辞后定
      scopeAria: '检索范围', // 措辞后定
      scopeAll: '全部范围', // 措辞后定
      scopeSearchPlaceholder: '搜索空间', // 措辞后定
      scopePersonalDocuments: '个人库文档', // 措辞后定
      scopeDocumentSearchPlaceholder: '搜索文档', // 措辞后定
      scopeDocumentDrillAria: '展开个人库文档', // 措辞后定
      sendAria: '发送', // 措辞后定
      stopAria: '停止生成', // 措辞后定
      stoppingAria: '正在停止', // 措辞后定
      inputPlaceholder: '输入你的问题…', // 措辞后定
    },

    // 消息区（共用基座 §3.4）
    message: {
      errorLine: '回答生成失败', // 措辞后定
      retry: '重试', // 措辞后定
      retryAttempt: '重试', // 措辞后定：重试链后继消息上方说明
      scrollToBottom: '回到底部', // 措辞后定
      citeFrom: (name: string) => `引自《${name}》`, // 措辞后定：文档名（Citation.document_name）
      citeFromFallback: '引自文档', // 措辞后定：document_name 缺失时的通用措辞（不显示不透明 ID）
      citePage: (page: number) => `第 ${page} 页`, // 措辞后定
      citePageSpan: (page: number, start: number, end: number) => `第 ${page} 页 第 ${start}–${end} 字符`, // 措辞后定
      citeSection: (path: readonly string[], paragraph?: number) =>
        `${path.join(' / ')}${paragraph !== undefined ? ` 第 ${paragraph} 段` : ''}`, // 措辞后定
      citeSheet: (sheet: string, range: string) => `${sheet} ${range}`, // 措辞后定
      citeUnavailable: '内容已不可用', // 措辞后定
      citeOpenAria: '打开引用预览', // 措辞后定
      timeAria: '消息时间', // 措辞后定
    },
    // 原文预览占位页（fe-doc-preview 本体未实现；brief：落占位路由并透传两个 id）
    preview: {
      title: '原文预览', // 措辞后定
      documentLabel: '文档 ID', // 措辞后定
      versionLabel: '版本 ID', // 措辞后定
      placeholderBody: '原文预览页由 fe-doc-preview 提供，当前为占位。', // 措辞后定
      closeAria: '关闭', // 措辞后定
    },
  },
} as const;

export type Copy = typeof zhCN;
