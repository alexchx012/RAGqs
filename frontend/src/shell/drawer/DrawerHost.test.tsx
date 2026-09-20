/*
 * 全屏抽屉宿主集成测试（fe-shared-shell 规格 §1–§3、§7；共用基座 §5.1–§5.2）。
 * 经 AppRoutes 整树渲染（真实 URL 驱动）：开合、深链恢复、未注册层占位、按角色左栏、
 * 跨段切换、下钻与返回、Esc 逐层、关闭按钮、下滑手势、管理段顶层自动选中总览、窄屏单栏化、
 * prefers-reduced-motion 降级。
 * 动画计时器走真实时钟（进入 400ms / 关闭 400ms / 下钻两相 500ms），断言最终稳定状态。
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import type { AdminApi } from '../../admin/api';
import { copy } from '../../copy';
import { AppRoutes } from '../../router/AppRoutes';
import type { SettingsApi } from '../../settings/api';
import { createAuthedStore, fakeAdminApi, renderWithShell, testUser } from '../../test/auth-fixtures';

const drawerCopy = copy.shell.drawer;
const modules = drawerCopy.modules;

type TestRole = 'user' | 'minister' | 'ops' | 'admin';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

/** §6.9 版本记录响应（含 1 条 active 版本，供参数化下钻层的参数生效断言使用）。 */
function versionsResponse(documentId: string) {
  return {
    document_id: documentId,
    version: 2,
    active_version_id: `dv_${documentId}_2`,
    items: [
      {
        document_version_id: `dv_${documentId}_2`,
        version_number: 2,
        status: 'active',
        created_at: '2026-07-20T10:00:00Z',
        activated_at: '2026-07-20T10:00:00Z',
        terminal_at: null,
        superseded_at: null,
        purge_after_at: null,
        purged_at: null,
        restored_from_version_id: null,
        content_available: true,
      },
    ],
  };
}

/** 文档列表中的一行（文档行「⋯」→ 版本 是参数化下钻层的真实入口）。 */
const SAMPLE_DOC = {
  id: 'doc_1',
  document_version_id: 'dv_1',
  version: 1,
  name: '员工手册.pdf',
  media_kind: 'pdf',
  version_status: 'active',
  active_operation: null,
  uploaded_at: '2026-07-20T02:00:00Z',
  usage: { pages: 50, images: 40 },
};

/** 知识库下钻所需的 settings api 子集：manage 权限的个人库或部门库 + 一行文档 + 版本记录。 */
function knowledgeApi(
  listVersions: (documentId: string) => Promise<unknown>,
  kind: 'personal' | 'department' = 'personal',
): SettingsApi {
  const space =
    kind === 'personal'
      ? { id: 'personal:u_1', kind: 'personal', name: '个人库', permission: 'manage', document_count: 1 }
      : { id: 'department:d_1', kind: 'department', name: '财务部', permission: 'manage', document_count: 1 };
  return {
    listUploadSpaces: vi.fn(async () => ({ items: [space] })),
    listManageSpaces: vi.fn(async () => ({ items: [space] })),
    listApprovals: vi.fn(async () => ({ items: [] })),
    listDocuments: vi.fn(async () => ({ items: [SAMPLE_DOC], total: 1, page: 1, page_size: 20 })),
    listVersions,
  } as unknown as SettingsApi;
}

/** 左栏内与左栏外（右栏内容列）各挂几个该类名的元素。 */
function splitByColumn(dialog: HTMLElement, selector: string): [number, number] {
  const nav = dialog.querySelector('nav');
  const nodes = Array.from(dialog.querySelectorAll(selector));
  const inNav = nodes.filter((node) => nav?.contains(node) === true).length;
  return [inNav, nodes.length - inNav];
}

async function renderApp(
  path: string,
  role: TestRole = 'user',
  options: { adminApi?: AdminApi; settingsApi?: SettingsApi } = {},
) {
  const store = await createAuthedStore(testUser({ role }));
  renderWithShell(
    <>
      <AppRoutes />
      <LocationProbe />
    </>,
    store,
    [path],
    { adminApi: options.adminApi, settingsApi: options.settingsApi },
  );
  return screen.getByTestId('location-path');
}

describe('抽屉开合与 URL 同步', () => {
  it('/settings 打开抽屉到个人段顶层：段标签、四模块、顶层占位', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.personalSegmentLabel)).toBeInTheDocument();
    for (const name of [modules.general, modules.account, modules.security, modules.knowledge]) {
      expect(within(dialog).getByRole('button', { name })).toBeInTheDocument();
    }
    expect(within(dialog).getByText(drawerCopy.topPlaceholderBody)).toBeInTheDocument();
    // 普通用户无管理段
    expect(within(dialog).queryByText(drawerCopy.adminSegmentLabel)).not.toBeInTheDocument();
  });

  it('/ 与未知路径不打开抽屉', async () => {
    await renderApp('/');
    await screen.findByLabelText(copy.chat.composer.inputPlaceholder);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('深链 /settings/knowledge/uploads 刷新式恢复到对应层', async () => {
    await renderApp('/settings/knowledge/uploads');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 左栏第一位为当前层名，上方为返回上一层按钮
    expect(within(dialog).getByText(modules.uploads)).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    ).toBeInTheDocument();
    // uploads 层渲染真实上传结果内容（空态；fake api 无任务）
    expect(
      await within(dialog).findByText(copy.settings.knowledge.uploads.empty),
    ).toBeInTheDocument();
  });

  it('未注册层深链落抽屉首层占位（规格 §3）', async () => {
    await renderApp('/settings/no-such-module');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.topPlaceholderBody)).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: modules.knowledge }),
    ).toBeInTheDocument();
  });

  it('运维访问管理段顶层 /admin 自动选中「总览」（/admin/dashboard）', async () => {
    const probe = await renderApp('/admin', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    expect(dialog).toBeInTheDocument();
    await waitFor(() => expect(probe.textContent).toBe('/admin/dashboard'));
  });

  it('普通用户访问 /admin 时回到可访问路径且不保留抽屉', async () => {
    const probe = await renderApp('/admin');

    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('普通用户访问 /admin/* 深链时回到可访问路径且不保留抽屉', async () => {
    const probe = await renderApp('/admin/users');

    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('抽屉作用域挂载与左栏新显示名（drawer-visual-system）', () => {
  it('个人段左栏按新显示名与顺序渲染，且不出现旧名', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog');
    const nav = dialog.querySelector('nav') as HTMLElement;
    const labels = Array.from(nav.querySelectorAll('button')).map((b) => b.textContent ?? '');
    expect(labels.slice(0, 4)).toEqual(['常规设置', '账号设置', '安全设置', '知识库']);
    expect(nav.textContent).not.toContain('外观');
    expect(nav.textContent).not.toContain('个人资料');
  });

  it('抽屉根容器挂载作用域属性，左栏胶囊为 200×40 圆角 8px', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog');
    const scoped = dialog.querySelector('[data-drawer-scope]');
    expect(scoped).not.toBeNull();
    const active = Array.from(dialog.querySelectorAll('nav button')).find((b) =>
      (b.className ?? '').includes('bg-mist-gray'),
    ) as HTMLElement;
    expect(active.className).toContain('w-[200px]');
    expect(active.className).toContain('h-10');
    expect(active.className).toContain('rounded-[var(--radius-buttons)]');
    expect(active.className).toContain('ml-6');
  });

  it('抽屉内打开的 Radix 浮层根节点自带作用域属性（浮层不走 DOM 继承）', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    await renderApp('/settings/knowledge', 'user', { settingsApi: knowledgeApi(listVersions) });
    const dialog = await screen.findByRole('dialog');
    const user = userEvent.setup();
    // 文档行「⋯」菜单：Radix DropdownMenu 默认 portal 到 document.body
    await user.click(
      within(dialog).getByRole('button', {
        name: copy.settings.knowledge.documents.rowMenuAria(SAMPLE_DOC.name),
      }),
    );
    const menu = await screen.findByRole('menu');
    expect(menu.closest('[data-drawer-scope]')).not.toBeNull();
    // 菜单入口打开的删除确认框：Radix Dialog 同样 portal 到 document.body
    await user.click(
      within(menu).getByRole('menuitem', { name: copy.settings.knowledge.documents.delete }),
    );
    const confirm = await screen.findByRole('dialog', {
      name: copy.settings.knowledge.documents.deleteConfirmTitle,
    });
    expect(confirm.closest('[data-drawer-scope]')).not.toBeNull();
  });

  it('内容区改抽屉画布底色且不限宽：880px 卡片居中只由卡片自身负责', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog');
    const nav = dialog.querySelector('nav') as HTMLElement;
    // 左栏保持纸白，右栏内容区改用抽屉作用域画布底色（--surface-drawer-canvas 只在作用域内定义）
    expect(nav.className).toContain('bg-paper-white');
    const pane = nav.nextElementSibling as HTMLElement;
    expect(pane.className).toContain('bg-[var(--surface-drawer-canvas)]');
    // 内容区不得再引入第二层 max-width / 居中容器：否则 880px 卡片会被双重收缩
    expect(pane.className).not.toContain('max-w-');
    expect(pane.className).not.toContain('mx-auto');
  });

  it('两栏行无栏间 gap 与桌面横向留白：内容区紧贴左栏右缘，窄屏保留 px-5', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog');
    const nav = dialog.querySelector('nav') as HTMLElement;
    const row = nav.parentElement as HTMLElement;
    // 设计图几何：内容区紧跟左栏右缘（1440 视口下自 x=240 起、到 x=1440 止，宽 1200），
    // 栏间无 gap、桌面无页面横向留白；880px 卡片居中 → 左缘 240 + (1200 − 880) / 2 = 400。
    expect(row.className).not.toContain('gap-');
    expect(row.className).not.toContain('md:px-10');
    // 窄屏（<768px）单栏化仍保留必需留白，桌面断点归零
    expect(row.className).toContain('px-5 md:px-0');
    // 左栏仍固定 240px；胶囊仍 200×40 + 24px 左缩进
    expect(nav.className).toContain('w-60');
    const firstItem = nav.querySelector('button') as HTMLElement;
    expect(firstItem.className).toContain('ml-6');
    expect(firstItem.className).toContain('w-[200px]');
    // 页头保留自己的内缩（与左栏胶囊的 24px 不是同一套值，不得被本次对齐带走）
    const header = dialog.querySelector('header') as HTMLElement;
    expect(header.className).toContain('px-5');
    expect(header.className).toContain('md:px-10');
  });

  it('抽屉内打开的管理段筛选浮层同样自带作用域属性（UsersModule Popover）', async () => {
    await renderApp('/admin/users', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.usersOps });
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole('button', { name: copy.admin.users.departmentFilter }));
    const group = await screen.findByRole('radiogroup', { name: copy.admin.users.departmentFilter });
    expect(group.closest('[data-drawer-scope]')).not.toBeNull();
  });
});

describe('左栏按角色渲染与跨段切换', () => {
  it('运维：左栏渲染管理段六模块；从个人段点管理模块切到管理段', async () => {
    const probe = await renderApp('/settings', 'ops');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.adminSegmentLabel)).toBeInTheDocument();
    for (const name of [
      modules.dashboard,
      modules.approvals,
      modules.spaces,
      modules.evaluation,
      modules.operations,
      modules.usersOps,
    ]) {
      expect(within(dialog).getByRole('button', { name })).toBeInTheDocument();
    }
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole('button', { name: modules.dashboard }));
    // 跨段导航到 /admin/dashboard，页级标题切换为当前模块名
    await screen.findByRole('dialog', { name: modules.dashboard });
    expect(probe.textContent).toBe('/admin/dashboard');
  });

  it('超管：管理段无审批中心，用户模块名为「人员与权限」', async () => {
    await renderApp('/settings', 'admin');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).queryByRole('button', { name: modules.approvals })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: modules.usersAdmin })).toBeInTheDocument();
  });
});

describe('下钻、返回与 Esc 逐层', () => {
  it('点模块下钻到知识库内容，再经模块内入口下钻到「我的投稿」；返回按钮逐层回退', async () => {
    const probe = await renderApp('/settings');
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
    // 知识库层：渲染真实模块内容（配额计数器 + 我的投稿入口）
    expect(await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge');
    // 顶层→模块按 §5.2 播同层切换序列（from 原地淡出 150ms 后卸载，再接 to 自下而上淡入 250ms）；
    // 等 from 侧占位文案卸载后再抓下钻入口，避免真实时钟负载下点击落在已卸载节点上
    await waitFor(() =>
      expect(within(dialog).queryByText(drawerCopy.topPlaceholderBody)).not.toBeInTheDocument(),
    );
    await user.click(within(dialog).getByRole('button', { name: modules.submissions }));
    expect(await within(dialog).findByText(copy.settings.knowledge.submissions.title)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge/submissions');
    // 同理：两相下钻动画（250+250ms）结束、from 侧知识库内容卸载后再抓返回按钮
    await waitFor(() =>
      expect(
        within(dialog).queryByText(copy.settings.knowledge.uploads.historyEntry),
      ).not.toBeInTheDocument(),
    );
    // 返回按钮回到知识库层
    await user.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    expect(await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge');
  });

  it('下钻为两相整页过渡：离开相两栏同上滑渐隐，进入相两栏同自下渐显，页头不参与', async () => {
    const probe = await renderApp('/settings/knowledge');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 同步触发，抓住离开相（真实计时器 250ms 内的中间态）
    fireEvent.click(within(dialog).getByRole('button', { name: modules.submissions }));
    expect(probe.textContent).toBe('/settings/knowledge/submissions');
    // 离开相：左栏与右栏各挂一个上滑渐隐类（同一提交 → 同帧起步，视觉上等价于整页位移）
    expect(splitByColumn(dialog, '.drill-page-leave-up')).toEqual([1, 1]);
    // 进入相尚未开始：新页两栏都还没挂渐显类（否则动画会在 drill-hidden 期间跑完）
    expect(dialog.querySelectorAll('.drill-page-arrive-from-below')).toHaveLength(0);
    // 页头（关闭按钮 + 页级标题 + 铃铛）不参与整页过渡
    expect(dialog.querySelector('header')?.querySelector('[class*="drill-page-"]')).toBeNull();
    // FLIP 飞字效果整体缺席
    expect(document.querySelector('.drill-flip-clone')).toBeNull();
    // 进入相：新页两栏各挂一个自下渐显类
    await waitFor(() => expect(splitByColumn(dialog, '.drill-page-arrive-from-below')).toEqual([1, 1]));
    // 收尾后回到稳态：不留任何过渡标记
    await waitFor(() => expect(dialog.querySelector('[class*="drill-page-"]')).toBeNull());
  });

  it('返回为反向镜像：离开相下滑渐隐，进入相自上方落位', async () => {
    const probe = await renderApp('/settings/knowledge/submissions');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    expect(probe.textContent).toBe('/settings/knowledge');
    expect(dialog.querySelectorAll('.drill-page-leave-down')).toHaveLength(2);
    // 镜像方向不串台：离开相不带任何进入类
    expect(dialog.querySelector('.drill-page-arrive-from-above')).toBeNull();
    expect(dialog.querySelector('[class*="arrive-from-below"]')).toBeNull();
    await waitFor(() =>
      expect(dialog.querySelectorAll('.drill-page-arrive-from-above')).toHaveLength(2),
    );
    await waitFor(() => expect(dialog.querySelector('[class*="drill-page-"]')).toBeNull());
  });

  it('窄屏首屏下钻：离开相滑隐的是屏幕上的模块名列表本身，而非占位文案', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('max-width'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings');
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      // 窄屏首屏：模块名列表渲染在内容列（单栏化）
      expect(dialog.querySelector('[data-nav-variant="modules"]')).not.toBeNull();
      fireEvent.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      expect(probe.textContent).toBe('/settings/knowledge');
      const leaving = Array.from(dialog.querySelectorAll('.drill-page-leave-up'));
      expect(leaving).toHaveLength(2);
      // 两侧离开节点都是真实模块列表（与窄屏 idle 同一形态），不是从未露面的顶层占位文案
      for (const node of leaving) {
        expect(node.getAttribute('data-nav-variant')).toBe('modules');
        expect(node.textContent).not.toContain(drawerCopy.topPlaceholderBody);
      }
    } finally {
      window.matchMedia = original;
    }
  });

  it('离开相渲染的上层页，其返回按钮指向该层自己的上一层（深层下钻不串层名）', async () => {
    await renderApp('/settings/knowledge/manage', 'minister');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    fireEvent.click(
      within(dialog).getByRole('button', {
        name: new RegExp(`^${copy.settings.knowledge.manage.approvals}`),
      }),
    );
    // 离开相（部门库管理）的返回按钮回「知识库」；到达相（投稿审核）的返回按钮回「部门库管理」。
    // 到达相在离开相以 drill-hidden 预挂载，故查询带上 hidden: true。
    const backNames = within(dialog)
      .queryAllByRole('button', { hidden: true })
      .map((node) => node.getAttribute('aria-label'))
      .filter((label): label is string => label !== null && label.startsWith('返回'));
    expect(backNames).toContain(drawerCopy.backAria(modules.knowledge));
    expect(backNames).toContain(drawerCopy.backAria(copy.settings.knowledge.manage.title));
  });

  it('运维在知识库层不渲染「我的投稿」（无权限模块不渲染）', async () => {
    await renderApp('/settings/knowledge', 'ops');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry)).toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: modules.submissions }),
    ).not.toBeInTheDocument();
  });

  it('Esc 逐层向上：下钻层 → 上一层 → 抽屉顶层 → 关闭抽屉', async () => {
    const probe = await renderApp('/settings/knowledge/uploads');
    const user = userEvent.setup();
    await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/settings/knowledge'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/settings'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/'));
    // 关闭动画（400ms --duration-slow）后抽屉卸载
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
  });

  it('版本记录（参数化下钻层）返回按钮直达知识库层：不落参数缺失的空版本记录层', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    const probe = await renderApp('/settings/knowledge/versions/docA', 'user', {
      settingsApi: { listVersions } as unknown as SettingsApi,
    });
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 参数生效：本层渲染 docA 的版本记录
    expect(
      await within(dialog).findByText(copy.settings.knowledge.versions.versionNumber(2)),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    // 修复前停 /settings/knowledge/versions（无 documentId → 空态「暂无版本记录」），须再点一次
    await waitFor(() => expect(probe.textContent).toBe('/settings/knowledge'));
    expect(
      await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(copy.settings.knowledge.versions.empty)).toBeNull();
  });

  it('版本记录（参数化下钻层）Esc 逐层向上同样直达知识库层', async () => {
    const probe = await renderApp('/settings/knowledge/versions/docA');
    const user = userEvent.setup();
    await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/settings/knowledge'));
  });

  it('离开版本记录层：退出动画期间离开侧仍是版本详情，不被清成空态', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    await renderApp('/settings/knowledge/versions/docA', 'user', {
      settingsApi: { listVersions } as unknown as SettingsApi,
    });
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(
      await within(dialog).findByText(copy.settings.knowledge.versions.versionNumber(2)),
    ).toBeInTheDocument();
    // 同步派发（不等动画推进）：断言退出相位（250ms）内的离开侧内容
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    expect(
      within(dialog).getByText(copy.settings.knowledge.versions.versionNumber(2)),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(copy.settings.knowledge.versions.empty)).toBeNull();
  });

  it('参数化下钻层返回为整页下滑过渡：左栏不再瞬时切换（离开相两栏同挂下滑类）', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    const probe = await renderApp('/settings/knowledge/versions/docA', 'user', {
      settingsApi: knowledgeApi(listVersions),
    });
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 等参数生效（本层渲染 docA 的版本记录）后再触发返回
    expect(
      await within(dialog).findByText(copy.settings.knowledge.versions.versionNumber(2)),
    ).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    expect(probe.textContent).toBe('/settings/knowledge');
    // 修复前：过渡按 URL 段数判定，3 段 → 1 段既非下钻也非返回，落到同层切换
    // （drill-exit 只挂右栏、左栏原地瞬换）；修复后：按层链深度判定（2 层 → 1 层）= 整页返回。
    expect(dialog.querySelectorAll('.drill-exit')).toHaveLength(0);
    expect(splitByColumn(dialog, '.drill-page-leave-down')).toEqual([1, 1]);
    await waitFor(() =>
      expect(splitByColumn(dialog, '.drill-page-arrive-from-above')).toEqual([1, 1]),
    );
    await waitFor(() => expect(dialog.querySelector('[class*="drill-page-"]')).toBeNull());
  });

  it('进入参数化下钻层为整页上滑下钻：左栏也随之上滑渐隐（不再瞬时换栏）', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    const probe = await renderApp('/settings/knowledge', 'user', {
      settingsApi: knowledgeApi(listVersions),
    });
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 真实入口：文档行「⋯」→ 版本（列表来自 manage 权限的个人库）
    await user.click(
      within(dialog).getByRole('button', {
        name: copy.settings.knowledge.documents.rowMenuAria(SAMPLE_DOC.name),
      }),
    );
    // 菜单 portal 到 body，不经 dialog 查询；同步派发选中以抓住离开相（真实计时器 250ms 内）
    fireEvent.click(
      await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.versions }),
    );
    expect(probe.textContent).toBe('/settings/knowledge/versions/doc_1');
    expect(listVersions).toHaveBeenCalledWith('doc_1');
    // 修复前：同层切换（左栏无动画类、右栏 drill-exit）；修复后：左栏（模块列表）与右栏同挂上滑渐隐类
    expect(dialog.querySelectorAll('.drill-exit')).toHaveLength(0);
    expect(splitByColumn(dialog, '.drill-page-leave-up')).toEqual([1, 1]);
    await waitFor(() =>
      expect(splitByColumn(dialog, '.drill-page-arrive-from-below')).toEqual([1, 1]),
    );
    await waitFor(() => expect(dialog.querySelector('[class*="drill-page-"]')).toBeNull());
  });

  it('层链深度不变的横向切换仍是同层切换：部门库管理 → 版本记录不升级为整页过渡', async () => {
    const listVersions = vi.fn(async (documentId: string) => versionsResponse(documentId));
    const probe = await renderApp('/settings/knowledge/manage', 'minister', {
      settingsApi: knowledgeApi(listVersions, 'department'),
    });
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(
      within(dialog).getByRole('button', {
        name: copy.settings.knowledge.documents.rowMenuAria(SAMPLE_DOC.name),
      }),
    );
    fireEvent.click(
      await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.versions }),
    );
    expect(probe.textContent).toBe('/settings/knowledge/versions/doc_1');
    // 深度 2 → 2：只动右栏内容（from 侧右栏 drill-exit），两栏都不得挂整页过渡类
    expect(dialog.querySelectorAll('[class*="drill-page-"]')).toHaveLength(0);
    expect(splitByColumn(dialog, '.drill-exit')).toEqual([0, 1]);
  });

  it('左上角关闭按钮关闭抽屉并回到聊天主页', async () => {
    const probe = await renderApp('/settings/knowledge');
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(within(dialog).getByRole('button', { name: drawerCopy.closeAria }));
    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
    // 聊天主页在抽屉下方保持挂载，关闭后立即呈现（输入区在场）
    expect(screen.getByLabelText(copy.chat.composer.inputPlaceholder)).toBeInTheDocument();
  });

  it('全屏抽屉圈定键盘焦点，并在关闭后恢复到打开控件', async () => {
    const originalOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent');
    Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
      configurable: true,
      get: () => document.body,
    });

    try {
      await renderApp('/');
      const user = userEvent.setup();
      const opener = screen.getByRole('button', { name: copy.shell.home.openDrawerAria });
      opener.focus();
      await user.click(opener);

      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      const closeButton = within(dialog).getByRole('button', { name: drawerCopy.closeAria });
      const focusable = within(dialog).getAllByRole('button');
      const last = focusable[focusable.length - 1]!;
      expect(closeButton).toHaveFocus();

      last.focus();
      await user.keyboard('{Tab}');
      expect(closeButton).toHaveFocus();

      closeButton.focus();
      await user.keyboard('{Shift>}{Tab}{/Shift}');
      expect(last).toHaveFocus();

      await user.click(closeButton);
      expect(dialog.contains(document.activeElement)).toBe(true);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
        timeout: 2000,
      });
      await waitFor(() => expect(opener).toHaveFocus());
    } finally {
      if (originalOffsetParent === undefined) {
        delete (HTMLElement.prototype as { offsetParent?: HTMLElement | null }).offsetParent;
      } else {
        Object.defineProperty(HTMLElement.prototype, 'offsetParent', originalOffsetParent);
      }
    }
  });

  it('下滑手势：跟手位移超过阈值即关闭（规格 §1）', async () => {
    const probe = await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    const panel = dialog.querySelector('.drawer-panel');
    expect(panel).not.toBeNull();
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.pointerDown(panel as Element, { clientY: 40 });
    fireEvent.pointerMove(panel as Element, { clientY: 400 });
    fireEvent.pointerUp(panel as Element, { clientY: 400 });
    await waitFor(() => expect(probe.textContent).toBe('/'));
  });

  it('置顶下拉 touchmove preventDefault：浏览器不接管，跟手关闭不被 pointercancel 中断（P2#20）', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    const panel = dialog.querySelector('.drawer-panel') as HTMLElement;
    const { fireEvent } = await import('@testing-library/react');
    // jsdom 无 TouchEvent：以 Event 子类最小 polyfill（touches 携带 clientY，cancelable 有效）
    class FakeTouch {
      identifier: number;
      target: EventTarget | null;
      clientY: number;
      constructor(init: { identifier: number; target: EventTarget | null; clientY: number }) {
        this.identifier = init.identifier;
        this.target = init.target;
        this.clientY = init.clientY;
      }
    }
    class FakeTouchEvent extends Event {
      touches: FakeTouch[];
      constructor(type: string, init: { touches?: FakeTouch[] } & EventInit) {
        super(type, init);
        this.touches = init.touches ?? [];
      }
    }
    const originalTouchEvent = window.TouchEvent;
    (window as unknown as { TouchEvent?: unknown }).TouchEvent = FakeTouchEvent;
    (window as unknown as { Touch?: unknown }).Touch = FakeTouch;
    try {
      fireEvent.pointerDown(panel, { clientY: 40 });
      const pulling = new FakeTouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [new FakeTouch({ identifier: 1, target: panel, clientY: 120 })],
      });
      panel.dispatchEvent(pulling);
      expect(pulling.defaultPrevented).toBe(true);
      // 向上滑动：放行原生滚动
      const pushing = new FakeTouchEvent('touchmove', {
        bubbles: true,
        cancelable: true,
        touches: [new FakeTouch({ identifier: 1, target: panel, clientY: 10 })],
      });
      panel.dispatchEvent(pushing);
      expect(pushing.defaultPrevented).toBe(false);
    } finally {
      (window as unknown as { TouchEvent?: unknown }).TouchEvent = originalTouchEvent;
      delete (window as unknown as { Touch?: unknown }).Touch;
    }
  });
});

describe('抽屉页头铃铛与窄屏单栏化', () => {
  it('抽屉页头右侧挂铃铛（规格 §4；共用基座 §5.1）', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(
      within(dialog).getByRole('button', { name: copy.notifications.bellAria }),
    ).toBeInTheDocument();
  });

  it('抽屉内打开通知面板后 Tab 放行浮层：不被陷阱拽回；抽屉内陷阱照常（P0#2/P2#34）', async () => {
    await renderApp('/settings');
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(within(dialog).getByRole('button', { name: copy.notifications.bellAria }));
    // 通知面板 portal 到抽屉外（popper 包装器）
    const popper = await waitFor(() => {
      const wrapper = document.querySelector('[data-radix-popper-content-wrapper]');
      expect(wrapper).not.toBeNull();
      return wrapper as HTMLElement;
    });
    // 焦点在浮层内（jsdom 无自动聚焦布局，显式给面板内容挂 tabindex 后聚焦）：
    // Tab 不被抽屉陷阱 preventDefault（Radix 自管循环/焦点流）
    const inside = popper.firstElementChild as HTMLElement;
    // 抽屉内触发的提醒面板 portal 到抽屉外：作用域属性由组件自带（不依赖 DOM 继承）
    expect(inside.closest('[data-drawer-scope]')).not.toBeNull();
    inside.setAttribute('tabindex', '-1');
    inside.focus();
    expect(popper.contains(document.activeElement)).toBe(true);
    const forwarded = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    inside.dispatchEvent(forwarded);
    expect(forwarded.defaultPrevented).toBe(false);
    // 对照：焦点在抽屉内最后一个可聚焦元素时陷阱照常圈定（Tab 被 preventDefault 拽回）
    const focusable = within(dialog).getAllByRole('button');
    const last = focusable[focusable.length - 1] as HTMLElement;
    last.focus();
    const trapped = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    last.dispatchEvent(trapped);
    expect(trapped.defaultPrevented).toBe(true);
  });

  it('窄屏（<768px）：首屏模块名单栏整页，点模块整页下钻', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('max-width'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings', 'ops');
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      // 单栏：桌面左栏 nav 不渲染，模块列表在内容区整页呈现
      expect(dialog.querySelector('nav')).toBeNull();
      expect(dialog.querySelector('[data-nav-variant="modules"]')).not.toBeNull();
      const user = userEvent.setup();
      await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      expect(await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry)).toBeInTheDocument();
      expect(probe.textContent).toBe('/settings/knowledge');
    } finally {
      window.matchMedia = original;
    }
  });

  it('窄屏下钻后页头出现返回控件且页头标题显示当前层级名（P1#9）', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('max-width'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings');
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      // 顶层无返回控件
      expect(
        within(dialog).queryByRole('button', { name: drawerCopy.backAria(drawerCopy.personalTitle) }),
      ).not.toBeInTheDocument();
      const user = userEvent.setup();
      await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      // 页头标题显示当前层级名，页头返回控件逐级回退（触屏无 Esc）
      expect(await screen.findByRole('heading', { name: modules.knowledge })).toBeInTheDocument();
      await user.click(
        screen.getByRole('button', { name: drawerCopy.backAria(drawerCopy.personalTitle) }),
      );
      expect(await screen.findByRole('heading', { name: drawerCopy.personalTitle })).toBeInTheDocument();
      expect(probe.textContent).toBe('/settings');
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('prefers-reduced-motion 降级', () => {
  it('下钻降级为直出：两栏均不挂页面级动画类，内容立即切换（共用基座 §5.2）', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings');
      const user = userEvent.setup();
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      // 直出：点击后的提交里内容已切换，无需等待动画时序
      expect(within(dialog).getByText(copy.settings.knowledge.uploads.historyEntry)).toBeInTheDocument();
      expect(probe.textContent).toBe('/settings/knowledge');
      // 直出：过渡渲染整体缺席，页面级动画类一个都不挂
      expect(document.querySelector('[class*="drill-page-"]')).toBeNull();
      expect(dialog.querySelector('.drill-hidden')).toBeNull();
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('左栏项右侧摘要（renderSummary：徽标 / 状态点）', () => {
  const openWindow = {
    window_id: 'cw_1',
    status: 'open' as const,
    opened_at: '2026-08-03T02:00:00Z',
    closed_at: null,
    pairs_collected: 12,
    close_deadline_at: null,
    window_kind: 'manual' as const,
    policy_version: 'eval_2026_v1',
    sample_rate: 0.1,
    opened_by: 'u_ops',
    closed_by: null,
  };

  it('管理段模块按钮：仅可靠的配额待审徽标 / 评测开窗状态点 / 系统运维超时琥珀徽标', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
      getCalibrationWindow: vi.fn(async () => openWindow),
      listOpsJobs: vi.fn(async () => ({ items: [], stale_count: 3 })),
    });
    await renderApp('/admin', 'ops', { adminApi });
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    // 审批中心：仅后端真实提供的配额待审数 2
    const approvalsButton = within(dialog).getByRole('button', { name: /审批中心/ });
    expect(await within(approvalsButton).findByText('2')).toBeInTheDocument();
    expect(within(approvalsButton).queryByText('3')).toBeNull();
    // 评测与校准：开窗中成功绿状态点
    const evaluationButton = within(dialog).getByRole('button', { name: new RegExp(modules.evaluation) });
    await waitFor(() => expect(evaluationButton.querySelector('.bg-success')).not.toBeNull());
    // 系统运维：stale_count 3 警告琥珀徽标
    const operationsButton = within(dialog).getByRole('button', { name: new RegExp(modules.operations) });
    expect(await within(operationsButton).findByText('3')).toBeInTheDocument();
    // 无摘要模块（总览 / 知识空间 / 用户管理）不渲染徽标
    const dashboardButton = within(dialog).getByRole('button', { name: modules.dashboard });
    expect(dashboardButton.querySelector('.bg-mist-gray')).toBeNull();
  });

  it('审批中心下钻行：仅配额申请显示可靠待审徽标', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
    });
    await renderApp('/admin/approvals', 'ops', { adminApi });
    const dialog = await screen.findByRole('dialog', { name: modules.approvals });
    const quotaRow = within(dialog).getByRole('button', { name: new RegExp(modules.quotaRequests) });
    expect(await within(quotaRow).findByText('2')).toBeInTheDocument();
    const submissionsRow = within(dialog).getByRole('button', {
      name: new RegExp(modules.knowledgeApprovals),
    });
    await waitFor(() => expect(adminApi.getApprovalSummary).toHaveBeenCalled());
    expect(within(submissionsRow).queryByText('1')).toBeNull();
  });

  it('超管投稿审核项不展示不可靠的投稿待审数', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
    });
    await renderApp('/admin/spaces', 'admin', { adminApi });
    const dialog = await screen.findByRole('dialog', { name: modules.spaces });
    const submissionsRow = within(dialog).getByRole('button', {
      name: new RegExp(modules.knowledgeApprovals),
    });
    expect(adminApi.getApprovalSummary).not.toHaveBeenCalled();
    expect(within(submissionsRow).queryByText('1')).toBeNull();
  });

  it('摘要为 0 不渲染：模块按钮保持原标题与布局', async () => {
    await renderApp('/admin', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    const approvalsButton = within(dialog).getByRole('button', { name: modules.approvals });
    // 默认 fake：合计 0 / closed / stale 0 —— 摘要静默，按钮可访问名即原标题
    expect(approvalsButton.textContent).toBe(modules.approvals);
  });
});
