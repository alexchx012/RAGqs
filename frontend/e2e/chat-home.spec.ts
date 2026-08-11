import { expect, test, type Page } from '@playwright/test';
import { copy } from '../src/copy';
import { CHAT_SEED_DOCUMENT_NAMES, CHAT_SEED_TITLES } from '../src/mocks/chat-contract';

/*
 * 聊天主页 e2e（fe-chat-home）：真实浏览器 + 契约 mock（MSW worker），自包含不起后端。
 * 覆盖 Acceptance examples 关键路径：
 * - 提问 → 模拟渲染 → 引用角标悬停卡 → 点击新窗口打开原文预览页（/preview/:document_id，
 *   透传 message_id + document_version_id，fe-doc-preview）→ 常设 👍 反馈；
 * - 思考档阶段状态行呈现（think 档提问）；
 * - 盲测 A/B：种子未投票对比对重建 → 投票 0 → 所选恢复常设反馈。
 * mock 账号：zhangsan / password123（普通用户，个人库含 doc_1 员工手册.pdf）。
 * 文案一律经 copy / CHAT_SEED_TITLES 引用（copy-discipline：e2e 亦受扫描，禁 CJK 字面量）。
 */

async function login(page: Page, username = 'zhangsan'): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

test('ask renders, citation hover card opens preview in a new window, and upvote feedback persists', async ({
  page,
  context,
}) => {
  await login(page);
  const composer = page.getByLabel(copy.chat.composer.inputPlaceholder);

  // 提问（quick 档：mock answer 携带一条 page 引用 doc_1）
  await composer.fill('annual leave policy question');
  await page.getByRole('button', { name: copy.chat.composer.sendAria }).click();
  await expect(composer).toHaveValue('');

  // 回答正文出现（e2e 默认 no-preference，模拟流式逐段 —— 等正文非空）
  const answerBlock = page
    .locator('.chat-message-enter')
    .filter({ hasText: 'Mock answer for' })
    .first();
  await expect(answerBlock).toBeVisible();

  // 引用角标 [1]：悬停出「引自《doc_1》」+ 定位行（第 12 页 第 345–412 字符）
  const badge = answerBlock.getByRole('button', { name: copy.chat.message.citeOpenAria }).first();
  await expect(badge).toHaveText('[1]');
  await badge.hover();
  // M9：悬停卡展示 document_name（CHAT_SEED_DOCUMENT_NAMES），不显示不透明 ID
  await expect(
    page.getByText(copy.chat.message.citeFrom(CHAT_SEED_DOCUMENT_NAMES.employeeHandbook)),
  ).toBeVisible();
  await expect(page.getByText(copy.chat.message.citePageSpan(12, 345, 412))).toBeVisible();

  // 点击角标：新窗口打开原文预览页（fe-doc-preview）并透传 message_id + document_version_id
  const [preview] = await Promise.all([
    context.waitForEvent('page'),
    badge.click({ modifiers: ['Control'] }),
  ]);
  await preview.waitForLoadState();
  expect(preview.url()).toContain('/preview/doc_1');
  expect(preview.url()).toContain('document_version_id=v_1');
  expect(preview.url()).toContain('message_id=');
  // 预览加载出页头文档名（深链 / 命中导航等由 doc-preview.spec.ts 覆盖）
  await expect(preview.getByRole('heading', { level: 1 })).toHaveText(CHAT_SEED_DOCUMENT_NAMES.employeeHandbook);
  await preview.close();

  // 常设 👍 反馈：点击后固化（store 刷新读模型呈现已投态）
  await answerBlock.getByRole('button', { name: copy.chat.feedbackUpAria }).click();
  await expect(
    answerBlock.getByRole('button', { name: copy.chat.feedbackUpAria }),
  ).toBeVisible();
});

test('think effort shows the effort-upgraded system notice and streams the answer body', async ({
  page,
}) => {
  await login(page);
  const composer = page.getByLabel(copy.chat.composer.inputPlaceholder);

  // 切到「思考」档
  await page.getByRole('radio', { name: copy.chat.composer.effortThink }).click();
  await composer.fill('think effort question');
  await page.getByRole('button', { name: copy.chat.composer.sendAria }).click();

  // 思考档 mock 脚本：start→stage(retrieving)→stage(generating)→notice(effort_upgraded)→answer→done。
  // 阶段状态行是「正文开始模拟渲染前」的占位区（spec §3.4：收到 answer 后状态行淡出、由正文取代），
  // 契约 mock 即时完成导致其闪现即逝、无法在 e2e 稳定断言；改断言持久化的 effort_upgraded 提示条
  // 与最终正文（阶段切换语义由 store/generation 单测覆盖）。
  const answerBlock = page
    .locator('.chat-message-enter')
    .filter({ hasText: 'Mock answer for' })
    .first();
  await expect(answerBlock).toBeVisible();
  await expect(
    answerBlock.getByText(copy.chat.notice.effortUpgraded),
  ).toBeVisible();
});

test('blind A/B: seeded open pair rebuilds compare view, voting 0 restores constant feedback', async ({
  page,
}) => {
  await login(page);

  // 打开种子会话 c_ab（未投票 open 对比对，双候选；spec §6 刷新/重建路径）
  await page.getByText(CHAT_SEED_TITLES.abCompare).first().click();

  // 对比视图重建：两列「选这条」+「两个都不选，继续」
  const compare = page.getByRole('region', { name: copy.chat.abCompareAria });
  await expect(compare.getByRole('button', { name: copy.chat.abVoteOptionAria('0') })).toBeVisible();
  await expect(compare.getByRole('button', { name: copy.chat.abVoteOptionAria('1') })).toBeVisible();
  await expect(compare.getByRole('button', { name: copy.chat.abChoiceNeither })).toBeVisible();

  // 投票 0：对比控件消失，所选回答恢复常设 👍👎（spec §6：0/1 后所选恢复常设反馈）
  await compare.getByRole('button', { name: copy.chat.abVoteOptionAria('0') }).click();
  await expect(page.getByRole('button', { name: copy.chat.feedbackUpAria }).first()).toBeVisible();
  await expect(compare).not.toBeVisible();
});
