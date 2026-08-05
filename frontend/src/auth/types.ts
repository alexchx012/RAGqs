/*
 * 认证域类型（契约《前端接口需求.md》§2）。
 * 角色目录固定，一名用户只持有一个角色；时间一律 ISO 8601 UTC 字符串。
 */

export type Role = 'user' | 'minister' | 'ops' | 'admin';

export interface DepartmentRef {
  readonly id: string;
  readonly name: string;
}

/** User 结构全端共用，POST /auth/login 与 GET /auth/me 同构。 */
export interface User {
  readonly id: string;
  readonly username: string;
  readonly display_name: string;
  readonly real_name: string;
  readonly department: DepartmentRef | null;
  readonly role: Role;
  readonly avatar_url: string | null;
}

/** GET /auth/sessions 的单行：设置页「活跃会话」卡数据（UI 在 fe-settings-personal）。 */
export interface DeviceSession {
  readonly id: string;
  readonly device: string;
  readonly last_active_at: string;
  readonly current: boolean;
}
