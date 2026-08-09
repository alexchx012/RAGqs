/*
 * 部门库管理跨层上下文（review Major 5：approvals 返回保留原部门）。
 * ManageLayer 与 ApprovalsLayer 是抽屉相邻下钻层，各自独立挂载/卸载；
 * 用 session 作用域的小型模块级 store 保存当前选中部门空间，返回时恢复。
 * 仅内存态（刷新/登出即清）；按 authSessionId:userId 隔离。
 */

const selectedSpaceBySession = new Map<string, string>();

export function getManageSpaceSelection(sessionKey: string | null): string | null {
  if (sessionKey === null || sessionKey === '') {
    return null;
  }
  return selectedSpaceBySession.get(sessionKey) ?? null;
}

export function setManageSpaceSelection(sessionKey: string | null, spaceId: string | null): void {
  if (sessionKey === null || sessionKey === '') {
    return;
  }
  if (spaceId === null) {
    selectedSpaceBySession.delete(sessionKey);
  } else {
    selectedSpaceBySession.set(sessionKey, spaceId);
  }
}
