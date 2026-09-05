/*
 * 下钻层跨层轻提示（A43）：版本恢复成功后自动导航到上传结果层，
 * 成功提示改在目标层（uploads）展示，而不是写在即将卸载的发起层。
 * 用 session 作用域的小型模块级 store 暂存一条一次性提示（take 语义：读即清）。
 * 仅内存态（刷新/登出即清）；按 authSessionId:userId 隔离（同 manage-context）。
 */

const noticesBySession = new Map<string, string>();

/** 写入一次性层间提示（sessionKey 为空时忽略）。 */
export function setLayerNotice(sessionKey: string | null, message: string): void {
  if (sessionKey === null || sessionKey === '') {
    return;
  }
  noticesBySession.set(sessionKey, message);
}

/** 读取并清除当前会话的一次性层间提示；无提示返回 null。 */
export function takeLayerNotice(sessionKey: string | null): string | null {
  if (sessionKey === null || sessionKey === '') {
    return null;
  }
  const message = noticesBySession.get(sessionKey);
  if (message === undefined) {
    return null;
  }
  noticesBySession.delete(sessionKey);
  return message;
}

/** 清空指定会话（或全部）的层间提示（测试/登出清理用）。 */
export function clearLayerNotices(sessionKey: string | null = null): void {
  if (sessionKey === null) {
    noticesBySession.clear();
  } else {
    noticesBySession.delete(sessionKey);
  }
}
