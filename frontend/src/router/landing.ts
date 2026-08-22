/*
 * 按角色落地（规格 §4；登录页设计 §1）。
 * 普通用户 / 部长 / 超管 → 聊天主页；运维 → 聊天主页并携带「自动展开管理抽屉」导航 state，
 * 该标记由后续 change（抽屉状态机）消费。
 * 「新登录落地」标记不走导航 state（守卫的 replace 会抢先于登录页的延迟导航消费掉它），
 * 改由 AuthSessionStore 的一次性标记承载（仅交互式 login 置位），主页挂载时读取。
 */

import type { Role } from '../auth/types';

export const AUTO_OPEN_ADMIN_DRAWER_STATE_KEY = 'autoOpenAdminDrawer';

export interface LandingTarget {
  readonly path: string;
  readonly state?: Record<string, unknown>;
}

export function landingTargetFor(role: Role): LandingTarget {
  if (role === 'ops') {
    return { path: '/', state: { [AUTO_OPEN_ADMIN_DRAWER_STATE_KEY]: true } };
  }
  return { path: '/' };
}
