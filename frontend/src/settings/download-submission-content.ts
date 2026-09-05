export function downloadSubmissionContent(content: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(content);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  link.click();
  // A48：撤销 blob URL 延迟到首帧之后——同步 revoke 在部分浏览器会让刚触发的下载失效
  window.requestAnimationFrame(() => {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  });
}
