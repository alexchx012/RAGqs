/*
 * 以 URL 表达抽屉开关与无限下钻层级的约定（规格 §5：路由基础设施预留能力；
 * 具体抽屉状态机在 fe-shared-shell 实现）。
 *
 * 约定：
 * - ?drawer=settings                        打开名为 settings 的抽屉；无 drawer 参数即关闭。
 * - ?drawer=settings&drill=personal&drill=profile
 *   drill 为有序可重复参数，表达无限下钻层级（第一项为最浅层）。
 */

export const DRAWER_PARAM = 'drawer';
export const DRILL_PARAM = 'drill';

export interface DrawerLocation {
  /** 抽屉名；null 表示关闭。 */
  readonly drawer: string | null;
  /** 下钻层级，有序、可任意深度；空数组表示位于抽屉顶层。 */
  readonly drill: readonly string[];
}

export function parseDrawerLocation(search: string): DrawerLocation {
  const params = new URLSearchParams(search);
  return {
    drawer: params.get(DRAWER_PARAM),
    drill: params.getAll(DRILL_PARAM).filter((level) => level.length > 0),
  };
}

/** 序列化为 search 字符串；drawer 为 null 时忽略 drill（下钻只能存在于打开的抽屉内）。 */
export function formatDrawerLocation(location: DrawerLocation): string {
  const params = new URLSearchParams();
  if (location.drawer !== null) {
    params.set(DRAWER_PARAM, location.drawer);
    for (const level of location.drill) {
      params.append(DRILL_PARAM, level);
    }
  }
  const query = params.toString();
  return query.length > 0 ? `?${query}` : '';
}
