import { expect, test, type Locator, type Page } from '@playwright/test';
import { copy } from '../src/copy';
import { ADMIN_SEED_NAMES } from '../src/mocks/admin-contract';

/*
 * 管理面板 e2e（fe-admin-panels）：真实浏览器 + 契约 mock（MSW worker）。
 * 覆盖 brief「e2e 关键路径」：
 * - 运维审批：审批中心待处理徽标 → 配额申请批准（100→80 页）→ 行淡出 + 页头下轻提示 + 徽标 -1；
 * - 运维图谱：公共库图谱维护区（种子 stale + source_revision 12）发起重建，确认层显示
 *   source_revision 与预估说明，提交后 mock 轮询推进到终态「构建成功 / 图谱可用」（真实等待，
 *   断言 timeout 放宽 30s）；
 * - 超管用户：自己 / admin 行无操作；编辑部长选「无部门」提交 422 部门框变红 → 改选有效部门
 *   保存成功；永久禁用另一用户 → 二次确认三点说明 → 行原地冻结（「已冻结，待清理」tag +
 *   「将于 … 清理」）；
 * - 超管部门：有成员部门停用 409 department_has_members 阻断（行仍在「在用」、无强制入口）；
 *   空部门停用成功转「已停用」；
 * - 左栏清单按角色：ops 六模块（含审批中心）、admin 五模块（无审批中心）。
 * mock 种子见 src/mocks/admin-contract.ts（配额 3 条 pending、用户 / 部门种子）；
 * 账号见 src/mocks/auth-contract.ts（ops-wang / admin，密码统一 password123）；
 * 断言一律引用 copy 常量，用户名 / 部门名等 mock 种子数据在注释中标明。
 */

const modules = copy.shell.drawer.modules;
const copyApprovals = copy.admin.approvals;
const copyGraph = copy.admin.spaces.graph;
const copyUsers = copy.admin.users;
const copyDepartments = copy.admin.departments;

async function login(page: Page, username: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

/** 超管落聊天主页；头像区按角色直达管理段「总览」（HomePage 分支）。 */
async function openAdminDrawer(page: Page): Promise<void> {
  await page.getByRole('button', { name: copy.shell.home.openDrawerAria }).first().click();
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
}

/**
 * 五步下钻动画窗口（DrawerHost TOTAL_DRILL_MS = 550ms）：窗口内内容区并存 exit/enter 两份
 * 拷贝，且动画结束内容层整棵重挂载（过渡态 absolute 双拷贝 → 稳态单拷贝）。先等目标层
 * 内容出现（证明过渡已开始），再等过渡包装（仅过渡中渲染的 .relative.h-full）消失，
 * 之后交互的才是最终拷贝；重挂载拷贝重新拉取数据由后续 expect 自动重试覆盖。
 */
async function waitDrillSettled(drawer: Locator): Promise<void> {
  await expect(drawer.locator('.relative.h-full')).toHaveCount(0);
}

test('ops approves a quota request with adjusted pages: row fades out, notice shows, and the badge decrements', async ({
  page,
}) => {
  await login(page, 'ops-wang');

  // 落地标记驱动抽屉自动展开到管理段「总览」；审批中心模块项带待处理徽标（非零计数）
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  const dashboardDialog = page.getByRole('dialog', { name: modules.dashboard });
  const approvalsModule = dashboardDialog.getByRole('button', {
    name: new RegExp(modules.approvals),
  });
  await expect(approvalsModule.getByText(/^\d+$/)).toBeVisible();

  // 审批中心 → 「配额申请」下钻项徽标 = quota_pending（种子 3 条：zhangsan / minister-li / ghost）
  await approvalsModule.click();
  await expect(page).toHaveURL(/\/admin\/approvals$/);
  const approvalsDialog = page.getByRole('dialog', { name: modules.approvals });
  const quotaRow = approvalsDialog.getByRole('button', { name: new RegExp(modules.quotaRequests) });
  await expect(quotaRow.getByText('3', { exact: true })).toBeVisible();
  await quotaRow.click();
  await expect(page).toHaveURL(/\/admin\/approvals\/quota$/);

  // 配额申请列表：种子首行 zhangsan 申请 100 页（按申请时间正序）
  const quotaSection = approvalsDialog.getByRole('region', { name: copyApprovals.quota });
  await expect(quotaSection).toBeVisible();
  await waitDrillSettled(approvalsDialog);
  const zhangsanRow = quotaSection.getByRole('listitem').filter({ hasText: 'zhangsan' });
  await expect(zhangsanRow).toHaveCount(1);
  await expect(zhangsanRow.getByText(copyApprovals.pages(100), { exact: true })).toBeVisible();

  // 批准：对话框改 approved_pages 100 → 80，确认
  await zhangsanRow.getByRole('button', { name: copyApprovals.approve }).click();
  const approveDialog = page.getByRole('dialog', { name: copyApprovals.approveDialogTitle });
  await expect(approveDialog).toBeVisible();
  await expect(
    approveDialog.getByText(copyApprovals.approveDialogDescription('zhangsan', 100)),
  ).toBeVisible();
  await approveDialog.getByLabel(copyApprovals.approvePagesLabel).fill('80');
  await approveDialog.getByRole('button', { name: copy.controls.confirm }).click();

  // 成功：页头下轻提示 + 行 250ms 淡出后移除（minister-li / ghost 行保留）
  await expect(
    quotaSection.getByRole('status').filter({ hasText: copyApprovals.approvedNotice }),
  ).toBeVisible();
  await expect(quotaSection.getByRole('listitem').filter({ hasText: 'zhangsan' })).toHaveCount(0);
  await expect(
    quotaSection.getByRole('listitem').filter({ hasText: 'minister-li' }),
  ).toHaveCount(1);

  // 徽标计数 -1：回审批中心层，「配额申请」下钻项徽标 3 → 2（invalidateSummaries 重取）
  await approvalsDialog
    .getByRole('button', { name: copy.shell.drawer.backAria(modules.approvals) })
    .click();
  await expect(page).toHaveURL(/\/admin\/approvals$/);
  await expect(
    approvalsDialog
      .getByRole('button', { name: new RegExp(modules.quotaRequests) })
      .getByText('2', { exact: true }),
  ).toBeVisible();
});

test('ops rebuilds the public graph: confirm layer shows the source revision and estimate, then polling reaches ready', async ({
  page,
}) => {
  // mock 非终态 5s 轮询逐拍推进（queued→running→succeeded），给终态断言留足真实等待
  test.setTimeout(60_000);
  await login(page, 'ops-wang');
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  const dashboardDialog = page.getByRole('dialog', { name: modules.dashboard });

  // 知识空间 → 公共库：图谱维护区（仅 ops 挂载）种子为 stale + source_revision 12 + 无构建记录
  await dashboardDialog.getByRole('button', { name: modules.spaces }).click();
  await expect(page).toHaveURL(/\/admin\/spaces$/);
  const spacesDialog = page.getByRole('dialog', { name: modules.spaces });
  await spacesDialog.getByRole('button', { name: modules.publicSpace }).click();
  await expect(page).toHaveURL(/\/admin\/spaces\/public$/);
  const graphSection = spacesDialog.getByRole('region', { name: copyGraph.title });
  await expect(graphSection).toBeVisible();
  await waitDrillSettled(spacesDialog);
  await expect(graphSection.getByText(copyGraph.availabilityStale)).toBeVisible();
  await expect(graphSection.getByText(copyGraph.sourceRevision(12))).toBeVisible();
  await expect(graphSection.getByText(copyGraph.empty)).toBeVisible();

  // 发起重建：确认层显示当前 source_revision；种子无上一 run，预估以提交后服务端计算说明呈现
  await graphSection.getByRole('button', { name: copyGraph.buildRebuild }).click();
  const confirmDialog = page.getByRole('dialog', { name: copyGraph.confirmTitleRebuild });
  await expect(confirmDialog).toBeVisible();
  await expect(confirmDialog.getByText(copyGraph.confirmRevision(12))).toBeVisible();
  await expect(confirmDialog.getByText(copyGraph.confirmEstimatePending)).toBeVisible();
  await confirmDialog.getByRole('button', { name: copyGraph.confirmStart }).click();

  // 提交成功：轻提示 + 最近一次构建行（预估主模型调用 = 公共库文档数 × 3，mock 计算）
  await expect(page.getByRole('dialog', { name: copyGraph.confirmTitleRebuild })).toHaveCount(0);
  await expect(
    graphSection.getByRole('status').filter({ hasText: copyGraph.startedNotice }),
  ).toBeVisible();
  const estimatedPattern = new RegExp(copyGraph.estimatedCalls(1).replace('1', '\\d+'));
  await expect(graphSection.getByText(estimatedPattern)).toBeVisible();

  // mock 每次状态查询推进一拍：queued→running→succeeded，终态 availability 转 ready
  await expect(graphSection.getByText(copyGraph.statusSucceeded)).toBeVisible({ timeout: 30_000 });
  await expect(graphSection.getByText(copyGraph.availabilityReady)).toBeVisible({
    timeout: 30_000,
  });
});

test('admin edits a minister user (422 on no-department, then saves) and disables another user in place', async ({
  page,
}) => {
  await login(page, 'admin');
  await openAdminDrawer(page);
  const dashboardDialog = page.getByRole('dialog', { name: modules.dashboard });
  await dashboardDialog.getByRole('button', { name: modules.usersAdmin }).click();
  await expect(page).toHaveURL(/\/admin\/users$/);
  const usersDialog = page.getByRole('dialog', { name: modules.usersAdmin });

  // 用户表渲染（种子 9 行）；自己（admin）行与冻结行无任何操作入口
  const usersSection = usersDialog.getByRole('region', { name: modules.usersOps });
  const rows = usersSection.getByRole('listitem');
  await expect(rows.filter({ hasText: ADMIN_SEED_NAMES.zhangsan })).toHaveCount(1);
  const selfRow = rows.filter({ hasText: ADMIN_SEED_NAMES.systemAdmin });
  await expect(selfRow).toHaveCount(1);
  await expect(selfRow.getByRole('button', { name: copyUsers.edit })).toHaveCount(0);
  await expect(selfRow.getByRole('button', { name: copyUsers.disable })).toHaveCount(0);
  const frozenSeedRow = rows.filter({ hasText: ADMIN_SEED_NAMES.ghost });
  await expect(frozenSeedRow.getByText(copy.admin.common.frozenTag)).toBeVisible();
  await expect(frozenSeedRow.getByRole('button', { name: copyUsers.edit })).toHaveCount(0);

  // 编辑部长用户（种子 minister-li 李部长）：部门选「无部门」提交 → 422 部门框变红 + 框下说明
  await rows.filter({ hasText: ADMIN_SEED_NAMES.ministerLi }).getByRole('button', { name: copyUsers.edit }).click();
  const editDialog = page.getByRole('dialog', { name: copyUsers.editDialogTitle });
  await expect(editDialog).toBeVisible();
  const departmentSelect = editDialog.getByLabel(copyUsers.colDepartment);
  await departmentSelect.selectOption({ label: copyUsers.noDepartmentOption });
  await editDialog.getByRole('button', { name: copyUsers.save }).click();
  await expect(
    editDialog.getByRole('alert').filter({ hasText: copyUsers.ministerDepartmentRequired }),
  ).toBeVisible();
  await expect(editDialog.getByLabel(copyUsers.colDepartment)).toHaveAttribute(
    'aria-invalid',
    'true',
  );

  // 改选有效部门（mock 种子 d_hr「人事部」）保存成功：对话框关闭，行部门就地更新
  await departmentSelect.selectOption({ value: 'd_hr' });
  await editDialog.getByRole('button', { name: copyUsers.save }).click();
  await expect(page.getByRole('dialog', { name: copyUsers.editDialogTitle })).toHaveCount(0);
  const ministerRow = rows.filter({ hasText: ADMIN_SEED_NAMES.ministerLi });
  await expect(ministerRow.getByText(ADMIN_SEED_NAMES.hr, { exact: true })).toBeVisible();

  // 永久禁用另一用户（种子 chenchen 陈晨）：二次确认固定三点说明 → 确认
  const chenRow = rows.filter({ hasText: ADMIN_SEED_NAMES.chenchen });
  await chenRow.getByRole('button', { name: copyUsers.disable }).click();
  const disableDialog = page.getByRole('dialog', { name: copyUsers.disableDialogTitle });
  await expect(disableDialog).toBeVisible();
  for (const point of [copyUsers.disablePoint1, copyUsers.disablePoint2, copyUsers.disablePoint3]) {
    await expect(disableDialog.getByText(point)).toBeVisible();
  }
  await disableDialog.getByRole('button', { name: copyUsers.disableConfirm }).click();

  // 202：行不移除，原地转冻结展示（「已冻结，待清理」tag +「将于 … 清理」），操作入口收起
  await expect(page.getByRole('dialog', { name: copyUsers.disableDialogTitle })).toHaveCount(0);
  await expect(chenRow.getByText(copy.admin.common.frozenTag)).toBeVisible();
  const purgePattern = new RegExp(copyUsers.purgeAfter('X').replace('X', '.+'));
  await expect(chenRow.getByText(purgePattern)).toBeVisible();
  await expect(chenRow.getByRole('button', { name: copyUsers.edit })).toHaveCount(0);
  await expect(chenRow.getByRole('button', { name: copyUsers.disable })).toHaveCount(0);
});

test('admin is blocked deactivating a department with members, then deactivates an empty department', async ({
  page,
}) => {
  await login(page, 'admin');
  await openAdminDrawer(page);
  const dashboardDialog = page.getByRole('dialog', { name: modules.dashboard });
  await dashboardDialog.getByRole('button', { name: modules.usersAdmin }).click();
  await expect(page).toHaveURL(/\/admin\/users$/);
  const usersDialog = page.getByRole('dialog', { name: modules.usersAdmin });

  // 「部门管理」整行下钻；默认「在用」筛选渲染种子三个 active 部门
  await usersDialog.getByRole('button', { name: copyUsers.departments }).click();
  await expect(page).toHaveURL(/\/admin\/users\/departments$/);
  const deptSection = usersDialog.getByRole('region', { name: modules.departments });
  await expect(deptSection).toBeVisible();
  await waitDrillSettled(usersDialog);
  const rows = deptSection.getByRole('listitem');
  await expect(rows).toHaveCount(3);

  // 有成员的部门（种子 d_finance 财务部：成员 3 · 任务 0 · 待审 2）点「停用」→ 确认
  const financeRow = rows.filter({ hasText: ADMIN_SEED_NAMES.finance });
  await financeRow.getByRole('button', { name: copyDepartments.deactivate }).click();
  const deactivateDialog = page.getByRole('dialog', {
    name: copyDepartments.deactivateDialogTitle,
  });
  await expect(deactivateDialog).toBeVisible();
  for (const point of [
    copyDepartments.deactivatePoint1,
    copyDepartments.deactivatePoint2,
    copyDepartments.deactivatePoint3,
  ]) {
    await expect(deactivateDialog.getByText(point)).toBeVisible();
  }
  await expect(
    deactivateDialog.getByText(copyDepartments.deactivateCounts(3, 0, 2)),
  ).toBeVisible();
  await deactivateDialog.getByRole('button', { name: copyDepartments.deactivateConfirm }).click();

  // 409 department_has_members：框内阻断说明（无「强制停用」替代入口），行刷新后仍在「在用」
  await expect(
    deactivateDialog.getByRole('alert').filter({ hasText: copyDepartments.blockedHasMembers }),
  ).toBeVisible();
  await deactivateDialog.getByRole('button', { name: copy.controls.cancel }).click();
  await expect(
    page.getByRole('dialog', { name: copyDepartments.deactivateDialogTitle }),
  ).toHaveCount(0);
  await expect(financeRow.getByText(copyDepartments.statusActive, { exact: true })).toBeVisible();
  // 操作仍只有「改名 / 停用」两个（被阻断后不补出强制入口）
  await expect(financeRow.getByRole('button')).toHaveCount(2);

  // 空部门（种子 d_empty 空壳部：计数全 0）停用成功：行转「已停用」，操作收起
  const emptyRow = rows.filter({ hasText: ADMIN_SEED_NAMES.emptyDept });
  await emptyRow.getByRole('button', { name: copyDepartments.deactivate }).click();
  const emptyDialog = page.getByRole('dialog', { name: copyDepartments.deactivateDialogTitle });
  await expect(emptyDialog.getByText(copyDepartments.deactivateCounts(0, 0, 0))).toBeVisible();
  await emptyDialog.getByRole('button', { name: copyDepartments.deactivateConfirm }).click();
  await expect(
    page.getByRole('dialog', { name: copyDepartments.deactivateDialogTitle }),
  ).toHaveCount(0);
  await expect(emptyRow.getByText(copyDepartments.statusInactive, { exact: true })).toBeVisible();
  await expect(emptyRow.getByRole('button', { name: copyDepartments.deactivate })).toHaveCount(0);
  await expect(emptyRow.getByRole('button', { name: copyDepartments.rename })).toHaveCount(0);
});

test('ops drawer nav lists six admin modules including the approvals entry', async ({ page }) => {
  await login(page, 'ops-wang');
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  const dialog = page.getByRole('dialog', { name: modules.dashboard });
  const nav = dialog.getByRole('navigation');

  // 运维六模块：总览 / 审批中心 / 知识空间 / 评测与校准 / 系统运维 / 用户管理（无超管「人员与权限」）
  for (const name of [
    modules.dashboard,
    modules.approvals,
    modules.spaces,
    modules.evaluation,
    modules.operations,
    modules.usersOps,
  ]) {
    await expect(nav.getByRole('button', { name })).toBeVisible();
  }
  await expect(nav.getByRole('button', { name: modules.usersAdmin })).toHaveCount(0);
});

test('admin drawer nav lists five admin modules without the approvals entry', async ({ page }) => {
  await login(page, 'admin');
  await openAdminDrawer(page);
  const dialog = page.getByRole('dialog', { name: modules.dashboard });
  const nav = dialog.getByRole('navigation');

  // 超管五模块：总览 / 知识空间 / 评测与校准 / 系统运维 / 人员与权限（无运维「审批中心」）
  for (const name of [
    modules.dashboard,
    modules.spaces,
    modules.evaluation,
    modules.operations,
    modules.usersAdmin,
  ]) {
    await expect(nav.getByRole('button', { name })).toBeVisible();
  }
  await expect(nav.getByRole('button', { name: modules.approvals })).toHaveCount(0);
  await expect(nav.getByRole('button', { name: modules.usersOps })).toHaveCount(0);
});
