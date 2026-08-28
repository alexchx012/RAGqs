import { expect, test, type Locator, type Page } from '@playwright/test';
import { copy } from '../src/copy';
import { OPS_BACKUP_SEED_IDS } from '../src/mocks/admin-contract';

/*
 * 备份与恢复 e2e（backup-restore-operations-layer 规格 §2/§9）：真实浏览器 + 契约 mock（MSW worker）。
 * - ops 主链路：深链 /admin/operations/backups 三分段 → creating 种子 5s 轮询收敛「完成」
 *   （真实等待，断言 timeout 放宽 30s）→ 恢复段 repair retry 受理轻提示 + 详情收敛「已修复」
 *   （阻断恢复转终态，维护门禁随之解除）→ 备份段一键备份受理轻提示 + 历史行数 +1 →
 *   恢复段危险确认框齐备备份 ID / 维护影响后取消（不再发起，避免重新进入维护门禁）→
 *   策略段版本行 / 保护式 AND 说明 / 下次执行可见。
 * - admin：深链按角色截断回退系统运维层，下钻行无「备份与恢复」（严格 ops-only）。
 * mock 种子见 src/mocks/admin-contract.ts（OPS_BACKUP_SEED_IDS）；账号见 src/mocks/auth-contract.ts
 * （ops-wang / admin，密码统一 password123）；断言一律引用 copy 常量与种子 ID 常量。
 */

const modules = copy.shell.drawer.modules;
const copyBackups = copy.admin.operations.backups;

async function login(page: Page, username: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

/**
 * 抽屉过渡窗口（五步下钻 550ms / 同层切换交叉淡变 150ms）：窗口内内容区并存 from/to 两份
 * 拷贝，断言/交互须等包装消失、只剩稳态单拷贝后进行。
 */
async function waitDrillSettled(drawer: Locator): Promise<void> {
  await expect(
    drawer.locator(
      '.drill-exit, .drill-hidden, .drill-content-rise, .drill-content-return, .drill-switch, .drill-flip-clone',
    ),
  ).toHaveCount(0);
}

test('ops runs the backup and restore operations loop: poll converges, repair retry lifts the maintenance gate, manual backup accepted, restore confirm shows impact, policy readouts visible', async ({
  page,
}) => {
  // 轮询收敛为真实 5s 拍，给终态断言留足等待
  test.setTimeout(60_000);
  await login(page, 'ops-wang');
  await page.goto('/admin/operations/backups');

  const dialog = page.getByRole('dialog', { name: modules.operations });
  await waitDrillSettled(dialog);
  const section = page.getByRole('region', { name: copyBackups.title });
  await expect(section).toBeVisible();

  // 三分段单选：默认「备份」选中
  const segmented = section.getByRole('radiogroup', { name: copyBackups.title });
  await expect(segmented.getByRole('radio', { name: copyBackups.viewBackups })).toBeChecked();
  await expect(segmented.getByRole('radio', { name: copyBackups.viewRestores })).toBeVisible();
  await expect(segmented.getByRole('radio', { name: copyBackups.viewPolicy })).toBeVisible();

  // creating 种子行：真实浏览器 dev 启动耗时可能越过首个 5s 拍，初态容忍竞态
  // （创建中 或已收敛 完成；「首轮读仍 creating」的确定性语义由组件测试锁定）；
  // 收敛到「完成」即证明 5s 轮询生效（无轮询则停留在创建中）
  const creatingRow = section
    .getByRole('row')
    .filter({ hasText: OPS_BACKUP_SEED_IDS.creatingBackup });
  await expect(creatingRow).toBeVisible();
  await expect(
    creatingRow.getByText(
      new RegExp(
        `${copyBackups.backupStatus('creating')}|${copyBackups.backupStatus('complete')}`,
      ),
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    creatingRow.getByText(copyBackups.backupStatus('complete'), { exact: true }),
  ).toBeVisible({ timeout: 30_000 });

  // 「恢复」段：展开种子 blocked 恢复，repair retry 受理 → 轻提示 + 详情收敛「已修复」
  await segmented.getByRole('radio', { name: copyBackups.viewRestores }).click();
  const restoreRow = section
    .getByRole('row')
    .filter({ hasText: OPS_BACKUP_SEED_IDS.repairRestore });
  await expect(restoreRow).toBeVisible();
  // 行在 li 包装内，进度按钮与行内详情均挂在同一 li 下
  const restoreItem = section.locator('li', { hasText: OPS_BACKUP_SEED_IDS.repairRestore });
  await restoreItem.getByRole('button', { name: copyBackups.progressExpand }).click();
  await restoreItem.getByRole('button', { name: copyBackups.repairRetry }).click();
  await expect(
    section.getByRole('status').filter({ hasText: copyBackups.repairRetried }),
  ).toBeVisible();
  await expect(
    restoreItem.getByText(new RegExp(copyBackups.repairStatus('succeeded'))),
  ).toBeVisible();
  await expect(
    restoreItem.getByRole('button', { name: copyBackups.repairRetry }),
  ).toHaveCount(0);

  // 「备份」段：阻断恢复已转终态（维护门禁解除），一键备份 202 受理 → 轻提示 + 行数 2→3
  await segmented.getByRole('radio', { name: copyBackups.viewBackups }).click();
  await expect(
    section.getByRole('row').filter({ hasText: OPS_BACKUP_SEED_IDS.completeBackup }),
  ).toBeVisible();
  await section.getByRole('button', { name: copyBackups.createBackup }).click();
  await expect(
    section
      .getByRole('status')
      .filter({ hasText: new RegExp(copyBackups.backupCreated('.+')) }),
  ).toBeVisible();
  await expect(section.getByRole('rowgroup').locator('li')).toHaveCount(3);

  // 「恢复」段：危险确认框齐备备份 ID / 维护模式影响 / 备份当前状态；取消不发起
  await segmented.getByRole('radio', { name: copyBackups.viewRestores }).click();
  const sourceSelect = section.getByLabel(copyBackups.sourceLabel);
  await expect(
    sourceSelect.locator('option', { hasText: OPS_BACKUP_SEED_IDS.completeBackup }),
  ).toHaveCount(1);
  await sourceSelect.selectOption(OPS_BACKUP_SEED_IDS.completeBackup);
  await section.getByRole('button', { name: copyBackups.startRestore }).click();
  const confirmDialog = page.getByRole('dialog', { name: copyBackups.restoreDialogTitle });
  await expect(
    confirmDialog.getByText(copyBackups.restoreDialogBackup(OPS_BACKUP_SEED_IDS.completeBackup)),
  ).toBeVisible();
  await expect(confirmDialog.getByText(copyBackups.restoreDialogImpact)).toBeVisible();
  await expect(
    confirmDialog.getByText(
      copyBackups.restoreDialogStatus(copyBackups.backupStatus('complete')),
    ),
  ).toBeVisible();
  await confirmDialog.getByRole('button', { name: copy.controls.cancel }).click();
  await expect(confirmDialog).toHaveCount(0);

  // 「策略」段：版本行 / 保护式 AND 说明 / 下次执行只读呈现
  await segmented.getByRole('radio', { name: copyBackups.viewPolicy }).click();
  await expect(section.getByText(copyBackups.policyVersion(1))).toBeVisible();
  await expect(section.getByText(copyBackups.policyRetentionNote)).toBeVisible();
  await expect(
    section.getByText(new RegExp(copyBackups.policyNextRun('.+'))),
  ).toBeVisible();
});

test('admin deep link to /admin/operations/backups is truncated by role: drill-in row hidden (strict ops-only)', async ({
  page,
}) => {
  await login(page, 'admin');
  await page.goto('/admin/operations/backups');

  const dialog = page.getByRole('dialog', { name: modules.operations });
  await waitDrillSettled(dialog);
  // 任务队列 / 指标看板下钻行在；备份与恢复注册项 roles=['ops'] 整行隐藏，子层不渲染
  await expect(
    dialog.getByRole('button', { name: new RegExp(`^${modules.opsJobs}`) }),
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: new RegExp(`^${modules.opsMetrics}`) }),
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: new RegExp(modules.backups) }),
  ).toHaveCount(0);
  await expect(dialog.getByRole('radiogroup')).toHaveCount(0);
});
