/*
 * 模拟流式渲染器（fe-chat-home 规格 §4；纯 TS，React 无关）。
 * answer 事件一次性下发完整稳定答案，前端逐段 / 逐字模拟渲染：
 * - 分段优先（按 Markdown 结构分段：代码块 / 段落 / 行 / 句），逐字兜底（无断点的长串）；
 * - 节奏可配置；prefers-reduced-motion 直出全文；
 * - 连接切换 / 断线不清空已展示正文（store 只在 feed 时驱动，不因断线重置）；
 * - stop() 保留已模拟部分并停止后续模拟。
 */

export interface StreamingSimulatorDeps {
  /** prefers-reduced-motion：true 时 feed 即直出全文。 */
  readonly reducedMotion: boolean;
  /** 每段间隔（默认 40ms）。 */
  readonly chunkIntervalMs?: number;
  /** 单字符兜底间隔（默认 16ms）。 */
  readonly charIntervalMs?: number;
  /** 单段最大长度（超过继续按句/字拆分；默认 120）。 */
  readonly maxChunkLength?: number;
  readonly onText: (text: string) => void;
  readonly onDone: () => void;
  /** 停止（保留已模拟部分）时回调。 */
  readonly onStop: () => void;
}

export interface StreamingSimulator {
  /** 当前已展示全文。 */
  getText(): string;
  /** 是否已完成（含 reduced-motion 直出）。 */
  isDone(): boolean;
  /** 是否已停止。 */
  isStopped(): boolean;
  /** 开始模拟指定正文；仅接受一次（重复调用忽略）。 */
  feed(text: string): void;
  /** 停止模拟：保留已模拟部分，不触发 onDone。 */
  stop(): void;
  dispose(): void;
}

const DEFAULT_CHUNK_INTERVAL_MS = 40;
const DEFAULT_CHAR_INTERVAL_MS = 16;
const DEFAULT_MAX_CHUNK_LENGTH = 120;

/** 按 Markdown 结构切分模拟段落：代码块原子 → 段落 → 行 → 句 → 字。 */
export function splitIntoChunks(text: string, maxChunkLength: number): string[] {
  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    // 先剥离前导空白（换行/空格）为独立块：避免代码块前的 \n\n 破坏原子性
    const leading = /^[\s]+/.exec(remaining);
    if (leading !== null) {
      chunks.push(leading[0]);
      remaining = remaining.slice(leading[0].length);
      continue;
    }
    const codeBlock = matchCodeBlock(remaining);
    if (codeBlock !== null && codeBlock.index === 0) {
      chunks.push(codeBlock.text);
      remaining = remaining.slice(codeBlock.text.length);
      continue;
    }
    // 从当前位置找最近的断点（段落 / 行 / 句）
    const breakPoint = findBreakPoint(remaining, maxChunkLength);
    let piece: string;
    if (breakPoint !== null && breakPoint <= maxChunkLength) {
      piece = remaining.slice(0, breakPoint);
      remaining = remaining.slice(breakPoint);
    } else {
      // 无可用断点或断点过远：先试按句拆分，仍超长则按字
      const sentence = findSentenceBreak(remaining, maxChunkLength);
      if (sentence !== null) {
        piece = remaining.slice(0, sentence);
        remaining = remaining.slice(sentence);
      } else {
        piece = remaining.slice(0, maxChunkLength);
        remaining = remaining.slice(maxChunkLength);
      }
    }
    if (piece.length > 0) {
      chunks.push(piece);
    }
  }
  return chunks;
}

function matchCodeBlock(text: string): { text: string; index: number } | null {
  const match = /^```[\s\S]*?(```|$)/.exec(text);
  return match === null ? null : { text: match[0], index: 0 };
}

/** 在 maxChunkLength 内找段落（\n\n）或行（\n）断点；优先段落。 */
function findBreakPoint(text: string, maxChunkLength: number): number | null {
  const paragraph = text.indexOf('\n\n');
  if (paragraph > 0 && paragraph <= maxChunkLength) return paragraph;
  const line = text.indexOf('\n');
  if (line > 0 && line <= maxChunkLength) return line;
  return null;
}

/** 在 maxChunkLength 内找句末断点（中英文句号/感叹号/问号/分号）。 */
function findSentenceBreak(text: string, maxChunkLength: number): number | null {
  const limit = Math.min(text.length, maxChunkLength);
  for (let index = limit; index > 0; index -= 1) {
    const char = text[index - 1];
    // CJK 标点用 unicode 转义书写，避免源码内出现 CJK 字面量（copy-discipline 约束）
    if (
      char === '\u3002' || // 。
      char === '\uff01' || // ！
      char === '\uff1f' || // ？
      char === '\uff1b' || // ；
      char === '.' ||
      char === '!' ||
      char === '?' ||
      char === ';'
    ) {
      return index;
    }
  }
  return null;
}

export function createStreamingSimulator(deps: StreamingSimulatorDeps): StreamingSimulator {
  const chunkIntervalMs = deps.chunkIntervalMs ?? DEFAULT_CHUNK_INTERVAL_MS;
  const charIntervalMs = deps.charIntervalMs ?? DEFAULT_CHAR_INTERVAL_MS;
  const maxChunkLength = deps.maxChunkLength ?? DEFAULT_MAX_CHUNK_LENGTH;

  let text = '';
  let done = false;
  let stopped = false;
  let started = false;
  let chunks: string[] = [];
  let cursor = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function clearTimer(): void {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
  }

  function emitNext(): void {
    if (stopped || done) return;
    if (cursor >= chunks.length) {
      done = true;
      deps.onDone();
      return;
    }
    const chunk = chunks[cursor];
    cursor += 1;
    text += chunk;
    deps.onText(text);
    const delay = chunk.length === 1 ? charIntervalMs : chunkIntervalMs;
    timer = setTimeout(emitNext, delay);
  }

  return {
    getText() {
      return text;
    },
    isDone() {
      return done;
    },
    isStopped() {
      return stopped;
    },
    feed(input) {
      if (done || stopped || started || input.length === 0) return;
      started = true;
      if (deps.reducedMotion) {
        text = input;
        done = true;
        deps.onText(text);
        deps.onDone();
        return;
      }
      chunks = splitIntoChunks(input, maxChunkLength);
      cursor = 0;
      emitNext();
    },
    stop() {
      if (done || stopped) return;
      stopped = true;
      clearTimer();
      deps.onStop();
    },
    dispose() {
      stopped = true;
      clearTimer();
    },
  };
}
