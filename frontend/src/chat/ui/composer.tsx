/*
 * 输入区（共用基座 §3.3；spec §3）。
 * 上行工具条（努力档位分段开关 + 检索范围 chip）→ 下行多行输入框。
 * - 多行自适应 1–8 行，超出内部滚动；Enter 发送，Shift+Enter 换行（基座 §3 未明确规定换行修饰键，
 *   按主流 chat 产品惯例以 Shift+Enter 换行，规避 IME 组合输入的误发）。
 * - 发送键 40px 圆形墨色底白箭头；空输入禁用（mist-gray 底 smoke 图标）。
 * - 生成中发送键变停止键（白色方块图标，150ms 交叉淡变）；收到 start 前不可停止（disabled）；
 *   点击即「正在停止」禁重复（stopRequested/stopping 时禁用）。
 * 范围选择与档位由父组件记忆（会话内记住、新会话重置）。
 */

import { ArrowUp, Square } from 'lucide-react';
import { useRef, useState, type KeyboardEvent } from 'react';
import { copy } from '../../copy';
import { SegmentedControl } from '../../ui/SegmentedControl';
import type { EffortLevel } from '../types';
import { ScopeChip, type ScopeDocument, type ScopeSelection } from './scope-chip';
import type { SpaceItem } from '../types';

export interface ComposerProps {
  readonly effortLevel: EffortLevel;
  readonly onEffortChange: (level: EffortLevel) => void;
  readonly spaces: readonly SpaceItem[];
  readonly onFetchDocuments: (spaceId: string, q?: string) => Promise<ScopeDocument[] | null>;
  readonly selection: ScopeSelection;
  readonly onSelectionChange: (selection: ScopeSelection) => void;
  /** 生成中：发送键变停止键。 */
  readonly generating: boolean;
  /** 收到 start 前不可停止（spec §3）。 */
  readonly canStop: boolean;
  /** 正在停止：停止键禁用，禁止重复操作。 */
  readonly stopping: boolean;
  readonly onSend: (content: string) => boolean | void | Promise<boolean | void>;
  readonly onStop: () => void;
}

const EFFORT_OPTIONS = [
  { value: 'quick', label: copy.chat.composer.effortQuick },
  { value: 'think', label: copy.chat.composer.effortThink },
  { value: 'deep', label: copy.chat.composer.effortDeep, accent: true },
];

export function Composer({
  effortLevel,
  onEffortChange,
  spaces,
  onFetchDocuments,
  selection,
  onSelectionChange,
  generating,
  canStop,
  stopping,
  onSend,
  onStop,
}: ComposerProps) {
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const empty = content.trim() === '';

  const submit = async () => {
    const trimmed = content.trim();
    if (trimmed === '' || generating || submitting) return;
    setSubmitting(true);
    try {
      const accepted = await onSend(trimmed);
      if (accepted !== false) {
        setContent((current) => (current.trim() === trimmed ? '' : current));
        if (textareaRef.current !== null && textareaRef.current.value.trim() === trimmed) {
          textareaRef.current.style.height = 'auto';
        }
      }
    } catch {
      // 发送失败时保留输入，允许用户重试。
    } finally {
      setSubmitting(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送；Shift+Enter 换行（规避 IME 组合输入误发；基座 §3 未规定修饰键，按主流约定）
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  };

  const resize = () => {
    const target = textareaRef.current;
    if (target === null) return;
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, 8 * 24)}px`;
  };

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <SegmentedControl
          options={EFFORT_OPTIONS}
          value={effortLevel}
          onChange={(value) => onEffortChange(value as EffortLevel)}
          ariaLabel={copy.chat.composer.effortAria}
        />
        <ScopeChip
          spaces={spaces}
          onFetchDocuments={onFetchDocuments}
          selection={selection}
          onSelectionChange={onSelectionChange}
        />
      </div>
      <div className="flex items-end gap-2 rounded-[var(--radius-inputs)] border border-hairline bg-paper-white p-4 transition-colors duration-[var(--duration-fast)] focus-within:border-ink-black">
        <textarea
          ref={textareaRef}
          value={content}
          rows={1}
          onChange={(event) => {
            setContent(event.target.value);
            resize();
          }}
          onKeyDown={onKeyDown}
          placeholder={copy.chat.composer.inputPlaceholder}
          aria-label={copy.chat.composer.inputPlaceholder}
          className="max-h-[192px] min-h-[24px] flex-1 resize-none overflow-y-auto text-[16px] leading-[1.35] text-ink-black outline-none placeholder:text-smoke-gray"
        />
        {generating ? (
          <button
            type="button"
            aria-label={stopping ? copy.chat.composer.stoppingAria : copy.chat.composer.stopAria}
            disabled={!canStop || stopping}
            onClick={onStop}
            className={
              'flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-opacity duration-[var(--duration-fast)] ' +
              (canStop && !stopping
                ? 'bg-ink-black text-paper-white hover:opacity-[0.88]'
                : 'bg-mist-gray text-smoke-gray')
            }
          >
            <Square aria-hidden="true" className="h-3.5 w-3.5 fill-current" />
          </button>
        ) : (
          <button
            type="button"
            aria-label={copy.chat.composer.sendAria}
            disabled={empty || submitting}
            onClick={() => void submit()}
            className={
              'flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-opacity duration-[var(--duration-fast)] ' +
              (empty || submitting ? 'bg-mist-gray text-smoke-gray' : 'bg-ink-black text-paper-white hover:opacity-[0.88]')
            }
          >
            <ArrowUp aria-hidden="true" className="h-5 w-5" />
          </button>
        )}
      </div>
    </div>
  );
}
