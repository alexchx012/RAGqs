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
        versions: '版本记录', // 措辞后定（知识库下钻）
        manage: '部门库管理', // 措辞后定（知识库下钻，仅部长）
        knowledgeApprovals: '投稿审核', // 措辞后定（知识库下钻，仅部长）
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
  settings: {
    profile: {
      sectionLabel: '个人资料', // 措辞后定
      avatarAlt: '个人头像', // 措辞后定
      avatarInputLabel: '更换头像', // 措辞后定
      displayNameLabel: '显示名', // 措辞后定
      save: '保存', // 措辞后定
      saveError: '保存失败，请稍后重试', // 措辞后定
      avatarError: '头像上传失败，请稍后重试', // 措辞后定
      realNameLabel: '姓名', // 措辞后定
      departmentLabel: '部门', // 措辞后定
      roleLabel: '角色', // 措辞后定
      roleUser: '普通用户', // 措辞后定
      roleMinister: '部长', // 措辞后定
      roleOps: '运维', // 措辞后定
      roleAdmin: '管理员', // 措辞后定
      adminManaged: '由管理员维护', // 措辞后定
    },
    security: {
      sectionLabel: '安全', // 措辞后定
      passwordTitle: '修改密码', // 措辞后定
      oldPasswordLabel: '当前密码', // 措辞后定
      newPasswordLabel: '新密码', // 措辞后定
      changePassword: '修改密码', // 措辞后定
      passwordRule: '密码至少 8 位，且包含字母和数字', // 措辞后定
      invalidPasswordRule: '密码至少 8 位，且包含字母和数字', // 措辞后定
      wrongOldPassword: '当前密码不正确', // 措辞后定
      passwordChangeError: '密码修改失败，请稍后重试', // 措辞后定
      sessionsTitle: '活跃会话', // 措辞后定
      sessionsLoading: '正在加载活跃会话', // 措辞后定
      sessionsError: '活跃会话加载失败，请稍后重试', // 措辞后定
      currentDevice: '当前设备', // 措辞后定
      lastActiveAt: (value: string) => `最近活跃：${value}`, // 措辞后定
      logoutCurrent: '退出登录', // 措辞后定
      logoutOther: '退出此设备', // 措辞后定
      logoutAll: '退出全部设备', // 措辞后定
      sessionActionError: '会话操作失败，请稍后重试', // 措辞后定
    },
    appearance: {
      sectionLabel: '外观', // 措辞后定
      themeTitle: '主题', // 措辞后定
      themeDescription: '选择界面主题，跟随系统会根据设备设置自动切换', // 措辞后定
      themeAria: '主题', // 措辞后定
      themeLight: '浅色', // 措辞后定
      themeDark: '深色', // 措辞后定
      themeSystem: '跟随系统', // 措辞后定
      fontSizeTitle: '对话字号', // 措辞后定
      fontSizeDescription: '只调整对话正文大小，不影响其他界面文字', // 措辞后定
      fontSizeAria: '对话字号', // 措辞后定
      fontStandard: '标准', // 措辞后定
      fontLarge: '大号', // 措辞后定
      privacyTitle: '隐私', // 措辞后定
      abOptOutLabel: '不参与答案对比测试', // 措辞后定
      abOptOutDescription: '采样由系统决定，用户只有退出权；已创建的对比对不受影响。', // 措辞后定
      loading: '正在加载外观设置', // 措辞后定
      loadError: '外观设置加载失败，请稍后重试', // 措辞后定
      retry: '重试', // 措辞后定
      saveError: '保存失败，已恢复上次设置，请稍后重试', // 措辞后定
    },
    knowledge: {
      sectionLabel: '知识库', // 措辞后定
      quota: {
        title: '本月配额', // 措辞后定
        usedOfLimit: (used: number, limit: number) => `${used} / ${limit} 页`, // 措辞后定
        unlimited: '不限', // 措辞后定
        unlimitedHint: '当前角色不设页面上限', // 措辞后定
        resetsAt: (value: string) => `重置时间：${value}`, // 措辞后定：按 reset_at 倒计时
        days: (count: number) => `${count} 天`, // 措辞后定：倒计时天数单位
        timezone: (value: string) => `以业务时区 ${value} 为准`, // 措辞后定
        pendingRequest: '申请已提交，等待处理', // 措辞后定：常驻行
        requestMore: '申请增加页数', // 措辞后定
        requestDialogTitle: '申请增加页数', // 措辞后定
        requestDescription: '输入需要增加的页数（1–500），提交后等待审批', // 措辞后定
        requestedPagesLabel: '页数', // 措辞后定
        invalidPages: '请输入 1–500 的整数', // 措辞后定：15px 危险红提示
        pendingRequestExists: '当月已有待处理申请，请等待审批结果', // 措辞后定：409
        requestError: '提交失败，请稍后重试', // 措辞后定
      },
      documents: {
        title: '文档', // 措辞后定
        searchPlaceholder: '搜索文档', // 措辞后定
        searchAria: '搜索文档', // 措辞后定
        empty: '暂无文档', // 措辞后定
        loadError: '文档加载失败，请稍后重试', // 措辞后定
        updating: '更新处理中', // 措辞后定：active_operation 非空
        usageDetail: (pages: number, images: number) => `${pages} 页正文${images > 0 ? ` + ${images} 张图` : ''}`, // 措辞后定
        fileSize: (bytes: number) => `${bytes} B`, // 措辞后定：保留简单字节呈现
        uploadedAt: (value: string) => `上传于 ${value}`, // 措辞后定
        uploadNewVersion: '上传新版本', // 措辞后定
        versions: '版本记录', // 措辞后定
        reindex: '重建索引', // 措辞后定
        delete: '删除', // 措辞后定
        deleteConfirmTitle: '删除文档？', // 措辞后定
        deleteConfirmDescription: (name: string) => `将永久删除「${name}」，此操作不可撤销。`, // 措辞后定
        reindexConfirmTitle: '重建索引？', // 措辞后定
        reindexConfirmDescription: (name: string) => `将对「${name}」重新解析并建立索引，处理期间文档继续可检索。`, // 措辞后定
        rowMenuAria: (name: string) => `文档 ${name} 操作`, // 措辞后定
        loading: '正在加载文档', // 措辞后定
      },
      upload: {
        button: '上传文档', // 措辞后定：知识库首页唯一上传入口
        dialogTitle: '上传文档', // 措辞后定
        dialogDescription: '选择目标空间，可一次上传多个文件', // 措辞后定
        targetLabel: '目标空间', // 措辞后定
        manageTargetHint: '上传后直接写入该空间', // 措辞后定
        contributeTargetHint: '需审核后才能发布，先进入「我的投稿」', // 措辞后定：contribute 分支提示
        chooseFiles: '选择文件', // 措辞后定
        noFiles: '尚未选择文件', // 措辞后定
        fileListAria: '已选文件', // 措辞后定
        upload: '上传', // 措辞后定
        uploading: '正在上传', // 措辞后定
        accepted: '已接收', // 措辞后定
        deduplicated: '内容重复，未新增任务', // 措辞后定：deduplicated 提示
        submissionCreated: '已创建投稿', // 措辞后定：投稿项
        quotaExceeded: '配额已达上限，本次上传已整批拒绝', // 措辞后定：409
        itemError: (code: string) => {
          switch (code) {
            case 'upload_too_large':
              return '文件过大，超出上传上限';
            case 'unsupported_media_type':
              return '不支持该文件类型';
            case 'upload_content_type_mismatch':
              return '文件内容与声明类型不符';
            case 'unsafe_archive':
              return '压缩包存在安全风险';
            case 'malware_detected':
              return '检测到恶意内容';
            default:
              return '文件上传失败';
          }
        }, // 措辞后定：服务端错误对象映射，未知 code 通用兜底
        resultSummary: (accepted: number, failed: number) =>
          `成功 ${accepted} 项${failed > 0 ? `，失败 ${failed} 项` : ''}`, // 措辞后定
        noSpaceSelected: '请选择目标空间', // 措辞后定
        newVersionDialogTitle: '上传新版本', // 措辞后定：固定目标对话框
        newVersionDescription: (name: string) => `为「${name}」上传新版本，处理完成后替换当前版本`, // 措辞后定
        newVersionSingle: '新版本上传为单文件', // 措辞后定
      },
      uploads: {
        title: '上传结果', // 措辞后定
        loading: '正在加载上传任务', // 措辞后定
        loadError: '上传任务加载失败，请稍后重试', // 措辞后定
        empty: '暂无上传任务', // 措辞后定
        historyTitle: '最近一次上传结果', // 措辞后定：上传结果历史入口（不随对话框卸载丢失）
        historyEmpty: '本次会话还没有上传记录', // 措辞后定
        historyEntry: '上传结果', // 措辞后定：知识库首页工具行入口按钮
        historyAt: (value: string) => `上传于 ${value}`, // 措辞后定
        historyTarget: (name: string) => `目标：${name}`, // 措辞后定
        recentWindow: '仅显示最近的上传任务', // 措辞后定：has_more 窗口提示
        batchTitle: (id: string) => `批次 ${id}`, // 措辞后定
        batchPartial: '部分任务尚未完成', // 措辞后定：partial 批次标题提示
        stage: {
          queued: '排队中', // 措辞后定
          parsing: '解析中', // 措辞后定
          indexing: '索引中', // 措辞后定
        }, // 仅 pending/running 显示，不做假进度条
        nextAttemptAt: (value: string) => `下次尝试：${value}`, // 措辞后定：retry_wait
        usageSucceeded: (pages: number, images: number) => `已入库 ${pages} 页${images > 0 ? ` + ${images} 张图` : ''}`, // 措辞后定
        failureReason: (reason: string) => `失败原因：${reason}`, // 措辞后定
        ocrLowConfidence: '识别置信度较低，建议人工核对', // 措辞后定：琥珀标记
        cancel: '取消任务', // 措辞后定
        replay: '人工重放', // 措辞后定
        cancelConfirmTitle: '取消任务？', // 措辞后定
        cancelConfirmDescription: (name: string) => `将取消「${name}」的处理，此操作不可撤销。`, // 措辞后定
        enteringAt: (value: string) => `进入时间：${value}`, // 措辞后定
        targetSpace: (name: string) => `目标：${name}`, // 措辞后定
        stateLabel: (state: string) => {
          switch (state) {
            case 'pending':
              return '排队中';
            case 'running':
              return '处理中';
            case 'retry_wait':
              return '等待重试';
            case 'succeeded':
              return '成功';
            case 'failed':
              return '失败';
            case 'dead_letter':
              return '已丢弃';
            case 'cancelled':
              return '已取消';
            default:
              return state;
          }
        }, // 措辞后定：§1 完整 job 状态集标签
        actionConflict: '状态已变化，已刷新', // 措辞后定：竞态 409/403 刷新提示
      },
      versions: {
        title: '版本记录', // 措辞后定
        loading: '正在加载版本记录', // 措辞后定
        loadError: '版本记录加载失败，请稍后重试', // 措辞后定
        empty: '暂无版本记录', // 措辞后定
        active: '当前版本', // 措辞后定
        versionNumber: (n: number) => `v${n}`, // 措辞后定
        createdAt: (value: string) => `创建于 ${value}`, // 措辞后定
        contentUnavailable: '内容已不可用', // 措辞后定：purging/purged
        preview: '预览', // 措辞后定
        restore: '恢复', // 措辞后定
        restoreConfirmTitle: '恢复该版本？', // 措辞后定
        restoreConfirmDescription: '恢复会创建新版本并重新处理，处理成功前当前版本继续服务。', // 措辞后定
        restoreSuccess: '已创建恢复任务，请在上传结果中查看进度', // 措辞后定
        versionPurged: '该版本已被清理，内容不可用', // 措辞后定：409 document_version_purged
        restoreError: '恢复失败，请稍后重试', // 措辞后定
      },
      submissions: {
        title: '我的投稿', // 措辞后定
        entry: '我的投稿', // 措辞后定：无边框按钮
        loading: '正在加载投稿', // 措辞后定
        loadError: '投稿加载失败，请稍后重试', // 措辞后定
        empty: '暂无投稿', // 措辞后定
        filters: {
          all: '全部', // 措辞后定
          pending: '待审核', // 措辞后定
          approved: '已通过', // 措辞后定
          rejected: '已驳回', // 措辞后定
          withdrawn: '已撤回', // 措辞后定
          invalidated: '已失效', // 措辞后定
        },
        filterAria: (name: string) => `筛选：${name}`, // 措辞后定
        statusTag: {
          pending: '待审核', // 措辞后定：ash-gray
          approved: '已通过', // 措辞后定：成功绿
          rejected: '已驳回', // 措辞后定：危险红
          withdrawn: '已撤回', // 措辞后定：slate-gray
          invalidated: '已失效', // 措辞后定：警告琥珀
        },
        rejectReason: (reason: string) => `驳回原因：${reason}`, // 措辞后定：tag 下方
        invalidatedReason: '因规则变更或内容问题已失效，如需重新提交请删除后重新上传', // 措辞后定：机器原因固定提示
        targetSpace: (name: string) => `目标：${name}`, // 措辞后定
        submittedAt: (value: string) => `投稿于 ${value}`, // 措辞后定
        viewContent: '查看内容', // 措辞后定
        withdraw: '撤回', // 措辞后定
        delete: '删除', // 措辞后定
        withdrawConfirmTitle: '撤回投稿？', // 措辞后定
        withdrawConfirmDescription: '撤回后不再进入审核且不可改回；原文件将被清理，需要重新上传。', // 措辞后定：固定两点说明
        deleteConfirmTitle: '删除投稿？', // 措辞后定
        deleteConfirmDescription: (name: string) => `将永久删除「${name}」，此操作不可撤销。`, // 措辞后定
        contentUnavailable: '内容已不可用', // 措辞后定：404 submission_content_unavailable
        contentOpenBlocked: '浏览器拦截了新窗口，请允许弹出后重试', // 措辞后定：popup 被拦截
        actionError: '操作失败，请稍后重试', // 措辞后定
        versionConflict: '内容已变化，已刷新，请确认后重试', // 措辞后定：409 version_conflict
        stateConflict: '投稿状态已变化，已刷新', // 措辞后定：409 submission_state_conflict
        retry: '重试', // 措辞后定：行尾重试文字链
      },
      manage: {
        title: '部门库管理', // 措辞后定：部长入口
        approvals: '投稿审核', // 措辞后定
        approvalsBadgeAria: (count: number) => `待处理投稿 ${count} 条`, // 措辞后定
        approvalsLoading: '正在加载待审核投稿', // 措辞后定
        approvalsError: '待审核投稿加载失败，请稍后重试', // 措辞后定
        approvalsEmpty: '暂无待审核投稿', // 措辞后定
        submitter: (name: string) => `投稿人：${name}`, // 措辞后定
        submittedAt: (value: string) => `投稿于 ${value}`, // 措辞后定
        fileMeta: (kind: string, size: string) => `${kind} · ${size}`, // 措辞后定
        approve: '通过', // 措辞后定：filled 小 pill
        reject: '驳回', // 措辞后定：ghost 小 pill
        rejectDialogTitle: '驳回投稿？', // 措辞后定
        rejectDialogDescription: '可填写一句原因，将随通知送达投稿人。', // 措辞后定
        rejectReasonPlaceholder: '可填一句原因', // 措辞后定
        approvedNotice: '已通过，投稿人将收到通知', // 措辞后定：页头下轻提示
        rejectedNotice: '已驳回，原因将随通知送达投稿人', // 措辞后定
        duplicateDocument: '该文件已存在，投稿人需处理后重新提交', // 措辞后定：duplicate_document 行内提示
        versionConflict: '投稿内容已变化，已刷新，请确认后重试', // 措辞后定：version_conflict
        scopeChanged: '投稿状态已变化，已刷新', // 措辞后定：submission_scope_changed / already_reviewed
        actionError: '操作失败，请稍后重试', // 措辞后定
        viewContent: '查看内容', // 措辞后定
        contentUnavailable: '内容已不可用', // 措辞后定：404
      },
    },
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
