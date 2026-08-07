import { expect, test, type Page } from '@playwright/test';
import { copy } from '../src/copy';
import { NOTIFICATION_SEED_TITLES } from '../src/mocks/notifications-contract';

/*
 * 共享壳层 e2e（fe-shared-shell）：真实浏览器 + 契约 mock（MSW worker）。
 * 覆盖规格 §1–§4 关键路径：
 * - 抽屉从头像区打开、下钻、刷新恢复同一层、粘贴深链恢复、Esc 逐层关闭；
 * - 铃铛徽标 / 面板 / 未知类型兜底 / 点击已读并跳转目标层 / read-all；
 * - 运维登录落地标记自动展开管理段「总览」，管理段模块按角色渲染。
 * mock 账号见 src/mocks/auth-contract.ts（密码统一 password123）；通知种子标题经
 * NOTIFICATION_SEED_TITLES 引用，不硬编码中文。
 */

async function login(page: Page, username: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

test('drawer opens from the avatar row, drills down, restores the same layer on reload, and closes layer by layer via Esc', async ({
  page,
}) => {
  await login(page, 'zhangsan');

  // 头像区 → 个人段「设置」抽屉（URL 同步）
  await page.getByRole('button', { name: copy.shell.home.openDrawerAria }).first().click();
  const dialog = page.getByRole('dialog', { name: copy.shell.drawer.personalTitle });
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/\/settings$/);

  // 下钻两层到上传结果层
  await dialog.getByRole('button', { name: copy.shell.drawer.modules.knowledge }).click();
  await expect(page).toHaveURL(/\/settings\/knowledge$/);
  await dialog
    .getByRole('button', { name: copy.shell.drawer.modules.uploads, exact: true })
    .click();
  await expect(page).toHaveURL(/\/settings\/knowledge\/uploads$/);
  // 下钻动画窗口内内容区会短暂并存 exit/enter 两份拷贝；层名断言限定在左栏导航内
  await expect(
    dialog.getByRole('navigation').getByText(copy.shell.drawer.modules.uploads),
  ).toBeVisible();

  // 刷新恢复到同一层；粘贴深链到新页面同样恢复
  await page.reload();
  const reloaded = page.getByRole('dialog', { name: copy.shell.drawer.personalTitle });
  await expect(reloaded.getByText(copy.shell.drawer.modules.uploads)).toBeVisible();
  await page.goto('/settings/knowledge');
  await expect(
    page
      .getByRole('dialog', { name: copy.shell.drawer.personalTitle })
      .getByRole('button', { name: copy.shell.drawer.modules.submissions }),
  ).toBeVisible();

  // 返回键（浏览器后退）：URL 同步驱动抽屉逐层回退恢复（后退整页加载亦恢复到对应层）
  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/knowledge\/uploads$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/knowledge$/);
  // goBack 可能是整页加载；toHaveURL 只反映浏览器 URL，不代表应用已启动。
  // 等知识库层内容渲染完毕（DrawerHost 挂载、Esc 监听就位）再按 Esc，消除启动竞态。
  await expect(
    page
      .getByRole('dialog', { name: copy.shell.drawer.personalTitle })
      .getByRole('button', { name: copy.shell.drawer.modules.submissions }),
  ).toBeVisible();

  // Esc 逐层：知识库层 → 顶层 → 关闭回聊天主页
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/\/settings$/);
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  // 主页保持挂载
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
});

test('bell badge, panel, unknown-type fallback, per-item read with navigation, and read-all', async ({
  page,
}) => {
  await login(page, 'zhangsan');

  // 未读徽标：服务端权威计数（zhangsan 种子 6 条未读）
  const bell = page.getByRole('button', { name: copy.notifications.bellAria });
  await expect(bell.getByLabel(copy.notifications.unreadBadgeAria(6))).toBeVisible();

  // 打开面板即拉列表；第一条为最新送达（未知类型 deep_research_completed）
  await bell.click();
  const panel = page.locator('.notification-panel');
  await expect(panel.getByText(NOTIFICATION_SEED_TITLES.unknownType)).toBeVisible();

  // 未知类型：后端 title + 通用图标呈现，点击标已读但不导航、不崩溃（面板保持打开）
  await panel.getByText(NOTIFICATION_SEED_TITLES.unknownType).click();
  await expect(page).toHaveURL(/\/$/);

  // 点击 ingestion_completed：标已读并跳转到知识库 → 上传结果层（抽屉自动展开）。
  // 面板仍开着，直接点下一条（Bell 触发器受 Radix 控制，重复点铃铛会把面板切换关闭）
  await panel.getByText(NOTIFICATION_SEED_TITLES.ingestionDone).click();
  await expect(page).toHaveURL(/\/settings\/knowledge\/uploads$/);
  const dialog = page.getByRole('dialog', { name: copy.shell.drawer.personalTitle });
  await expect(dialog).toBeVisible();

  // read-all：已渲染条目标已读，随后刷新计数与列表，徽标清零（150ms 淡出后移除）。
  // 抽屉已盖住主页铃铛，从抽屉页头右侧的铃铛开面板
  await dialog.getByRole('button', { name: copy.notifications.bellAria }).click();
  await page.getByRole('button', { name: copy.notifications.readAll }).click();
  const badgeAriaPattern = new RegExp(copy.notifications.unreadBadgeAria(1).replace('1', '\\d+'));
  await expect(page.getByLabel(badgeAriaPattern)).toHaveCount(0);
  // 脱敏文案样例原样展示（后端固定文案，前端不恢复文件名）
  await expect(panel.getByText(NOTIFICATION_SEED_TITLES.redacted)).toBeVisible();
});

test('ops login lands with the admin drawer auto-expanded to the dashboard, and the admin segment renders ops modules', async ({
  page,
}) => {
  await login(page, 'ops-wang');

  // 落地标记驱动抽屉自动展开到管理段首层「总览」
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  const dialog = page.getByRole('dialog', { name: copy.shell.drawer.modules.dashboard });
  await expect(dialog).toBeVisible();

  // 管理段六模块（运维视角）：总览 / 审批中心 / 知识空间 / 评测与校准 / 系统运维 / 用户管理
  for (const name of [
    copy.shell.drawer.modules.dashboard,
    copy.shell.drawer.modules.approvals,
    copy.shell.drawer.modules.spaces,
    copy.shell.drawer.modules.evaluation,
    copy.shell.drawer.modules.operations,
    copy.shell.drawer.modules.usersOps,
  ]) {
    await expect(dialog.getByRole('button', { name })).toBeVisible();
  }

  // 关闭按钮回到聊天主页
  await dialog.getByRole('button', { name: copy.shell.drawer.closeAria }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
