/*
 * 输入区（共用基座 §3.3；spec §3；动效 AI Agent Input 移植）。
 * 单一设计稿输入 frame（chat-composer-* 全局类，项目无 CSS Modules；样式见 styles/chat.css 文末）：
 * 努力档位分段开关与检索范围选择收进「+」菜单（档位为菜单底部区块、范围为右侧 flyout，
 * 复用 scope-chip.tsx 的 ScopeSelector/scopeSummary，与 ScopeChip 浮层同一事实源）。
 * - 编辑器为 contentEditable div（技能药丸 inline 流式排布）；placeholder 走 data-placeholder ::before。
 *   Enter 发送、Shift+Enter 换行；isComposing 守卫中文 IME 组合输入误发（设计稿无此守卫，必须保留）。
 * - 「+」菜单：添加图片/添加文件 → 附件 chips；技能 flyout → 插入药丸。「/」斜杠面板同一技能集
 *   （打开/过滤/方向键/Enter/Tab/Esc/鼠标照设计稿）。设计稿 Model 区块对应努力档位，已入菜单。
 * - 增强药丸（优化输入）仅注入 onEnhance 时显示；增强中播 conic 渐变描边旋转环 + 正文 shimmer，
 *   完成后药丸变「还原」；AbortSignal 中止 / 失败均还原原文；高度变化走 FLIP。生成中隐藏增强药丸。
 * - 发送键 28px 圆形（22px 设计稿几何经 UI 审查放大；不沿用旧 40px）：空输入禁用；生成中变停止键（Square 图标，
 *   canStop/stopping 语义不变）。发送语义：onSend 返回 false 或抛错时保留输入；接受时清空编辑器
 *   （发送期间继续输入的稿件保留）。
 * - 「+」菜单 / 斜杠面板打开期间经 useEscShield 挂空盾：Esc 由面板自身关闭逻辑消费，不下传到抽屉层。
 * 范围选择与档位由父组件记忆（会话内记住、新会话重置）。
 */

import {
  ArrowUp,
  BookOpen,
  ChevronRight,
  Database,
  Image as ImageIcon,
  LoaderCircle,
  Paperclip,
  Plus,
  Square,
  X,
} from 'lucide-react';
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import { copy } from '../../copy';
import { useEscShield } from '../../lib/esc-stack-provider';
import { SegmentedControl } from '../../ui/SegmentedControl';
import type { EffortLevel, SpaceItem } from '../types';
import { ScopeSelector, scopeSummary, type ScopeDocument, type ScopeSelection } from './scope-chip';

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
  /** 输入优化接缝（动效 AI Agent Input）：注入才显示「优化输入」药丸；须兑现 AbortSignal 中止。 */
  readonly onEnhance?: (prompt: string, signal?: AbortSignal) => Promise<string>;
}

const EFFORT_OPTIONS = [
  { value: 'quick', label: copy.chat.composer.effortQuick },
  { value: 'think', label: copy.chat.composer.effortThink },
  { value: 'deep', label: copy.chat.composer.effortDeep },
];

// 技能集（动效 AI Agent Input）：id 固定，名称读 copy
const SKILLS = [
  { id: 'deep-research', name: copy.chat.composer.skillDeepResearch },
  { id: 'code-review', name: copy.chat.composer.skillCodeReview },
  { id: 'web-search', name: copy.chat.composer.skillWebSearch },
  { id: 'summarize', name: copy.chat.composer.skillSummarize },
];

const skillName = (id: string) => SKILLS.find((sk) => sk.id === id)?.name ?? id;

const escapeHtml = (str: string) =>
  str.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] ?? c);

type Phase = 'idle' | 'enhancing' | 'enhanced';
type Attachment = { id: number; name: string; kind: 'image' | 'file' };

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
  onEnhance,
}: ComposerProps) {
  // value 镜像编辑器纯文本（技能药丸贡献其 label），驱动空态/placeholder 与增强/发送逻辑
  const [value, setValue] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [submitting, setSubmitting] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  // 检索范围 flyout（「+」菜单内，点击切换；菜单关闭时收起）
  const [scopeOpen, setScopeOpen] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // 正在播放退场动画、待移除的 chip id
  const [exitingAtt, setExitingAtt] = useState<number[]>([]);

  // 增强药丸退场期间保持挂载，以来时同样的柔和方式离开（chat-composer-pill-in/out 镜像）
  const [pillMounted, setPillMounted] = useState(false);
  const [pillExiting, setPillExiting] = useState(false);

  // 「/」斜杠面板（输入 / 打开同一个技能选择器）
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashQuery, setSlashQuery] = useState('');
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashKeyboard, setSlashKeyboard] = useState(false);

  const editorRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const plusRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const preEnhanceHTML = useRef('');
  const pendingHTML = useRef<string | null>(null);
  // 增强/还原替换前捕获的 frame 高度，供 FLIP 从旧高度动画到新高度而不是跳变
  const flipFrom = useRef<number | null>(null);
  const savedRange = useRef<Range | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const slashOpenRef = useRef(false);
  const slashIndexRef = useRef(0);
  const slashResultsRef = useRef<typeof SKILLS>([]);
  const slashQueryRef = useRef('');
  const slashTokenRef = useRef<{ node: Text; start: number; end: number } | null>(null);
  const ignoreHoverRef = useRef(false);
  const applySlashRef = useRef<(id: string) => void>(() => {});
  const slashKeyLock = useRef(false);

  // Esc 分层：「+」菜单 / 斜杠面板打开期间挂空盾，Esc 不再穿透关闭下层抽屉；
  // 面板自身的 Escape 关闭逻辑（下方 document/window 监听）保留
  useEscShield(menuOpen || slashOpen);

  const hasText = value.trim().length > 0;
  const enhancing = phase === 'enhancing';
  const sendActive = hasText && !enhancing && !submitting;
  const showPill = onEnhance !== undefined && hasText && !enhancing && !generating;
  // 「+」菜单检索范围行摘要（与 ScopeChip 触发器同一事实源 scopeSummary）
  const { summary: scopeText, isDefault: scopeIsDefault } = scopeSummary(spaces, selection);
  const slashResults = SKILLS.filter((sk) =>
    sk.name.toLowerCase().includes(slashQuery.toLowerCase()),
  );
  slashOpenRef.current = slashOpen;
  slashIndexRef.current = slashIndex;
  slashResultsRef.current = slashResults;

  // 聚焦编辑器并把光标落到内容末尾
  const focusEnd = () => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    savedRange.current = range.cloneRange();
  };

  const syncFromEditor = () => {
    const editor = editorRef.current;
    if (!editor) return;
    setValue(editor.textContent ?? '');
    // 标记位于最开头（之前只有空白）的药丸，CSS 据此去掉左 margin —— :first-child 看不见文本节点
    editor.querySelectorAll<HTMLElement>('.chat-composer-skill-pill').forEach((pill) => {
      let atStart = true;
      for (let n = pill.previousSibling; n; n = n.previousSibling) {
        if (n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() === '') continue;
        atStart = false;
        break;
      }
      pill.toggleAttribute('data-start', atStart);
    });
  };

  // 记住最近光标位置：编辑器失焦后「+」菜单仍能原位插入
  const saveSelection = () => {
    const editor = editorRef.current;
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editor && editor.contains(sel.anchorNode)) {
      savedRange.current = sel.getRangeAt(0).cloneRange();
    }
  };

  const closeSlash = () => {
    setSlashOpen(false);
    setSlashQuery('');
    setSlashIndex(0);
    setSlashKeyboard(false);
    slashQueryRef.current = '';
    slashTokenRef.current = null;
    ignoreHoverRef.current = false;
  };

  // 构造技能药丸节点（contenteditable=false，整体作为一个单元删除）
  const buildPill = (id: string) => {
    const name = skillName(id);
    const el = document.createElement('span');
    el.className = 'chat-composer-skill-pill';
    el.setAttribute('contenteditable', 'false');
    el.dataset.skill = id;
    el.innerHTML =
      '<span class="chat-composer-skill-pill-label">/' + escapeHtml(name) + '</span>' +
      '<button type="button" class="chat-composer-skill-pill-x" data-remove="1" aria-label="' +
      escapeHtml(copy.chat.composer.removeItemAria(name)) +
      '"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>';
    return el;
  };

  // 用药丸 + 尾随空格替换 range，光标停在药丸之后
  const insertPillOverRange = (range: Range, id: string) => {
    const editor = editorRef.current;
    if (!editor) return;
    range.deleteContents();
    const pill = buildPill(id);
    range.insertNode(pill);
    const space = document.createTextNode('\u00A0');
    pill.after(space);
    const after = document.createRange();
    after.setStartAfter(space);
    after.collapse(true);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(after);
    editor.focus();
    savedRange.current = after.cloneRange();
    syncFromEditor();
  };

  // 从「+」菜单插入：用当前/最近光标，否则追加到末尾
  const addSkillFromMenu = (id: string) => {
    const editor = editorRef.current;
    if (!editor) return;
    const sel = window.getSelection();
    let range: Range | null = null;
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      range = sel.getRangeAt(0).cloneRange();
    } else if (savedRange.current && editor.contains(savedRange.current.startContainer)) {
      range = savedRange.current.cloneRange();
    }
    if (!range) {
      range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    insertPillOverRange(range, id);
    setMenuOpen(false);
  };

  // 从「/」命令插入：吞掉已输入的「/query」再落药丸
  const applySlash = (id: string) => {
    const editor = editorRef.current;
    if (!editor) {
      closeSlash();
      return;
    }
    let range: Range | null = null;
    const token = slashTokenRef.current;
    if (
      token &&
      token.node.isConnected &&
      editor.contains(token.node) &&
      token.end <= (token.node.textContent?.length ?? 0)
    ) {
      range = document.createRange();
      range.setStart(token.node, token.start);
      range.setEnd(token.node, token.end);
    } else {
      const sel = window.getSelection();
      if (sel && sel.rangeCount) {
        const caret = sel.getRangeAt(0);
        range = caret.cloneRange();
        const node = caret.startContainer;
        if (node.nodeType === Node.TEXT_NODE && editor.contains(node)) {
          const before = (node.textContent ?? '').slice(0, caret.startOffset);
          const m = before.match(/\/([^\s/]*)$/);
          if (m) {
            range = document.createRange();
            range.setStart(node, caret.startOffset - m[0].length);
            range.setEnd(node, caret.startOffset);
          }
        }
      }
    }
    if (!range) {
      closeSlash();
      return;
    }
    insertPillOverRange(range, id);
    closeSlash();
  };
  applySlashRef.current = applySlash;

  // 光标紧跟在「/」token 之后时打开面板
  const detectSlash = () => {
    const editor = editorRef.current;
    const sel = window.getSelection();
    if (!editor || !sel || !sel.rangeCount || !sel.isCollapsed) return closeSlash();
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (node.nodeType !== Node.TEXT_NODE || !editor.contains(node)) return closeSlash();
    const before = (node.textContent ?? '').slice(0, range.startOffset);
    const m = before.match(/(?:^|\s)\/([^\s/]*)$/);
    if (!m) return closeSlash();
    const q = m[1];
    const slashStart = before.length - m[1].length - 1;
    slashTokenRef.current = {
      node: node as Text,
      start: slashStart,
      end: range.startOffset,
    };
    if (q !== slashQueryRef.current) {
      slashQueryRef.current = q;
      setSlashIndex(0);
    }
    setSlashQuery(q);
    setSlashOpen(true);
  };

  const onEditorInput = () => {
    syncFromEditor();
    if (phase === 'enhanced') setPhase('idle');
    detectSlash();
  };

  const moveSlash = (delta: number) => {
    const results = slashResultsRef.current;
    if (!results.length) return;
    ignoreHoverRef.current = true;
    setSlashKeyboard(true);
    setSlashIndex((i) => (i + delta + results.length * 10) % results.length);
  };

  const handleSlashKey = (e: {
    key: string;
    preventDefault: () => void;
    stopPropagation?: () => void;
  }) => {
    const results = slashResultsRef.current;
    if (!slashOpenRef.current || !results.length) return false;
    if (
      e.key !== 'ArrowDown' &&
      e.key !== 'ArrowUp' &&
      e.key !== 'Enter' &&
      e.key !== 'Tab' &&
      e.key !== 'Escape'
    ) {
      return false;
    }
    e.preventDefault();
    // capture 阶段 window 监听先消费；阻止继续传播，避免 React 的编辑区 keydown 把 Enter 当发送
    e.stopPropagation?.();
    if (slashKeyLock.current) return true;
    slashKeyLock.current = true;
    queueMicrotask(() => {
      slashKeyLock.current = false;
    });
    if (e.key === 'ArrowDown') {
      moveSlash(1);
      return true;
    }
    if (e.key === 'ArrowUp') {
      moveSlash(-1);
      return true;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      applySlashRef.current((results[slashIndexRef.current] ?? results[0]).id);
      return true;
    }
    if (e.key === 'Escape') {
      closeSlash();
      return true;
    }
    return false;
  };

  const onEditorKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (handleSlashKey(e)) return;
    // Enter 发送；Shift+Enter 换行；isComposing 守卫中文 IME 组合输入误发（设计稿无，项目保留）
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  useEffect(() => {
    if (!slashOpen) return;
    const onKey = (e: KeyboardEvent) => {
      handleSlashKey(e);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slashOpen]);

  useEffect(() => {
    if (!slashOpen || !slashResults.length) return;
    if (slashIndex >= slashResults.length) setSlashIndex(0);
  }, [slashOpen, slashResults.length, slashIndex]);

  const onEditorClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    const remove = (e.target as HTMLElement).closest('[data-remove]');
    if (remove) {
      e.preventDefault();
      const pill = remove.closest<HTMLElement>('[data-skill]');
      if (pill) {
        // 紧跟药丸的分隔空格一并清除，避免残留空格累积把下一个药丸顶偏
        const sep = pill.nextSibling;
        // 药丸占位（宽度 + margin + padding）与淡出同步收拢，后续文本平滑滑入而不是跳变
        const w = pill.getBoundingClientRect().width;
        pill.style.maxWidth = `${w}px`;
        pill.style.overflow = 'hidden';
        pill.style.whiteSpace = 'nowrap';
        void pill.offsetWidth;
        pill.style.transition =
          'max-width 180ms cubic-bezier(0.22,1,0.36,1), margin 180ms cubic-bezier(0.22,1,0.36,1), padding 180ms cubic-bezier(0.22,1,0.36,1)';
        // 以来时同样的柔和方式离开，然后摘节点
        pill.setAttribute('data-exit', '');
        pill.style.maxWidth = '0px';
        pill.style.marginLeft = '0px';
        pill.style.marginRight = '0px';
        pill.style.paddingLeft = '0px';
        pill.style.paddingRight = '0px';
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          if (sep && sep.nodeType === Node.TEXT_NODE && sep.textContent?.startsWith('\u00A0')) {
            const rest = sep.textContent.slice(1);
            if (rest) sep.textContent = rest;
            else sep.parentNode?.removeChild(sep);
          }
          pill.remove();
          syncFromEditor();
          editorRef.current?.focus();
        };
        pill.addEventListener('animationend', finish, { once: true });
        // jsdom 不触发 CSS 动画事件：setTimeout 即降级路径，必须保留
        setTimeout(finish, 220);
      }
      return;
    }
    saveSelection();
  };

  // 外部点击 / Escape 关闭「+」菜单
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: PointerEvent) => {
      if (!plusRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  // 菜单关闭时收起技能 / 检索范围 flyout
  useEffect(() => {
    if (!menuOpen) {
      setSkillsOpen(false);
      setScopeOpen(false);
    }
  }, [menuOpen]);

  // 增强/还原后编辑器恢复可编辑：把暂存 HTML 写回（优化结果，或带药丸的原文），
  // 并让 frame 从旧高度动画到新高度（FLIP），输入框不跳变
  useLayoutEffect(() => {
    if (enhancing || pendingHTML.current === null) return;
    const editor = editorRef.current;
    if (!editor) return;
    editor.innerHTML = pendingHTML.current;
    pendingHTML.current = null;
    syncFromEditor();
    requestAnimationFrame(focusEnd);

    const frame = frameRef.current;
    const from = flipFrom.current;
    flipFrom.current = null;
    if (!frame || from === null) return;
    const to = frame.offsetHeight;
    const reduce =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce || from === to) return;
    frame.style.height = from + 'px';
    frame.style.overflow = 'hidden';
    void frame.offsetHeight; // 强制 reflow，让起始高度先提交
    frame.style.transition = 'height 200ms cubic-bezier(0.22, 1, 0.36, 1)';
    frame.style.height = to + 'px';
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      frame.style.transition = '';
      frame.style.height = '';
      frame.style.overflow = '';
      frame.removeEventListener('transitionend', finish);
    };
    frame.addEventListener('transitionend', finish);
    // jsdom 不触发 transitionend：setTimeout 兜底（降级路径），必须保留
    setTimeout(finish, 260);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, enhancing]);

  // 驱动增强药丸挂载/退场：有文本时进入；离开前先播放退场动画——
  // 交给 spinner（enhancing）时则立即互换
  useEffect(() => {
    if (showPill) {
      setPillMounted(true);
      setPillExiting(false);
      return;
    }
    if (!pillMounted) return;
    if (enhancing) {
      setPillMounted(false);
      setPillExiting(false);
      return;
    }
    setPillExiting(true);
    const t = setTimeout(() => {
      setPillMounted(false);
      setPillExiting(false);
    }, 200);
    return () => clearTimeout(t);
  }, [showPill, enhancing, pillMounted]);

  // 卸载时中止进行中的增强
  useEffect(() => () => abortRef.current?.abort(), []);

  const runEnhance = async () => {
    if (onEnhance === undefined || !hasText || enhancing || generating) return;
    preEnhanceHTML.current = editorRef.current?.innerHTML ?? '';
    setPhase('enhancing');
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const result = await onEnhance(value, ac.signal);
      if (ac.signal.aborted) return;
      pendingHTML.current = escapeHtml(result);
      flipFrom.current = frameRef.current?.offsetHeight ?? null;
      setPhase('enhanced');
    } catch {
      // 调用失败/中止时还原未动的原文
      if (ac.signal.aborted) return;
      pendingHTML.current = preEnhanceHTML.current;
      setPhase('idle');
    }
  };

  const revert = () => {
    abortRef.current?.abort();
    pendingHTML.current = preEnhanceHTML.current;
    flipFrom.current = frameRef.current?.offsetHeight ?? null;
    setPhase('idle');
  };

  // 发送语义（保留项目行为）：异步；onSend 返回 false 或抛错时保留输入；
  // 接受时清空编辑器——发送期间继续输入的稿件保留
  const submit = async () => {
    const trimmed = value.trim();
    if (trimmed === '' || generating || submitting || enhancing) return;
    setSubmitting(true);
    try {
      const accepted = await onSend(trimmed);
      const editor = editorRef.current;
      if (accepted !== false && editor !== null && (editor.textContent ?? '').trim() === trimmed) {
        editor.innerHTML = '';
        setValue('');
        setPhase('idle');
        setAttachments([]);
        setExitingAtt([]);
        closeSlash();
        requestAnimationFrame(() => editorRef.current?.focus());
      }
    } catch {
      // 发送失败时保留输入，允许用户重试。
    } finally {
      setSubmitting(false);
    }
  };

  // 与技能药丸同样的柔和淡出/缩放退场，然后摘除 chip
  const removeAttachment = (id: number) => {
    setExitingAtt((e) => (e.includes(id) ? e : [...e, id]));
    // jsdom 不触发 CSS 动画事件：200ms 定时即移除路径，必须保留
    window.setTimeout(() => {
      setAttachments((a) => a.filter((x) => x.id !== id));
      setExitingAtt((e) => e.filter((x) => x !== id));
    }, 200);
  };

  const openPicker = (kind: Attachment['kind']) => {
    const input = fileRef.current;
    if (!input) return;
    input.accept = kind === 'image' ? 'image/*' : '';
    input.value = '';
    input.dataset.kind = kind;
    input.click();
    setMenuOpen(false);
  };

  return (
    <div>
      <div className="chat-composer">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            if (!files.length) return;
            const fallback = (e.target.dataset.kind as Attachment['kind']) ?? 'file';
            setAttachments((a) => [
              ...a,
              ...files.map((f) => ({
                id: nextId.current++,
                name: f.name,
                kind: f.type.startsWith('image/') ? ('image' as const) : fallback,
              })),
            ]);
            e.target.value = '';
            requestAnimationFrame(() => editorRef.current?.focus());
          }}
        />

        <div ref={frameRef} className="chat-composer-frame" data-enhancing={enhancing || undefined}>
          {attachments.length > 0 && (
            <div className="chat-composer-chips">
              {attachments.map((att) => (
                <span
                  key={att.id}
                  className="chat-composer-chip"
                  data-exit={exitingAtt.includes(att.id) || undefined}
                >
                  <span className="chat-composer-chip-icon">
                    {att.kind === 'image' ? <ImageIcon size={13} /> : <Paperclip size={13} />}
                  </span>
                  <span className="chat-composer-chip-name">{att.name}</span>
                  <button
                    type="button"
                    className="chat-composer-chip-remove"
                    aria-label={copy.chat.composer.removeItemAria(att.name)}
                    onClick={() => removeAttachment(att.id)}
                  >
                    <X size={11} />
                  </button>
                </span>
              ))}
            </div>
          )}

          <div className="chat-composer-editor-wrap">
            {enhancing ? (
              <div className="chat-composer-enhancing-text" aria-live="polite">
                {value}
              </div>
            ) : (
              <div
                ref={editorRef}
                className="chat-composer-field"
                contentEditable
                suppressContentEditableWarning
                role="textbox"
                aria-multiline="true"
                aria-label={copy.chat.composer.inputPlaceholder}
                data-empty={!hasText || undefined}
                data-placeholder={copy.chat.composer.inputPlaceholder}
                onInput={onEditorInput}
                onKeyDown={onEditorKeyDown}
                onKeyUp={saveSelection}
                onMouseUp={saveSelection}
                onBlur={saveSelection}
                onClick={onEditorClick}
              />
            )}

            {slashOpen && !enhancing && (
              <div
                className="chat-composer-slash-menu"
                role="listbox"
                aria-label={copy.chat.composer.skillsLabel}
                data-keyboard={slashKeyboard || undefined}
                onMouseMove={() => {
                  ignoreHoverRef.current = false;
                  if (slashKeyboard) setSlashKeyboard(false);
                }}
              >
                <div className="chat-composer-slash-label">{copy.chat.composer.skillsLabel}</div>
                {slashResults.length ? (
                  slashResults.map((sk, i) => (
                    <button
                      key={sk.id}
                      type="button"
                      role="option"
                      aria-selected={i === slashIndex}
                      className={[
                        'chat-composer-menu-item',
                        i === slashIndex && 'chat-composer-menu-item-active',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                      onMouseDown={(e) => e.preventDefault()}
                      onMouseEnter={() => {
                        if (ignoreHoverRef.current) return;
                        setSlashIndex(i);
                      }}
                      onClick={() => applySlash(sk.id)}
                    >
                      <span className="chat-composer-menu-name">{sk.name}</span>
                    </button>
                  ))
                ) : (
                  <div className="chat-composer-slash-empty">
                    {copy.chat.composer.noMatchingSkills}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="chat-composer-row">
            <div className="chat-composer-plus-wrap" ref={plusRef}>
              <button
                type="button"
                className="chat-composer-icon-btn chat-composer-plus"
                data-open={menuOpen || undefined}
                aria-label={copy.chat.composer.addMenuAria}
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
              >
                <span className="chat-composer-plus-icon">
                  <Plus size={16} />
                </span>
              </button>

              {menuOpen && (
                <div className="chat-composer-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    className="chat-composer-menu-item"
                    onClick={() => openPicker('image')}
                  >
                    <span className="chat-composer-menu-icon">
                      <ImageIcon size={16} />
                    </span>
                    <span className="chat-composer-menu-name">{copy.chat.composer.addPhotos}</span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className="chat-composer-menu-item"
                    onClick={() => openPicker('file')}
                  >
                    <span className="chat-composer-menu-icon">
                      <Paperclip size={16} />
                    </span>
                    <span className="chat-composer-menu-name">{copy.chat.composer.attachFiles}</span>
                  </button>
                  <div className="chat-composer-menu-divider" />
                  <div
                    className="chat-composer-menu-sub"
                    onMouseEnter={() => setSkillsOpen(true)}
                    onMouseLeave={() => setSkillsOpen(false)}
                  >
                    <button
                      type="button"
                      role="menuitem"
                      className="chat-composer-menu-item"
                      aria-haspopup="menu"
                      aria-expanded={skillsOpen}
                      onClick={() => setSkillsOpen(true)}
                    >
                      <span className="chat-composer-menu-icon">
                        <BookOpen size={16} />
                      </span>
                      <span className="chat-composer-menu-name">
                        {copy.chat.composer.skillsLabel}
                      </span>
                      <span className="chat-composer-menu-chevron">
                        <ChevronRight size={16} />
                      </span>
                    </button>
                    {skillsOpen && (
                      <div className="chat-composer-menu-flyout" role="menu">
                        {SKILLS.map((sk) => (
                          <button
                            key={sk.id}
                            type="button"
                            role="menuitem"
                            className="chat-composer-menu-item"
                            onClick={() => addSkillFromMenu(sk.id)}
                          >
                            <span className="chat-composer-menu-name">{sk.name}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {/* 检索范围：点击行右侧展开 flyout（内含搜索/多选，不能用 hover 生命周期） */}
                  <div className="chat-composer-menu-sub">
                    <button
                      type="button"
                      role="menuitem"
                      className="chat-composer-menu-item"
                      aria-label={copy.chat.composer.scopeAria}
                      aria-expanded={scopeOpen}
                      onClick={() => setScopeOpen((open) => !open)}
                    >
                      <span className="chat-composer-menu-icon">
                        <Database size={16} />
                      </span>
                      <span className="chat-composer-menu-name">
                        {copy.chat.composer.scopeLabel}
                      </span>
                      {!scopeIsDefault && (
                        <span aria-hidden="true" className="chat-composer-scope-dot" />
                      )}
                      <span className="chat-composer-scope-summary">{scopeText}</span>
                      <span className="chat-composer-menu-chevron">
                        <ChevronRight size={16} />
                      </span>
                    </button>
                    {scopeOpen && (
                      <div className="chat-composer-menu-flyout chat-composer-scope-flyout">
                        <ScopeSelector
                          spaces={spaces}
                          onFetchDocuments={onFetchDocuments}
                          selection={selection}
                          onSelectionChange={onSelectionChange}
                        />
                      </div>
                    )}
                  </div>
                  <div className="chat-composer-menu-divider" />
                  {/* 努力档位（设计稿 Model 区块的对应物）：菜单底部区块，切换不收起菜单 */}
                  <div className="chat-composer-menu-label">{copy.chat.composer.effortAria}</div>
                  <div className="chat-composer-menu-effort">
                    <SegmentedControl
                      options={EFFORT_OPTIONS}
                      value={effortLevel}
                      onChange={(value) => onEffortChange(value as EffortLevel)}
                      ariaLabel={copy.chat.composer.effortAria}
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="chat-composer-right">
              {enhancing ? (
                <span
                  className="chat-composer-icon-btn chat-composer-spinner-btn"
                  aria-label={copy.chat.composer.enhancingAria}
                >
                  <LoaderCircle size={16} className="chat-composer-spinner" />
                </span>
              ) : (
                pillMounted && (
                  <button
                    type="button"
                    className={['chat-composer-pill', pillExiting && 'chat-composer-pill-exit']
                      .filter(Boolean)
                      .join(' ')}
                    onClick={phase === 'enhanced' ? revert : () => void runEnhance()}
                  >
                    {phase === 'enhanced'
                      ? copy.chat.composer.revertEnhance
                      : copy.chat.composer.enhancePrompt}
                  </button>
                )
              )}
              {generating ? (
                <button
                  type="button"
                  className={[
                    'chat-composer-icon-btn',
                    'chat-composer-send',
                    canStop && !stopping && 'chat-composer-send-active',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  aria-label={
                    stopping ? copy.chat.composer.stoppingAria : copy.chat.composer.stopAria
                  }
                  disabled={!canStop || stopping}
                  onClick={onStop}
                >
                  <Square aria-hidden="true" size={16} className="fill-current" />
                </button>
              ) : (
                <button
                  type="button"
                  className={[
                    'chat-composer-icon-btn',
                    'chat-composer-send',
                    sendActive && 'chat-composer-send-active',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  aria-label={copy.chat.composer.sendAria}
                  disabled={!sendActive}
                  onClick={() => void submit()}
                >
                  <ArrowUp aria-hidden="true" size={16} />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
