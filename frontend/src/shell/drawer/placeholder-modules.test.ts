import { describe, expect, it } from 'vitest';
import { copy } from '../../copy';
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

  it('knowledge 注册 uploads 与 submissions 两个下层（标题取自文案常量）', () => {
    const knowledge = createPlaceholderModules().find((module) => module.id === 'knowledge');
    expect(knowledge?.title).toBe(modules.knowledge);
    expect(ids(knowledge?.children ?? [])).toEqual(['uploads', 'submissions']);
    expect(knowledge?.children?.map((child) => child.title)).toEqual([
      modules.uploads,
      modules.submissions,
    ]);
  });

  it('uploads 全角色可解析；submissions 仅 user/minister 可解析', () => {
    const registry = createDrawerRegistry();
    for (const role of ALL_ROLES) {
      expect(registry.resolve('personal', ['knowledge', 'uploads'], role).exact).toBe(true);
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

  it('spaces 注册 public 下层（公共库）', () => {
    const spaces = createPlaceholderModules().find((module) => module.id === 'spaces');
    expect(ids(spaces?.children ?? [])).toEqual(['public']);
    expect(spaces?.children?.[0]?.title).toBe(modules.publicSpace);
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
