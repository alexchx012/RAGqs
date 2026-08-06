import { describe, expect, it } from 'vitest';
import { DrawerRegistry } from './registry';

// 自造模块：故意乱序登记、含角色白名单、同 id 多注册与四层嵌套，覆盖清单与深链解析行为
function createRegistry(): DrawerRegistry {
  const registry = new DrawerRegistry();
  // 个人段
  registry.register({ id: 'alpha', title: '甲模块', segment: 'personal', order: 30 });
  registry.register({ id: 'beta', title: '乙模块', segment: 'personal', order: 10 });
  registry.register({
    id: 'gamma',
    title: '丙模块',
    segment: 'personal',
    order: 20,
    roles: ['ops', 'admin'],
  });
  // 四层嵌套下钻（验证「下钻层数不限」）；l1 下另挂一个仅 admin 可见的受限层
  registry.register({
    id: 'root',
    title: '根模块',
    segment: 'personal',
    order: 40,
    children: [
      {
        id: 'l1',
        title: '第一层',
        children: [
          {
            id: 'l2',
            title: '第二层',
            children: [
              { id: 'l3', title: '第三层', children: [{ id: 'l4', title: '第四层' }] },
            ],
          },
          { id: 'l2-secret', title: '受限第二层', roles: ['admin'] },
        ],
      },
    ],
  });
  // 管理段：同 id 按角色注册不同标题；dup 同 id 重复登记验证清单去重
  registry.register({
    id: 'panel',
    title: '面板',
    segment: 'admin',
    order: 10,
    roles: ['ops', 'admin'],
  });
  registry.register({ id: 'users', title: '运维用户', segment: 'admin', order: 20, roles: ['ops'] });
  registry.register({
    id: 'users',
    title: '超管用户',
    segment: 'admin',
    order: 20,
    roles: ['admin'],
  });
  registry.register({
    id: 'dup',
    title: '重复模块',
    segment: 'admin',
    order: 30,
    roles: ['ops', 'admin'],
  });
  registry.register({
    id: 'dup',
    title: '重复模块再登记',
    segment: 'admin',
    order: 30,
    roles: ['ops', 'admin'],
  });
  return registry;
}

const ids = (layers: readonly { id: string }[]) => layers.map((layer) => layer.id);

describe('DrawerRegistry.listModules（左栏清单）', () => {
  it('按段过滤：只返回目标段的模块', () => {
    const registry = createRegistry();
    expect(ids(registry.listModules('personal', 'user'))).toEqual(['beta', 'alpha', 'root']);
    expect(ids(registry.listModules('admin', 'ops'))).toEqual(['panel', 'users', 'dup']);
  });

  it('按 roles 白名单过滤：无权限模块不出现', () => {
    const registry = createRegistry();
    expect(ids(registry.listModules('personal', 'user'))).not.toContain('gamma');
    expect(ids(registry.listModules('personal', 'ops'))).toContain('gamma');
  });

  it('按 order 升序排列（与登记顺序无关）', () => {
    const registry = createRegistry();
    expect(ids(registry.listModules('personal', 'ops'))).toEqual([
      'beta',
      'gamma',
      'alpha',
      'root',
    ]);
  });

  it('同 id 按角色注册：清单中只出现一次，标题按角色取', () => {
    const registry = createRegistry();
    const opsList = registry.listModules('admin', 'ops');
    expect(opsList.filter((module) => module.id === 'users')).toHaveLength(1);
    expect(opsList.find((module) => module.id === 'users')?.title).toBe('运维用户');
    const adminList = registry.listModules('admin', 'admin');
    expect(adminList.filter((module) => module.id === 'users')).toHaveLength(1);
    expect(adminList.find((module) => module.id === 'users')?.title).toBe('超管用户');
  });

  it('同 id 对同一角色重复登记：清单去重只出现一次', () => {
    const registry = createRegistry();
    const list = registry.listModules('admin', 'ops');
    expect(list.filter((module) => module.id === 'dup')).toHaveLength(1);
    expect(ids(list)).toEqual(['panel', 'users', 'dup']);
  });
});

describe('DrawerRegistry.hasAdminModules（管理段可见性）', () => {
  it('无可见管理模块的角色返回 false', () => {
    const registry = createRegistry();
    expect(registry.hasAdminModules('user')).toBe(false);
    expect(registry.hasAdminModules('minister')).toBe(false);
  });

  it('有可见管理模块的角色返回 true', () => {
    const registry = createRegistry();
    expect(registry.hasAdminModules('ops')).toBe(true);
    expect(registry.hasAdminModules('admin')).toBe(true);
  });
});

describe('DrawerRegistry.resolve（深链解析）', () => {
  it('drill 为空：返回空层链且完整命中', () => {
    const registry = createRegistry();
    expect(registry.resolve('personal', [], 'user')).toEqual({ layers: [], exact: true });
  });

  it('模块 id 未注册：返回空层链且未完整命中', () => {
    const registry = createRegistry();
    expect(registry.resolve('personal', ['ghost'], 'user')).toEqual({
      layers: [],
      exact: false,
    });
  });

  it('完整命中四层下钻：层链长度与顺序正确且 exact', () => {
    const registry = createRegistry();
    const resolved = registry.resolve('personal', ['root', 'l1', 'l2', 'l3', 'l4'], 'user');
    expect(resolved.exact).toBe(true);
    expect(ids(resolved.layers)).toEqual(['root', 'l1', 'l2', 'l3', 'l4']);
    expect(resolved.layers[0]?.title).toBe('根模块');
    expect(resolved.layers[4]?.title).toBe('第四层');
  });

  it('深层某段未注册：停在最深已注册层且未完整命中', () => {
    const registry = createRegistry();
    const resolved = registry.resolve('personal', ['root', 'l1', 'l2', 'ghost', 'l4'], 'user');
    expect(resolved.exact).toBe(false);
    expect(ids(resolved.layers)).toEqual(['root', 'l1', 'l2']);
  });

  it('角色不可见的模块按未注册处理', () => {
    const registry = createRegistry();
    expect(registry.resolve('personal', ['gamma'], 'user')).toEqual({
      layers: [],
      exact: false,
    });
    expect(registry.resolve('personal', ['gamma'], 'ops').exact).toBe(true);
  });

  it('角色不可见的深层按未注册处理：停在最深可见层', () => {
    const registry = createRegistry();
    const denied = registry.resolve('personal', ['root', 'l1', 'l2-secret'], 'user');
    expect(denied.exact).toBe(false);
    expect(ids(denied.layers)).toEqual(['root', 'l1']);
    const allowed = registry.resolve('personal', ['root', 'l1', 'l2-secret'], 'admin');
    expect(allowed.exact).toBe(true);
    expect(ids(allowed.layers)).toEqual(['root', 'l1', 'l2-secret']);
  });

  it('segment 不匹配同 id 模块：视为未命中', () => {
    const registry = createRegistry();
    expect(registry.resolve('admin', ['alpha'], 'user')).toEqual({ layers: [], exact: false });
    expect(registry.resolve('personal', ['panel'], 'ops')).toEqual({ layers: [], exact: false });
  });
});
