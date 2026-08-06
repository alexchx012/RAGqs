/*
 * 抽屉内置占位模块（shared-shell 规格 §1、§3）。
 * 业务模块内容在后续 change 实现（fe-settings-personal / fe-admin-panels 等）；
 * 本 change 先以占位内容注册固定模块名与下钻路由，保证：
 * - 左栏「个人/管理」两段结构、按角色渲染（无权限模块不渲染）；
 * - 铃铛跳转目标层可落位（已注册层渲染占位，未注册层由 DrawerHost 落抽屉首层占位）。
 * 模块名与顺序以《共用基座设计.md》§5、《运维端设计.md》§7.1、《超管端设计.md》§7.1 为准。
 */

import { copy } from '../../copy';
import type { DrawerModule } from './registry';

const modules = copy.shell.drawer.modules;
const MEMBER_ROLES = ['user', 'minister'] as const;
const OPS = ['ops'] as const;
const ADMIN = ['admin'] as const;
const ADMIN_SEGMENT_ROLES = ['ops', 'admin'] as const;

export function createPlaceholderModules(): DrawerModule[] {
  return [
    // 个人段（全角色；顺序固定）
    { id: 'profile', title: modules.profile, segment: 'personal', order: 10 },
    { id: 'security', title: modules.security, segment: 'personal', order: 20 },
    { id: 'appearance', title: modules.appearance, segment: 'personal', order: 30 },
    {
      id: 'knowledge',
      title: modules.knowledge,
      segment: 'personal',
      order: 40,
      children: [
        // 上传结果层（§5.7）全角色共用；我的投稿层仅普通用户与部长
        { id: 'uploads', title: modules.uploads },
        { id: 'submissions', title: modules.submissions, roles: MEMBER_ROLES },
      ],
    },
    // 管理段：运维六模块（《运维端设计.md》§7.1）
    { id: 'dashboard', title: modules.dashboard, segment: 'admin', order: 10, roles: ADMIN_SEGMENT_ROLES },
    { id: 'approvals', title: modules.approvals, segment: 'admin', order: 20, roles: OPS },
    {
      id: 'spaces',
      title: modules.spaces,
      segment: 'admin',
      order: 30,
      roles: ADMIN_SEGMENT_ROLES,
      children: [{ id: 'public', title: modules.publicSpace }],
    },
    { id: 'evaluation', title: modules.evaluation, segment: 'admin', order: 40, roles: ADMIN_SEGMENT_ROLES },
    { id: 'operations', title: modules.operations, segment: 'admin', order: 50, roles: ADMIN_SEGMENT_ROLES },
    // 同一模块 id 按角色给不同名：运维「用户管理」、超管「人员与权限」
    { id: 'users', title: modules.usersOps, segment: 'admin', order: 60, roles: OPS },
    { id: 'users', title: modules.usersAdmin, segment: 'admin', order: 60, roles: ADMIN },
  ];
}
