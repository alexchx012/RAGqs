import React, { useRef, useState } from 'react';
import { useChat } from '../chat/ChatContext';

const ACCEPTED_TYPES = '.txt,.md,.markdown,.csv,.html,.htm,.json';

interface FileUploadProps {
  spaceId: string;
  disabled?: boolean;
  onRefresh: () => void;
}

export default function FileUpload({ spaceId, disabled = false, onRefresh }: FileUploadProps) {
  const { addMessage } = useChat();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isUploading, setIsUploading] = useState(false);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setIsUploading(true);
    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch(`/api/upload?space_id=${encodeURIComponent(spaceId)}`, { method: 'POST', body: formData });
      const data = await res.json();

      if (data.code === 200) {
        addMessage({ type: 'assistant', content: `✅ 文件 "${file.name}" 上传成功，已建立向量索引。` });
        onRefresh();
      } else {
        addMessage({ type: 'assistant', content: `❌ 上传失败: ${data.detail || data.message || '未知错误'}` });
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '上传出错';
      addMessage({ type: 'assistant', content: `❌ 上传出错: ${message}` });
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <>
      <button
        className="tools-btn"
        title={isUploading ? '上传中...' : '上传文件'}
        onClick={() => fileInputRef.current?.click()}
        disabled={disabled || isUploading}
        style={{ opacity: isUploading ? 0.6 : 1 }}
      >
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <input ref={fileInputRef} type="file" accept={ACCEPTED_TYPES} style={{ display: 'none' }} onChange={handleFileChange} />
    </>
  );
}
