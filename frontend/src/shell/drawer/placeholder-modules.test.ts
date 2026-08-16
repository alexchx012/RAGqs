import { isValidElement } from 'react';
import { describe, expect, it } from 'vitest';
import {
  ApprovalsSummaryBadge,
  EvaluationWindowDot,
  OperationsStaleBadge,
  QuotaRequestsSummaryBadge,
} from '../../admin/summaries';
import { copy } from '../../copy';
import { AppearanceModule } from '../../settings/AppearanceModule';
import { ProfileModule } from '../../settings/ProfileModule';
import { SecurityModule } from '../../settings/SecurityModule';
import { createDrawerRegistry } from './DrawerRegistryProvider';
import { createPlaceholderModules } from './placeholder-modules';

const modules = copy.shell.drawer.modules;
const ALL_ROLES = ['user', 'minister', 'ops', 'admin'] as const;

const ids = (layers: readonly { id: string }[]) => layers.map((layer) => layer.id);

describe('内置占位模块：个人段（全角色四模块，顺序固定）', () => {
  it.each(ALL_ROLES)('%s 角色看到且仅看到四个固定模块', (role) => {
    const list = createDrawerRegistry().listModules('personal', role);
    expect(ids(list)).toEqual(['profile', 'security', 'appearance', 'knowledge']);
    expect(list.map((module) => module.title)).toEqual([
      modules.profile,
      modules.security,
      modules.appearance,
      modules.knowledge,
    ]);
  });

  it('在首次 placeholder 注册内合成 Profile/Security/Appearance real render，不重复登记模块 ID', () => {
    const registry = createDrawerRegistry();
    const list = registry.listModules('personal', 'user');
    const profile = registry.resolve('personal', ['profile'], 'user').layers[0];
    const security = registry.resolve('personal', ['security'], 'user').layers[0];
    const appearance = registry.resolve('personal', ['appearance'], 'user').layers[0];

    expect(new Set(ids(list)).size).toBe(list.length);
    expect(profile?.render).toBeDefined();
    expect(security?.render).toBeDefined();
    expect(appearance?.render).toBeDefined();

    const profileNode = profile?.render?.({ path: ['profile'] });
    const securityNode = security?.render?.({ path: ['security'] });
    const appearanceNode = appearance?.render?.({ path: ['appearance'] });
    expect(isValidElement(profileNode)).toBe(true);
    expect(isValidElement(securityNode)).toBe(true);
    expect(isValidElement(appearanceNode)).toBe(true);
    if (!isValidElement(profileNode) || !isValidElement(securityNode) || !isValidElement(appearanceNode)) {
      throw new Error('profile, security, and appearance renderers must return elements');
    }
    expect(profileNode.type).toBe(ProfileModule);
    expect(securityNode.type).toBe(SecurityModule);
    expect(appearanceNode.type).toBe(AppearanceModule);
  });

  it('knowledge 注册 uploads/submissions/versions/manage 下层，approvals 为 manage 的子层（标题取自文案常量）', () => {
    const knowledge = createPlaceholderModules().find((module) => module.id === 'knowledge');
    expect(knowledge?.title).toBe(modules.knowledge);
    expect(ids(knowledge?.children ?? [])).toEqual(['uploads', 'submissions', 'versions', 'manage']);
    expect(knowledge?.children?.map((child) => child.title)).toEqual([
      modules.uploads,
      modules.submissions,
      modules.versions,
      modules.manage,
    ]);
    // 投稿审核是部门库管理下的正确子层（返回回到部门库管理）
    const manage = knowledge?.children?.find((child) => child.id === 'manage');
    expect(ids(manage?.children ?? [])).toEqual(['approvals']);
    expect(manage?.children?.map((child) => child.title)).toEqual([modules.knowledgeApprovals]);
  });

  it('uploads/versions 全角色可解析；submissions 仅 user/minister；manage 仅部长，approvals 为 manage 子层', () => {
    const registry = createDrawerRegistry();
    for (const role of ALL_ROLES) {
      expect(registry.resolve('personal', ['knowledge', 'uploads'], role).exact).toBe(true);
      expect(registry.resolve('personal', ['knowledge', 'versions'], role).exact).toBe(true);
    }
    for (const role of ['user', 'minister'] as const) {
      const resolved = registry.resolve('personal', ['knowledge', 'submissions'], role);
      expect(resolved.exact).toBe(true);
      expect(ids(resolved.layers)).toEqual(['knowledge', 'submissions']);
    }
    for (const role of ['ops', 'admin'] as const) {
      const resolved = registry.resolve('personal', ['knowledge', 'submissions'], role);
      expect(resolved.exact).toBe(false);
      expect(ids(resolved.layers)).toEqual(['knowledge']);
    }
    for (const role of ALL_ROLES) {
      const resolved = registry.resolve('personal', ['knowledge', 'manage'], role);
      expect(resolved.exact).toBe(role === 'minister');
      // 部长经 manage 子层解析 approvals；非部长停在 knowledge（manage 不可见）
      const approvals = registry.resolve('personal', ['knowledge', 'manage', 'approvals'], role);
      expect(approvals.exact).toBe(role === 'minister');
      expect(ids(approvals.layers)).toEqual(
        role === 'minister' ? ['knowledge', 'manage', 'approvals'] : ['knowledge'],
      );
    }
  });
});

describe('内置占位模块：管理段（按角色渲染）', () => {
  it('ops 见六个固定模块，users 为运维口径标题', () => {
    const list = createDrawerRegistry().listModules('admin', 'ops');
    expect(ids(list)).toEqual([
      'dashboard',
      'approvals',
      'spaces',
      'evaluation',
      'operations',
      'users',
    ]);
    expect(list.map((module) => module.title)).toEqual([
      modules.dashboard,
      modules.approvals,
      modules.spaces,
      modules.evaluation,
      modules.operations,
      modules.usersOps,
    ]);
  });

  it('admin 见五个固定模块（无 approvals），users 为超管口径标题', () => {
    const list = createDrawerRegistry().listModules('admin', 'admin');
    expect(ids(list)).toEqual(['dashboard', 'spaces', 'evaluation', 'operations', 'users']);
    expect(list.map((module) => module.title)).toEqual([
      modules.dashboard,
      modules.spaces,
      modules.evaluation,
      modules.operations,
      modules.usersAdmin,
    ]);
  });

  it('user/minister 无任何管理模块（hasAdminModules 为 false）；ops/admin 为 true', () => {
    const registry = createDrawerRegistry();
    for (const role of ['user', 'minister'] as const) {
      expect(registry.listModules('admin', role)).toEqual([]);
      expect(registry.hasAdminModules(role)).toBe(false);
    }
    for (const role of ['ops', 'admin'] as const) {
      expect(registry.hasAdminModules(role)).toBe(true);
    }
  });

  it('spaces 注册公共库 / 用户个人库 / 部门库 / 投稿审核下层', () => {
    const spaces = createPlaceholderModules().find((module) => module.id === 'spaces');
    expect(ids(spaces?.children ?? [])).toEqual(['public', 'personal-libs', 'department-libs', 'submissions']);
    expect(spaces?.children?.[0]?.title).toBe(modules.publicSpace);
  });

  it('approvals / operations / users(admin) 注册下钻子层', () => {
    const modules2 = createPlaceholderModules();
    const approvals = modules2.find((module) => module.id === 'approvals' && module.roles?.includes('ops'));
    expect(ids(approvals?.children ?? [])).toEqual(['quota', 'submissions']);
    const operations = modules2.find((module) => module.id === 'operations');
    expect(ids(operations?.children ?? [])).toEqual(['jobs', 'metrics']);
    const usersAdmin = modules2.find(
      (module) => module.id === 'users' && module.roles?.includes('admin'),
    );
    expect(ids(usersAdmin?.children ?? [])).toEqual(['departments']);
  });

  it('左栏摘要接线：审批中心只保留配额摘要，评测与系统运维保留既有摘要', () => {
    const modules2 = createPlaceholderModules();
    const summaryType = (layer: { renderSummary?: () => unknown } | undefined) => {
      const node = layer?.renderSummary?.();
      expect(isValidElement(node)).toBe(true);
      return isValidElement(node) ? node.type : null;
    };
    const approvals = modules2.find((module) => module.id === 'approvals' && module.roles?.includes('ops'));
    expect(summaryType(approvals)).toBe(ApprovalsSummaryBadge);
    expect(summaryType(approvals?.children?.find((child) => child.id === 'quota'))).toBe(
      QuotaRequestsSummaryBadge,
    );
    expect(approvals?.children?.find((child) => child.id === 'submissions')?.renderSummary).toBeUndefined();
    expect(summaryType(modules2.find((module) => module.id === 'evaluation'))).toBe(
      EvaluationWindowDot,
    );
    const operations = modules2.find((module) => module.id === 'operations');
    expect(summaryType(operations)).toBe(OperationsStaleBadge);
    // 系统运维「任务队列」子层同样带超时徽标
    expect(summaryType(operations?.children?.find((child) => child.id === 'jobs'))).toBe(
      OperationsStaleBadge,
    );
    // 超管知识空间「投稿审核」子层没有服务端未实现的投稿待审徽标
    const spaces = modules2.find((module) => module.id === 'spaces');
    expect(spaces?.children?.find((child) => child.id === 'submissions')?.renderSummary).toBeUndefined();
    // personal 段不用摘要 slot
    expect(modules2.find((module) => module.id === 'profile')?.renderSummary).toBeUndefined();
    expect(modules2.find((module) => module.id === 'dashboard')?.renderSummary).toBeUndefined();
  });
});

describe('内置占位模块：深链解析示例', () => {
  it('ops 完整解析 spaces/public 两层', () => {
    const resolved = createDrawerRegistry().resolve('admin', ['spaces', 'public'], 'ops');
    expect(resolved.exact).toBe(true);
    expect(ids(resolved.layers)).toEqual(['spaces', 'public']);
    expect(resolved.layers.map((layer) => layer.title)).toEqual([
      modules.spaces,
      modules.publicSpace,
    ]);
  });

  it('approvals 对 admin 按未注册处理（ops 可完整命中）', () => {
    const registry = createDrawerRegistry();
    expect(registry.resolve('admin', ['approvals'], 'admin')).toEqual({
      layers: [],
      exact: false,
    });
    expect(registry.resolve('admin', ['approvals'], 'ops').exact).toBe(true);
  });
});
