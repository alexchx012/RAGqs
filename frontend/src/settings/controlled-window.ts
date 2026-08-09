/*
 * 受控新窗口打开辅助（review：window.open + noopener 在真实浏览器返回 null，
 * 会被误判 popup blocked）。安全模式：不加 noopener 打开以获得 Window 引用，
 * 立即把 opener 置 null 切断反向引用（避免 opener 泄露），随后由调用方
 * document.write 加载页 + 异步 fetch 后 navigate blob。
 */

export function openControlledWindow(): Window | null {
  let windowRef: Window | null = null;
  try {
    // 不加 noopener/noreferrer：noopener 会使返回值恒为 null，无法加载/导航受控窗口。
    // 打开后立即切断 opener（现代浏览器允许，window.opener 置 null 即不可被反向访问）。
    windowRef = window.open('', '_blank');
  } catch {
    return null;
  }
  if (windowRef === null) {
    return null;
  }
  try {
    windowRef.opener = null;
  } catch {
    // 个别环境禁止写 opener：忽略（opener 关系保持但功能不受影响）
  }
  return windowRef;
}
