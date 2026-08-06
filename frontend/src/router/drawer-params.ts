/*
 * 以 URL 表达抽屉开关与无限下钻层级的约定（shared-shell 规格 §3：路径段式，可读、可深链）。
 *
 * 约定（生产环境经后端 SPA 深链回退托管，刷新 / 粘贴链接均可用）：
 * - /settings                          打开抽屉，个人段顶层（无选中模块）
 * - /settings/knowledge/uploads        个人段下钻两层；路径段有序、可任意深度
 * - /admin                             打开抽屉，管理段顶层
 * - /admin/dashboard                   管理段选中「总览」
 * - 其余路径（含 /）表示抽屉关闭。
 */

export const PERSONAL_ROOT = 'settings';
export const ADMIN_ROOT = 'admin';

export type DrawerSegment = 'personal' | 'admin';

export interface DrawerLocation {
  /** 抽屉是否打开。 */
  readonly open: boolean;
  /** 当前所在段；关闭时为 null。 */
  readonly segment: DrawerSegment | null;
  /** 下钻层级（路径段，有序、可任意深度）；空数组表示位于该段顶层。 */
  readonly drill: readonly string[];
}

const CLOSED: DrawerLocation = { open: false, segment: null, drill: [] };

export function parseDrawerLocation(pathname: string): DrawerLocation {
  const segments = pathname.split('/').filter((segment) => segment.length > 0);
  const [root, ...drill] = segments;
  if (root === PERSONAL_ROOT) {
    return { open: true, segment: 'personal', drill };
  }
  if (root === ADMIN_ROOT) {
    return { open: true, segment: 'admin', drill };
  }
  return CLOSED;
}

/** 序列化为路径；open=false 时忽略 segment 与 drill（下钻只能存在于打开的抽屉内）。 */
export function formatDrawerLocation(location: DrawerLocation): string {
  if (!location.open || location.segment === null) {
    return '/';
  }
  const root = location.segment === 'personal' ? PERSONAL_ROOT : ADMIN_ROOT;
  return `/${[root, ...location.drill].join('/')}`;
}
