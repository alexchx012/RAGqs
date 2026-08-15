export function downloadSubmissionContent(content: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(content);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(objectUrl);
}
