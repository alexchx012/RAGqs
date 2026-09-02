/**
 * 单一文案常量文件（规格 §6；共用基座 §1）。
 * 全部「措辞后定」文案集中占位于此，不散落在组件里；组件内不允许硬编码中文文案。
 * 后续所有 change 的「措辞后定」文案一律先加入本文件，再在组件中经 copy 引用。
 */
import type { Role } from '../auth/types';

export const zhCN = {
  appName: 'RAGqs',
  shell: {
    skipToContent: '跳到主要内容', // 措辞后定
    notFoundTitle: '页面不存在', // 措辞后定
    notFoundBack: '返回首页', // 措辞后定
    loading: '加载中', // 措辞后定
    // 顶层 ErrorBoundary 兜底页（渲染异常防白屏）
    errorBoundary: {
      title: '页面出现异常', // 措辞后定
      description: '页面渲染遇到问题，刷新通常可以恢复；若持续出现请稍后再试。', // 措辞后定
      reload: '刷新页面', // 措辞后定
    },
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
        quotaRequests: '配额申请', // 措辞后定（审批中心下钻）
        personalLibs: '用户个人库', // 措辞后定（知识空间下钻）
        departmentLibs: '部门库', // 措辞后定（知识空间下钻）
        opsJobs: '任务队列', // 措辞后定（系统运维下钻）
        opsMetrics: '指标看板', // 措辞后定（系统运维下钻）
        backups: '备份与恢复', // 措辞后定（系统运维下钻，仅 ops）
        departments: '部门管理', // 措辞后定（人员与权限下钻）
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
      saved: '已保存', // 措辞后定：保存成功小字（15px 成功绿，约 2s 后淡出）
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
      confirmPasswordLabel: '再次输入新密码', // 措辞后定
      passwordMismatch: '两次输入的新密码不一致', // 措辞后定
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
      privacyTitle: '隐私', // 措辞后定
      abOptOutLabel: '不参与答案对比测试', // 措辞后定
      abOptOutDescription: '采样由系统决定，用户只有退出权；已创建的对比对不受影响。', // 措辞后定
      preferencesLoading: '正在加载隐私设置', // 措辞后定
      preferencesLoadError: '隐私设置加载失败，请稍后重试', // 措辞后定
      preferencesSaveError: '保存失败，已恢复上次设置，请稍后重试', // 措辞后定
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
        stored: '已入库', // 措辞后定：常态状态列文字
        usageDetail: (pages: number, images: number) => `${pages} 页正文${images > 0 ? ` + ${images} 张图` : ''}`, // 措辞后定
        fileSize: (bytes: number) => `${bytes} B`, // 措辞后定：保留简单字节呈现
        uploadedAt: (value: string) => `上传于 ${value}`, // 措辞后定
        uploadNewVersion: '上传新版本', // 措辞后定
        versions: '版本记录', // 措辞后定
        reindex: '重建索引', // 措辞后定
        delete: '删除', // 措辞后定
        deleteConfirmTitle: '删除文档？', // 措辞后定
        deleteConfirmDescription: '文档及全部版本立即永久退出列表、检索、预览和下载，操作不可恢复；历史回答正文保留，但引用内容将不可用。', // 措辞后定：固定两点说明（共用基座 §5.6）
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
        spaceSearchPlaceholder: '搜索空间', // 措辞后定：返回项 >8 行时顶部搜索框
        spaceSearchEmpty: '没有匹配的空间', // 措辞后定：过滤无结果
        manageTargetHint: '上传后直接写入该空间', // 措辞后定
        contributeTargetHint: '需审核后才能发布，先进入「我的投稿」', // 措辞后定：contribute 分支提示
        chooseFiles: '选择文件', // 措辞后定
        dropHint: '拖拽文件到此处，或点击选择', // 措辞后定：拖拽区说明
        removeFile: '移除文件', // 措辞后定：已选文件行尾 × 的 aria
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
            case 'upload_media_mismatch':
              return '文件类型与扩展名不符';
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
  admin: {
    // 管理面板共用标记（ops/admin 双端）
    common: {
      readOnly: '只读', // 措辞后定：超管查看侧标记
      deactivatedReadOnly: '已停用，只读', // 措辞后定
      frozenTag: '已冻结，待清理', // 措辞后定：pending_delete 行标记
      refresh: '刷新', // 措辞后定
      // 角色中文标签（用户列表角色列；与 mock 聚合搜索匹配口径一致）
      roleLabels: {
        user: '普通用户', // 措辞后定
        minister: '部长', // 措辞后定
        ops: '运维', // 措辞后定
        admin: '管理员', // 措辞后定
      } as Record<Role, string>,
    },
    // 总览 dashboard（§9.1）
    dashboard: {
      title: '总览', // 措辞后定
      windowAria: '时间窗口', // 措辞后定
      today: '今天', // 措辞后定
      d7: '近 7 天', // 措辞后定
      d30: '近 30 天', // 措辞后定
      noData: '暂无数据', // 措辞后定：value 为 null 的指标卡
      empty: '暂无指标', // 措辞后定
      loadError: '指标加载失败，请稍后重试', // 措辞后定
      expandAll: '展开全部', // 措辞后定
      collapse: '收起', // 措辞后定：user_rank 展开全部后的收起链
      // 超管四包标题下 15px slate 说明行（契约外展示字段，由组包方随 pack.description 下发）
      packs: {
        usageOverview: '全平台使用趋势与部门分布', // 措辞后定
        assetUsage: '各空间被检索与被引用的频次分布', // 措辞后定
        costShare: 'LLM 成本总量与部门、用户分摊', // 措辞后定
        qualityQuota: '回答质量反馈与配额消耗、追加发放', // 措辞后定
      },
    },
    // 审批中心（§8：配额申请 ops 写 / 投稿审核复用 knowledge.manage 文案）
    approvals: {
      quota: '配额申请', // 措辞后定
      submissions: '投稿审核', // 措辞后定
      colApplicant: '申请人', // 措辞后定
      colUsage: '当前用量', // 措辞后定
      colRequested: '申请页数', // 措辞后定
      colRequestedAt: '申请时间', // 措辞后定
      colActions: '操作', // 措辞后定
      colFile: '文件', // 投稿审核语义表格列头
      colSubmittedAt: '投稿时间', // 投稿审核语义表格列头
      colSubmitter: '投稿人', // 投稿审核语义表格列头（超管端 §7.3）
      colKindSize: '类型 / 大小', // 投稿审核语义表格列头（超管端 §7.3）
      colTargetSpace: '目标空间', // 投稿审核语义表格列头（超管端 §7.3）
      filterAll: '全部', // 目标空间筛选（超管端 §7.3）
      filterPublic: '公共库', // 目标空间筛选
      filterDepartment: '部门库', // 目标空间筛选
      filterSpaceAria: '目标空间筛选', // 目标空间筛选分段控件 aria
      filterDepartmentAria: '部门筛选', // 部门下拉 aria（选中「部门库」时出现）
      usageOf: (used: number, limit: number) => `${used} / ${limit} 页`, // 措辞后定
      pages: (count: number) => `${count} 页`, // 措辞后定
      approve: '批准', // 措辞后定
      reject: '驳回', // 措辞后定
      approveDialogTitle: '批准配额申请？', // 措辞后定
      approveDialogDescription: (name: string, pages: number) =>
        `将为 ${name} 增加 ${pages} 页额度，当月生效。`, // 措辞后定
      approvePagesLabel: '批准页数（可选）', // 措辞后定
      approvePagesPlaceholder: (requested: number) => `留空按申请量 ${requested} 页批准`, // 措辞后定
      approvePagesInvalid: (requested: number) => `请输入 1–${requested} 的整数`, // 措辞后定
      rejectDialogTitle: '驳回配额申请？', // 措辞后定
      rejectDialogDescription: '驳回后申请人当月可重新提交申请。', // 措辞后定
      approvedNotice: '已批准，申请人将收到通知', // 措辞后定：页头下轻提示
      rejectedNotice: '已驳回，申请人将收到通知', // 措辞后定
      alreadyProcessed: '该申请已被处理，已刷新', // 措辞后定：409 already_processed
      notApprovable: '该申请已不可审批，已刷新', // 措辞后定：409 quota_request_not_approvable
      versionConflict: '内容已变化，已刷新，请确认后重试', // 措辞后定：409 version_conflict
      actionError: '操作失败，请稍后重试', // 措辞后定
      empty: '暂无待处理申请', // 措辞后定
      loadError: '申请加载失败，请稍后重试', // 措辞后定
      scopeAll: '全部', // 措辞后定：超管投稿审核范围分段
      scopePublic: '公共库', // 措辞后定
      scopeDepartment: '部门库', // 措辞后定
    },
    // 知识空间（§7：公共库 / 用户个人库 / 部门库；图谱维护区 ops 写 / admin 读）
    spaces: {
      personalLibs: '用户个人库', // 措辞后定
      departmentLibs: '部门库', // 措辞后定
      documents: (count: number) => `${count} 篇文档`, // 措辞后定
      members: (count: number) => `${count} 名成员`, // 措辞后定
      colDocument: '文档', // 文档列表语义表格列头
      colStatus: '状态', // 文档列表语义表格列头
      colUploadedAt: '上传时间', // 文档列表语义表格列头
      colUsage: '用量', // 文档列表语义表格列头
      colActions: '操作', // 文档列表语义表格列头
      empty: '暂无空间', // 措辞后定
      loadError: '空间加载失败，请稍后重试', // 措辞后定
      // 个人库下钻
      userSearchPlaceholder: '搜索姓名、用户名、部门或角色', // 措辞后定：聚合搜索框
      userSearchAria: '搜索用户', // 措辞后定
      backToUsers: '返回用户列表', // 措辞后定
      backToDepartments: '返回部门列表', // 措辞后定
      personalLibOf: (name: string) => `${name} 的个人库`, // 措辞后定：下钻页头
      emptyUsers: '暂无用户', // 措辞后定
      // 只读文档列表（§12.6：无上传、无行操作；打开即记审计由后端负责）
      docStatusAvailable: '可用', // 措辞后定：状态列 15px
      openPreviewAria: (name: string) => `打开「${name}」预览`, // 措辞后定：行点击新窗口
      reindexStarted: '已发起重建索引', // 措辞后定：重建 202 轻提示
      graph: {
        title: '图谱维护', // 措辞后定
        availabilityReady: '图谱可用', // 措辞后定
        availabilityStale: '图谱需重建', // 措辞后定：公共库已变更，不得把旧 generation 展示为可用
        availabilityDisabled: '图谱未构建', // 措辞后定
        generationInfo: (id: string, builtAt: string) => `当前生成 ${id} · 构建于 ${builtAt}`, // 措辞后定
        generationExpired: '上一版生成已过期，重建后方可使用', // 措辞后定：stale 时 active_generation 标注
        statusQueued: '排队中', // 措辞后定
        statusRunning: '构建中', // 措辞后定
        statusSucceeded: '构建成功', // 措辞后定
        statusFailed: '构建失败', // 措辞后定
        statusCancelled: '已取消', // 措辞后定
        sourceRevision: (revision: number) => `源版本 ${revision}`, // 措辞后定
        latestRunTitle: '最近一次构建', // 措辞后定
        estimatedCalls: (count: number) => `预估主模型调用 ${count} 次`, // 措辞后定
        actualCalls: (primary: number, provider: number) =>
          `实际用量：主模型 ${primary} 次 / provider ${provider} 次`, // 措辞后定
        runCreatedAt: (value: string) => `创建于 ${value}`, // 措辞后定
        runStartedAt: (value: string) => `开始于 ${value}`, // 措辞后定
        runFinishedAt: (value: string) => `完成于 ${value}`, // 措辞后定
        failureClass: (value: string) => `失败分类：${value}`, // 措辞后定
        buildCreate: '构建图谱', // 措辞后定：disabled 态发起
        buildRebuild: '重建图谱', // 措辞后定：ready / stale 态发起
        cancel: '取消构建', // 措辞后定
        confirmTitleCreate: '发起图谱构建？', // 措辞后定
        confirmTitleRebuild: '重建公共库图谱？', // 措辞后定
        confirmDescription: '构建耗时较长，期间可离开页面，完成后将经铃铛通知你。', // 措辞后定
        confirmRevision: (revision: number) => `当前内容版本：${revision}`, // 措辞后定
        confirmEstimate: (calls: number) =>
          `上次构建预估主模型调用 ${calls} 次，本次预估以提交后服务端计算为准`, // 措辞后定
        confirmEstimatePending: '预估主模型调用次数将在提交后由服务端计算并展示', // 措辞后定
        confirmStart: '确认发起', // 措辞后定
        empty: '暂无构建记录', // 措辞后定
        loadError: '图谱状态加载失败，请稍后重试', // 措辞后定
        actionError: '操作失败，请稍后重试', // 措辞后定
        sourceChanged: '公共库内容已变化，已刷新状态，请重新确认', // 措辞后定：409 graph_source_changed
        inProgress: '已有构建进行中，已刷新状态', // 措辞后定：409 graph_build_in_progress
        notCancellable: '该构建已不可取消，已刷新状态', // 措辞后定：409 graph_build_not_cancellable
        sourceEmpty: '公共库暂无文档，无法构建图谱', // 措辞后定：422 graph_source_empty
        estimateUnavailable: '暂时无法计算构建预估，请稍后重新发起', // 措辞后定：503 + 重试文字链
        runVersionConflict: '构建状态已变化，已刷新', // 措辞后定：取消时 409 version_conflict
        startedNotice: '已发起构建', // 措辞后定：202 轻提示
        cancelledNotice: '已取消构建', // 措辞后定
      },
    },
    // 评测与校准（§11；开窗/关窗仅运维，超管只读）
    evaluation: {
      windowCardTitle: '校准窗口', // 措辞后定
      leaderboardTitle: '评测榜单', // 措辞后定
      shadowTitle: '影子评测排名', // 措辞后定
      statusOpen: '开窗中', // 措辞后定
      statusClosing: '收口中', // 措辞后定
      statusClosed: '已关闭', // 措辞后定
      opsOnlyNote: '开窗由运维操作', // 措辞后定：超管端固定说明
      pairsCollected: (count: number) => `已收集对比 ${count} 对`, // 措辞后定
      sampleRate: (percent: string) => `实际采样率 ${percent}`, // 措辞后定（0–1 经 formatPercent 格式化后传入）
      policyVersion: (value: string) => `策略版本 ${value}`, // 措辞后定
      windowOpenedAt: (value: string) => `开窗时间 ${value}`, // 措辞后定
      windowRange: (opened: string, closed: string) => `窗口 ${opened} 至 ${closed}`, // 措辞后定
      closingDeadline: (time: string) => `将于 ${time} 收口`, // 措辞后定：closing 收口倒计时
      empty: '暂无评测数据', // 措辞后定
      loadError: '评测数据加载失败，请稍后重试', // 措辞后定
      windowLoadError: '校准窗口状态加载失败，请稍后重试', // 措辞后定
      open: '开窗', // 措辞后定
      close: '关窗', // 措辞后定
      switchAria: '校准窗口开关', // 措辞后定
      kindLabel: '开窗方式', // 措辞后定：开窗确认对话框内单选
      kindColdStart: '冷启动', // 措辞后定
      kindSentinel: '哨兵', // 措辞后定
      kindManual: '手动', // 措辞后定
      openDialogTitle: '开启校准窗口？', // 措辞后定
      openDialogDescription: '开启后按所选方式采样真实提问，收集 A/B 对比。', // 措辞后定
      closeDialogTitle: '关闭校准窗口？', // 措辞后定
      closeDialogDescription: '关闭后窗口进入收口：不再创建新对比，已有对比在截止前仍可投票。', // 措辞后定
      openedNotice: '已开窗', // 措辞后定
      closingNotice: '已关窗，窗口收口中', // 措辞后定
      actionError: '操作失败，请稍后重试', // 措辞后定
      errorNotEligible: '当前不满足开窗条件，已刷新窗口状态', // 措辞后定：409 calibration_window_not_eligible
      errorAlreadyOpen: '已有开窗中的校准窗口，已刷新窗口状态', // 措辞后定：409 calibration_window_already_open
      errorClosing: '已有收口中的校准窗口，已刷新窗口状态', // 措辞后定：409 calibration_window_closing
      errorNotOpen: '当前没有可关闭的窗口，已刷新窗口状态', // 措辞后定：409 calibration_window_not_open
      colRank: '名次', // 措辞后定
      colName: '配置', // 措辞后定
      colScore: '得分', // 措辞后定
      notEligibleTag: '未达门槛', // 措辞后定：eligible=false 名称后行内说明
      policyGap: (value: number) => `开窗分差阈值 ${value}`, // 措辞后定
      policyColdStartRate: (percent: string) => `冷启动采样 ${percent}`, // 措辞后定
      policySentinelRate: (percent: string) => `哨兵采样 ${percent}`, // 措辞后定
      policyMinRealQueries: (count: number) => `最小真实提问 ${count}`, // 措辞后定
      policyShadowMaxExamples: (count: number) => `影子题目上限 ${count}`, // 措辞后定
      policyShadowMaxConfigs: (count: number) => `候选配置上限 ${count}`, // 措辞后定
    },
    // 系统运维（§10 任务队列 + §9.2 指标看板）
    operations: {
      jobs: '任务队列', // 措辞后定
      metrics: '指标看板', // 措辞后定
      viewAll: '全部', // 措辞后定
      viewActive: '处理中', // 措辞后定
      viewReplayable: '待人工处理', // 措辞后定
      viewStale: '超时', // 措辞后定
      staleTag: '超时', // 措辞后定：行内 stale 标记
      taskTypeIngestion: '文档入库', // 措辞后定：任务类型列（V1 仅 ingestion）
      colTask: '任务', // 措辞后定
      colJobId: '任务 ID', // 措辞后定
      colQueuedAt: '入队时间', // 措辞后定
      colWaitDuration: '停留时长', // 措辞后定
      colStatus: '状态', // 措辞后定
      colActions: '操作', // 措辞后定
      waitDuration: (totalSeconds: number) => {
        // 停留时长（wait_seconds）：不足 1 分钟显秒，不足 1 小时显分秒，再长显小时分
        const seconds = Math.max(0, Math.floor(totalSeconds));
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        if (hours > 0) {
          return `${hours} 小时 ${minutes % 60} 分`;
        }
        if (minutes > 0) {
          return `${minutes} 分 ${seconds % 60} 秒`;
        }
        return `${seconds} 秒`;
      }, // 措辞后定
      emptyJobs: '暂无任务', // 措辞后定
      loadError: '加载失败，请稍后重试', // 措辞后定
      actionError: '操作失败，请稍后重试', // 措辞后定：任务行操作兜底
      // 备份与恢复（backup-restore-operations-layer 规格 §9；严格 ops-only，深链 /admin/operations/backups）
      backups: {
        title: '备份与恢复', // 措辞后定：子层标题与分段控件 aria
        denied: '仅运维账号可访问备份与恢复', // 措辞后定：非 ops 深链 / 降权后拒绝态
        viewBackups: '备份', // 措辞后定
        viewRestores: '恢复', // 措辞后定
        viewPolicy: '策略', // 措辞后定
        loadError: '加载失败，请稍后重试', // 措辞后定
        actionError: '操作失败，请稍后重试', // 措辞后定
        maintenanceMode: '实例处于维护模式，请稍后重试', // 措辞后定：503 maintenance_mode
        // ---- 「备份」分段 ----
        createBackup: '一键备份', // 措辞后定
        backupCreated: (id: string) => `已创建备份 ${id}`, // 措辞后定：受理轻提示（含 backup_id）
        backupTableAria: '备份历史', // 措辞后定
        colBackupId: '备份 ID', // 措辞后定
        colBackupStatus: '状态', // 措辞后定
        colBackupCreatedAt: '创建时间', // 措辞后定
        colBackupCompletedAt: '完成时间', // 措辞后定
        colBackupRestorable: '可恢复', // 措辞后定
        colBackupActions: '操作', // 措辞后定
        backupStatus: (status: string) => {
          switch (status) {
            case 'creating':
              return '创建中';
            case 'complete':
              return '完成';
            case 'failed':
              return '失败';
            default:
              return '未知状态'; // 未知状态：通用兜底，不回显机读原串
          }
        }, // 措辞后定
        restorableYes: '可恢复', // 措辞后定
        restorableNo: '—', // 措辞后定：不可恢复不额外标注
        emptyBackups: '暂无备份', // 措辞后定
        detailExpand: '组成物', // 措辞后定：行内展开备份详情
        detailCollapse: '收起', // 措辞后定
        detailLoadError: '备份详情加载失败', // 措辞后定
        componentKind: (kind: string) => {
          switch (kind) {
            case 'postgres_snapshot':
              return '数据库快照';
            case 'object_store_snapshot':
              return '对象存储快照';
            case 'object_manifest':
              return '对象清单';
            default:
              return '组件'; // 未知 kind：通用兜底，不回显机读原串
          }
        }, // 措辞后定
        componentReference: (reference: string) => `快照引用 ${reference}`, // 措辞后定
        componentFailure: (reason: string) => `失败原因：${reason}`, // 措辞后定
        // ---- 「恢复」分段 ----
        sourceLabel: '来源备份', // 措辞后定：恢复来源选择框
        sourcePlaceholder: '选择可恢复的备份', // 措辞后定
        noRestorableSource: '暂无可恢复的备份', // 措辞后定
        startRestore: '发起恢复', // 措辞后定
        restoreDialogTitle: '发起实例恢复？', // 措辞后定
        restoreDialogBackup: (id: string) => `来源备份：${id}`, // 措辞后定：确认框内备份 ID 行
        restoreDialogStatus: (statusLabel: string) => `备份当前状态：${statusLabel}`, // 措辞后定
        restoreDialogImpact:
          '恢复期间实例进入维护模式：业务读写暂停，新建备份与策略修改暂不可用，恢复完成后自动解除。', // 措辞后定：确认框内维护模式影响说明
        restoreConfirm: '确认恢复', // 措辞后定
        restoreStarted: (id: string) => `已发起恢复 ${id}`, // 措辞后定：受理轻提示（含 restore_id）
        restoreInProgress: '已有进行中的恢复，已刷新记录', // 措辞后定：409 restore_in_progress
        backupNotRestorable: '该备份已不可恢复，已刷新列表', // 措辞后定：404 backup_not_found / 409 backup_not_restorable
        restoreTableAria: '恢复记录', // 措辞后定
        colRestoreId: '恢复 ID', // 措辞后定
        colRestoreBackup: '来源备份', // 措辞后定
        colRestoreStatus: '状态', // 措辞后定
        colRestoreCreatedAt: '发起时间', // 措辞后定
        colRestoreCompletedAt: '完成时间', // 措辞后定
        colRestoreActions: '操作', // 措辞后定
        restoreStatus: (status: string) => {
          switch (status) {
            case 'accepted':
              return '已受理';
            case 'running':
              return '恢复中';
            case 'blocked':
              return '待修复';
            case 'succeeded':
              return '已完成';
            case 'failed':
              return '已失败';
            default:
              return '未知状态';
          }
        }, // 措辞后定
        emptyRestores: '暂无恢复记录', // 措辞后定
        progressExpand: '进度', // 措辞后定：行内展开恢复进度
        stagesTitle: '恢复阶段', // 措辞后定
        stageLabel: (stage: string) => {
          switch (stage) {
            case 'postgres':
              return '数据库';
            case 'object_store':
              return '对象存储';
            case 'milvus':
              return '向量索引';
            case 'sparse':
              return '稀疏索引';
            case 'summary':
              return '摘要索引';
            case 'graph':
              return '图谱';
            case 'cache':
              return '缓存';
            default:
              return '阶段'; // 未知 stage：通用兜底，不回显机读原串
          }
        }, // 措辞后定
        stageStatus: (status: string) => {
          switch (status) {
            case 'pending':
              return '等待';
            case 'running':
              return '进行中';
            case 'succeeded':
              return '已完成';
            case 'failed':
              return '失败';
            default:
              return '未知';
          }
        }, // 措辞后定
        restoreFailure: (reason: string) => `失败原因：${reason}`, // 措辞后定
        repairTitle: '修复目标', // 措辞后定
        repairStatus: (status: string) => {
          switch (status) {
            case 'open':
              return '待处理';
            case 'succeeded':
              return '已修复';
            default:
              return '未知';
          }
        }, // 措辞后定
        repairFailure: (classification: string) => `失败分类：${classification}`, // 措辞后定
        repairRetry: '重试修复', // 措辞后定：open 修复目标行内按钮
        repairRetried: '已重新受理修复目标', // 措辞后定：202 轻提示
        repairNotOpen: '修复目标状态已变化，已刷新', // 措辞后定：404 / 409 repair_target_not_open
        // ---- 「策略」分段 ----
        policyEnabledLabel: '定时备份', // 措辞后定
        policyEnabledAria: '定时备份开关', // 措辞后定
        policyFrequencyLabel: '周期', // 措辞后定
        policyFrequencyDaily: '每天', // 措辞后定
        policyFrequencyWeekly: '每周', // 措辞后定
        policyFrequencyAria: '备份周期', // 措辞后定
        policyLocalTimeLabel: '本地时间', // 措辞后定
        policyLocalTimeInvalid: '请输入 HH:MM 格式的时间', // 措辞后定：客户端校验
        policyWeekdaysLabel: '星期', // 措辞后定：weekly 多选
        policyWeekday: (value: number) => {
          switch (value) {
            case 0:
              return '周一';
            case 1:
              return '周二';
            case 2:
              return '周三';
            case 3:
              return '周四';
            case 4:
              return '周五';
            case 5:
              return '周六';
            case 6:
              return '周日';
            default:
              return '—';
          }
        }, // 措辞后定（0=周一 … 6=周日，与后端 date.weekday() 一致）
        policyWeekdaysRequired: '每周周期至少选择一天', // 措辞后定：客户端校验
        policyTimezoneLabel: '时区', // 措辞后定
        policyTimezonePlaceholder: '如 Asia/Shanghai', // 措辞后定
        policyTimezoneInvalid: '时区无效，请输入 IANA 时区名', // 措辞后定：422 validation_error（field=timezone）
        policyKeepLastLabel: '保留最近份数', // 措辞后定
        policyRetentionDaysLabel: '保留天数', // 措辞后定
        policyPositiveInteger: '请输入正整数', // 措辞后定：keep_last / retention_days 客户端校验
        // 规格 §5 保护式 AND：页面固定说明，不得省略
        policyRetentionNote: '两项同时满足才会清理：备份超过保留天数，且不在最近保留份数之内。', // 措辞后定
        policyNextRun: (value: string) => `下次执行：${value}`, // 措辞后定
        policyNextRunDisabled: '定时备份未启用', // 措辞后定：next_run_at 为 null
        policyLastScheduled: (value: string) => `上次排程：${value}`, // 措辞后定
        policyLastOutcome: (outcome: string) => {
          switch (outcome) {
            case 'succeeded':
              return '成功';
            case 'skipped':
              return '跳过';
            case 'failed':
              return '失败';
            default:
              return outcome; // 运行事实机读值原样回显
          }
        }, // 措辞后定
        policyVersion: (version: number) => `策略版本 ${version}`, // 措辞后定
        policySave: '保存', // 措辞后定
        policySaved: '策略已保存', // 措辞后定
        policyVersionConflict: '策略已被其他操作修改，已刷新最新值，请确认后重试', // 措辞后定：409 version_conflict
        policyValidationError: '输入不符合要求，请检查后重试', // 措辞后定：422 validation_error
      },
    },
    // 用户管理（§12.1–12.4；ops 与 admin 共用列表，写能力按角色收窄）
    users: {
      searchPlaceholder: '搜索用户', // 措辞后定
      departmentFilter: '部门', // 措辞后定
      roleFilter: '角色', // 措辞后定
      allDepartments: '全部部门', // 措辞后定
      allRoles: '全部角色', // 措辞后定
      addUser: '新增用户', // 措辞后定
      edit: '编辑', // 措辞后定
      disable: '永久禁用', // 措辞后定
      colUsername: '用户名', // 措辞后定
      colRealName: '姓名', // 措辞后定
      colDepartment: '部门', // 措辞后定
      colRole: '角色', // 措辞后定
      colLastActive: '最近活跃', // 措辞后定
      colActions: '操作', // 措辞后定
      noDepartment: '—', // 措辞后定：无部门
      purgeAfter: (date: string) => `将于 ${date} 清理`, // 措辞后定：pending_delete 行
      clearFilters: '清除条件', // 措辞后定：空态附带的筛选重置文字链
      departments: '部门管理', // 措辞后定：下钻入口
      matrixTitle: '权限矩阵', // 措辞后定
      matrixNote: '权限矩阵固定，修改走版本控制和部署配置变更', // 措辞后定
      addDialogTitle: '新增用户', // 措辞后定
      editDialogTitle: '编辑用户', // 措辞后定
      save: '保存', // 措辞后定
      displayNameLabel: '显示名', // 措辞后定
      displayNamePlaceholder: '缺省同姓名', // 措辞后定
      noDepartmentOption: '无部门', // 措辞后定：部门下拉项
      // 原部门已不在 active 目录（已停用）：下拉以只读禁用项呈现原部门，不静默改写
      departmentInactiveOption: (name: string) => `${name}（已停用）`, // 措辞后定
      directoryLoadError: '部门目录加载失败', // 措辞后定：对话框内目录重试行
      passwordLabel: '初始密码', // 措辞后定
      passwordOfflineNote: '初始密码由管理员线下传达', // 措辞后定
      passwordInvalid: '密码至少 8 位且需字母与数字混合', // 措辞后定
      fieldRequired: '该字段必填', // 措辞后定
      usernameExists: '用户名已存在', // 措辞后定：409 username_exists
      ministerDepartmentRequired: '部长必须绑定一个在用部门', // 措辞后定：422
      // 措辞后定：409 department_inactive / 404 department_not_found
      departmentChanged: '可选部门已变化，已刷新部门目录，请重新确认',
      sessionRevokedNote: '保存角色或部门后，该用户全部设备的会话将被撤销', // 措辞后定
      userPendingDelete: '该账号已冻结，已刷新列表', // 措辞后定：409 user_pending_delete
      forbiddenTarget: '无权对该账号执行此操作', // 措辞后定：403 forbidden_target
      cannotModifySelf: '不可对自己的账号执行此操作', // 措辞后定：403 cannot_modify_self
      disableDialogTitle: '永久禁用账号', // 措辞后定
      disablePoint1: '账号立即永久冻结并退出全部会话，操作不可恢复', // 措辞后定：固定说明一
      disablePoint2: '其个人库文档与聊天会话将归档，并在保留期届满后由系统清理', // 措辞后定：固定说明二
      disablePoint3: '其共享到部门库与公共库的文档、文件和索引不受影响', // 措辞后定：固定说明三
      disableConfirm: '确认永久禁用', // 措辞后定
      empty: '暂无用户', // 措辞后定
      loadError: '用户加载失败，请稍后重试', // 措辞后定
      actionError: '操作失败，请稍后重试', // 措辞后定
      versionConflict: '内容已变化，已刷新，请确认后重试', // 措辞后定
    },
    // 部门管理（§12.5，仅超管写；目录读接口 ops 亦可）
    departments: {
      filterActive: '在用', // 措辞后定
      filterInactive: '已停用', // 措辞后定
      filterAll: '全部', // 措辞后定
      add: '新增部门', // 措辞后定
      rename: '改名', // 措辞后定
      deactivate: '停用', // 措辞后定
      noActions: '—', // 措辞后定：行无可用操作
      colName: '部门名', // 措辞后定
      colStatus: '状态', // 措辞后定
      colMembers: '成员', // 措辞后定
      colDocuments: '文档', // 措辞后定
      colTasks: '进行中任务', // 措辞后定
      colSubmissions: '待审投稿', // 措辞后定
      colDeactivatedAt: '停用时间', // 措辞后定
      colActions: '操作', // 措辞后定
      statusActive: '在用', // 措辞后定
      statusInactive: '已停用', // 措辞后定
      addDialogTitle: '新增部门', // 措辞后定
      renameDialogTitle: '重命名部门', // 措辞后定
      deactivateDialogTitle: '停用部门', // 措辞后定
      deactivateConfirm: '确认停用', // 措辞后定
      deactivatePoint1: '停用后部门与部门库转为只读，仅运维与超管可见，新成员归属、上传、任务与投稿均被拒绝', // 措辞后定
      deactivatePoint2: '部门记录、文档、索引与历史不删除，名称保留且不可复用', // 措辞后定
      deactivatePoint3: '仍有成员、进行中任务或待审投稿时无法停用', // 措辞后定
      deactivateCounts: (members: number, jobs: number, submissions: number) =>
        `成员 ${members} · 进行中任务 ${jobs} · 待审投稿 ${submissions}`, // 措辞后定：停用确认内计数行
      nameLabel: '部门名称', // 措辞后定
      nameNote: '名称规范化后唯一，在用与已停用部门均不可重名', // 措辞后定：新增框下说明行
      nameRequired: '请输入部门名称', // 措辞后定
      nameExists: '部门名称已存在', // 措辞后定：409 department_name_exists
      validationError: '名称不符合要求，请检查后重试', // 措辞后定：422 validation_error
      actionForbidden: '当前账号无权执行部门操作，已刷新目录', // 措辞后定：403 department_action_forbidden
      blockedHasMembers: '部门内仍有成员，无法停用；请先在用户管理中调整归属', // 措辞后定：409 department_has_members
      blockedHasWork: '部门仍有进行中任务或待审投稿，处理完成后再试', // 措辞后定：409 department_has_active_work
      unverified: '系统暂时无法确认停用前置状态，请稍后重试', // 措辞后定：503 可重试
      statusChanged: '部门状态已变化，已刷新目录', // 措辞后定：404 department_not_found / 409 department_inactive
      versionConflict: '内容已变化，已刷新，请确认后重试', // 措辞后定
      actionError: '操作失败，请稍后重试', // 措辞后定
      empty: '暂无部门', // 措辞后定
      loadError: '部门加载失败，请稍后重试', // 措辞后定
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
      retrievalRouted: '正在整理检索结果', // 措辞后定
      retrievingAgain: '正在重新检索', // 措辞后定
      rewriting: '正在改写问题', // 措辞后定
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
      actionFailed: '操作失败，请稍后重试', // 措辞后定：会话/分组 patch·delete 失败轻提示（store 返回 false 时）
      untitledConversation: '新会话', // 措辞后定：新会话首条消息生成标题前的默认列表标题
      sectionPinned: '置顶', // 措辞后定
      sectionToday: '今天', // 措辞后定
      sectionWeek: '本周', // 措辞后定
      sectionEarlier: '更早', // 措辞后定
      menuRename: '重命名', // 措辞后定
      menuPin: '置顶', // 措辞后定
      menuUnpin: '取消置顶', // 措辞后定
      menuMoveToGroup: '移入分组', // 措辞后定
      menuMoveOutOfGroup: '移出分组', // 措辞后定：仅分组内会话显示，移出后按最后对话时间回落默认列表
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
      effortAria: '思考深度',
      effortQuick: '快速', // 措辞后定
      effortThink: '思考', // 措辞后定
      effortDeep: '深度研究', // 措辞后定
      scopeAria: '检索范围', // 措辞后定
      scopeLabel: '检索范围', // 措辞后定：「+」菜单行可见文案
      scopeAll: '全部范围', // 措辞后定
      scopeSearchPlaceholder: '搜索空间', // 措辞后定
      scopePersonalDocuments: '个人库文档', // 措辞后定
      scopeDocumentSearchPlaceholder: '搜索文档', // 措辞后定
      scopeDocumentDrillAria: '展开个人库文档', // 措辞后定
      sendAria: '发送', // 措辞后定
      stopAria: '停止生成', // 措辞后定
      stoppingAria: '正在停止', // 措辞后定
      inputPlaceholder: '输入你的问题…', // 措辞后定
      // 「+」菜单：附件与技能（动效 AI Agent Input）
      addMenuAria: '添加附件或技能', // 措辞后定
      addPhotos: '添加图片', // 措辞后定
      attachFiles: '添加文件', // 措辞后定
      skillsLabel: '技能', // 措辞后定
      skillDeepResearch: '深度研究', // 措辞后定
      skillCodeReview: '代码评审', // 措辞后定
      skillWebSearch: '联网搜索', // 措辞后定
      skillSummarize: '总结', // 措辞后定
      noMatchingSkills: '没有匹配的技能', // 措辞后定
      removeItemAria: (name: string) => `移除 ${name}`, // 措辞后定
      // 输入优化（prompt-enhance §3：药丸/动效为既有行为，这里只留文案）
      enhancePrompt: '优化输入', // 措辞后定
      revertEnhance: '还原', // 措辞后定
      enhancingAria: '正在优化输入', // 措辞后定
      enhanceFailed: '优化失败，请稍后重试', // 措辞后定：非中止失败轻提示（原文不动，可重试）
    },

    // 消息区（共用基座 §3.4）
    message: {
      errorLine: '回答生成失败', // 措辞后定
      retry: '重试', // 措辞后定
      retryAttempt: '重试', // 措辞后定：重试链后继消息上方说明
      scrollToBottom: '回到底部', // 措辞后定
      citeFrom: (name: string) => `引自《${name}》`, // 措辞后定：文档名（Citation.document_name）
      citeFromFallback: '引自文档', // 措辞后定：document_name 缺失时的通用措辞（不显示不透明 ID）
      citePage: (page: number) => `第 ${page} 页`, // 措辞后定（span 是预览页内部消歧数据，不进用户文案）
      citeSection: (path: readonly string[], paragraph?: number) =>
        `${path.join(' / ')}${paragraph !== undefined ? ` 第 ${paragraph} 段` : ''}`, // 措辞后定
      citeSheet: (sheet: string, range: string) => `${sheet} ${range}`, // 措辞后定
      citeUnavailable: '内容已不可用', // 措辞后定
      citeOpenAria: '打开引用预览', // 措辞后定
      timeAria: '消息时间', // 措辞后定
    },
  },
  // 原文预览页（fe-doc-preview；共用基座 §6）：独立窗口页，/preview/:document_id
  preview: {
    closeAria: '关闭', // 措辞后定
    unavailable: '内容已不可用', // 措辞后定：文档删除 / 版本 purging/purged / 无权限，不泄露任何元数据
    error: '预览加载失败，请稍后重试', // 措辞后定
    retry: '重试', // 措辞后定
    loadingAria: '正在加载预览', // 措辞后定
    navAria: '命中导航', // 措辞后定
    navTitle: (count: number) => `命中点 ${count}`, // 措辞后定：窄屏收起按钮与面板标题
    navEmpty: '暂无命中点', // 措辞后定：无 message_id 管理侧只读形态 / 错误态导航空态
    hitLocatorPage: (page: number) => `第 ${page} 页`, // 措辞后定
    hitLocatorSection: (path: readonly string[], paragraph?: number) =>
      `${path.join(' / ')}${paragraph !== undefined ? ` 第 ${paragraph} 段` : ''}`, // 措辞后定
    hitLocatorSheet: (sheet: string, range: string) => `${sheet} ${range}`, // 措辞后定
    sheetTabsAria: '工作表', // 措辞后定：Sheet 页签分段开关
    // 载体类型标签（页头文档名下方 14px ash-gray；未知载体回退通用「文档」）
    mediaKind: {
      pdf: 'PDF 文档', // 措辞后定
      word: 'Word 文档', // 措辞后定
      md: 'Markdown 文档', // 措辞后定
      txt: '文本文件', // 措辞后定
      excel: '表格文档', // 措辞后定
      csv: 'CSV 表格', // 措辞后定
      image: '图片', // 措辞后定
      code: '代码文件', // 措辞后定
      data: '数据文件', // 措辞后定
      fallback: '文档', // 措辞后定
    },
  },
} as const;

export type Copy = typeof zhCN;
