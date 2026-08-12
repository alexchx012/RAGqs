/*
 * 抽屉模块注册机制（shared-shell 规格 §1）。
 * - 各业务模块（后续 change 实现）向抽屉注册左栏项、首层内容与下钻路由；
 *   本 change 只内置占位模块（个人四模块 + 知识库下钻两层 + 管理段模块）。
 * - 「无权限模块不渲染」：注册项带 roles 白名单，按角色渲染不同左栏清单；鉴权以后端为准。
 * - 下钻层数不限：层以 children 递归嵌套，不硬编码层数上限（规格 §2）。
 */

import type { ReactNode } from 'react';
import type { Role } from '../../auth/types';
import type { DrawerSegment } from '../../router/drawer-params';

export interface DrawerLayerRenderContext {
  /** 当前层完整 drill 路径（含本层 id）。 */
  readonly path: readonly string[];
}

export interface DrawerLayer {
  /** URL 路径段（同一父层内唯一）。 */
  readonly id: string;
  /** 层名（左栏第一位 / 触发下钻行显示）。 */
  readonly title: string;
  /** 下级菜单（递归，不限层数）；存在时本层右栏默认渲染下钻行列表。 */
  readonly children?: readonly DrawerLayer[];
  /** 自定义内容渲染；缺省时：有 children 渲染下钻行列表，无 children 渲染通用占位。 */
  readonly render?: (context: DrawerLayerRenderContext) => ReactNode;
  /** 项右侧摘要 slot（徽标 / 状态点；fe-admin-panels 管理段用，personal 段不用）。
   *  渲染在左栏模块按钮与下钻行内、chevron 左侧；组件自行静默（加载中 / 出错渲染 null）。 */
  readonly renderSummary?: () => ReactNode;
  /** 可见角色白名单；缺省全部角色可见。 */
  readonly roles?: readonly Role[];
}

export interface DrawerModule extends DrawerLayer {
  /** 所属左栏段。 */
  readonly segment: DrawerSegment;
  /** 左栏固定顺序（小 → 大）。 */
  readonly order: number;
}

/** 深链解析结果：沿 drill 命中的层链（首元素为模块层）。 */
export interface ResolvedDrill {
  /** 命中的层链；空数组表示首段（模块）未注册，调用方落抽屉顶层占位（规格 §3）。 */
  readonly layers: readonly DrawerLayer[];
  /** drill 是否完整命中；false 表示更深的路径段未注册，停在最深已注册层占位。 */
  readonly exact: boolean;
}

function visibleTo(layer: DrawerLayer, role: Role): boolean {
  return layer.roles === undefined || layer.roles.includes(role);
}

export class DrawerRegistry {
  private readonly modules: DrawerModule[] = [];

  register(module: DrawerModule): void {
    this.modules.push(module);
  }

  /** 左栏清单：按段 + 角色过滤，按固定顺序排列；同 id 不同角色的注册只出现一次。 */
  listModules(segment: DrawerSegment, role: Role): DrawerModule[] {
    const seen = new Set<string>();
    return this.modules
      .filter((module) => module.segment === segment && visibleTo(module, role))
      .sort((a, b) => a.order - b.order)
      .filter((module) => {
        if (seen.has(module.id)) {
          return false;
        }
        seen.add(module.id);
        return true;
      });
  }

  /** 管理段是否对角色可见（用于是否渲染「管理」段标签与发丝线）。 */
  hasAdminModules(role: Role): boolean {
    return this.listModules('admin', role).length > 0;
  }

  /** 沿 drill 逐层解析；首段未注册返回空层链，更深层未注册停在最深已注册层。 */
  resolve(segment: DrawerSegment, drill: readonly string[], role: Role): ResolvedDrill {
    const [moduleId, ...rest] = drill;
    if (moduleId === undefined) {
      return { layers: [], exact: true };
    }
    const module = this.modules.find(
      (candidate) =>
        candidate.segment === segment && candidate.id === moduleId && visibleTo(candidate, role),
    );
    if (module === undefined) {
      return { layers: [], exact: false };
    }
    const layers: DrawerLayer[] = [module];
    let current: DrawerLayer = module;
    for (const id of rest) {
      const child = current.children?.find((candidate) => candidate.id === id && visibleTo(candidate, role));
      if (child === undefined) {
        return { layers, exact: false };
      }
      layers.push(child);
      current = child;
    }
    return { layers, exact: true };
  }
}
