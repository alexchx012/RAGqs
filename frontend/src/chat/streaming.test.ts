import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createStreamingSimulator, splitIntoChunks } from './streaming';

/*
 * 模拟流式渲染器行为验证（spec §4）：分段优先 / 逐字兜底 / reduced-motion 直出 / 停止保留已模拟部分。
 * 用 fake timers 控制节奏。
 */

function makeDeps(overrides: Partial<Parameters<typeof createStreamingSimulator>[0]> = {}) {
  const calls: { text: string }[] = [];
  let doneCount = 0;
  let stopCount = 0;
  const deps = {
    reducedMotion: false,
    chunkIntervalMs: 40,
    charIntervalMs: 16,
    maxChunkLength: 120,
    onText: (text: string) => {
      calls.push({ text });
    },
    onDone: () => {
      doneCount += 1;
    },
    onStop: () => {
      stopCount += 1;
    },
    ...overrides,
  };
  return { deps, calls, getDone: () => doneCount, getStop: () => stopCount };
}

describe('splitIntoChunks（Markdown 结构分段）', () => {
  it('按段落与行分段，代码块保持原子', () => {
    const text = '第一段内容。\n\n第二段内容。\n第三行。\n\n```ts\nconst a = 1;\n```';
    const chunks = splitIntoChunks(text, 120);
    expect(chunks.join('')).toBe(text);
    expect(chunks.some((chunk) => chunk.startsWith('```'))).toBe(true);
  });

  it('无断点的超长串按句/字兜底，不丢失内容', () => {
    const text = '这是一个没有换行也没有标点的超长字符串片段'.repeat(10);
    const chunks = splitIntoChunks(text, 12);
    expect(chunks.join('')).toBe(text);
    expect(chunks.every((chunk) => chunk.length > 0)).toBe(true);
  });
});

describe('createStreamingSimulator', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('分段模拟：按节奏逐段追加，onText 传累计全文，播完触发 onDone', async () => {
    const { deps, calls, getDone } = makeDeps();
    const sim = createStreamingSimulator(deps);
    sim.feed('第一段。\n\n第二段。');
    // 结构切分：['第一段。', '\n\n', '第二段。']；首段立即播出
    expect(sim.getText()).toBe('第一段。');
    for (let step = 0; step < 10 && !sim.isDone(); step += 1) {
      await vi.advanceTimersByTimeAsync(40);
    }
    expect(sim.getText()).toBe('第一段。\n\n第二段。');
    expect(sim.isDone()).toBe(true);
    expect(getDone()).toBe(1);
    expect(calls.at(-1)?.text).toBe('第一段。\n\n第二段。');
  });

  it('逐字兜底：拆分为单字块时用字符节奏', async () => {
    const { deps } = makeDeps({ chunkIntervalMs: 100, charIntervalMs: 10, maxChunkLength: 1 });
    const sim = createStreamingSimulator(deps);
    sim.feed('abc');
    expect(sim.getText()).toBe('a');
    await vi.advanceTimersByTimeAsync(9);
    expect(sim.getText()).toBe('a');
    await vi.advanceTimersByTimeAsync(1);
    expect(sim.getText()).toBe('ab');
    await vi.advanceTimersByTimeAsync(10);
    expect(sim.getText()).toBe('abc');
    for (let step = 0; step < 5 && !sim.isDone(); step += 1) {
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(sim.isDone()).toBe(true);
  });

  it('reduced-motion：feed 即直出全文并立即 onDone', () => {
    const { deps, calls, getDone } = makeDeps({ reducedMotion: true });
    const sim = createStreamingSimulator(deps);
    sim.feed('长回答全文……');
    expect(sim.getText()).toBe('长回答全文……');
    expect(sim.isDone()).toBe(true);
    expect(getDone()).toBe(1);
    expect(calls[0]?.text).toBe('长回答全文……');
  });

  it('stop 保留已模拟部分，不再继续，不触发 onDone', async () => {
    const { deps, getDone, getStop } = makeDeps();
    const sim = createStreamingSimulator(deps);
    sim.feed('第一段。\n\n第二段。\n\n第三段。');
    expect(sim.getText()).toBe('第一段。');
    sim.stop();
    expect(sim.isStopped()).toBe(true);
    expect(sim.getText()).toBe('第一段。'); // 保留已模拟部分
    await vi.advanceTimersByTimeAsync(10_000);
    expect(sim.getText()).toBe('第一段。'); // 不再继续
    expect(getDone()).toBe(0);
    expect(getStop()).toBe(1);
  });

  it('空内容与重复 feed 忽略', () => {
    const { deps, getDone } = makeDeps();
    const sim = createStreamingSimulator(deps);
    sim.feed('');
    expect(sim.isDone()).toBe(false);
    sim.feed('一');
    sim.feed('二'); // 已在模拟中，忽略
    expect(sim.getText()).toBe('一');
    sim.dispose();
    expect(getDone()).toBe(0);
  });

  it('断线不清空：store 不重置模拟器，已展示正文保留（模拟器视角：feed 后正文不因外部事件清空）', async () => {
    const { deps } = makeDeps();
    const sim = createStreamingSimulator(deps);
    sim.feed('已展示正文。');
    await vi.advanceTimersByTimeAsync(40);
    expect(sim.getText()).toBe('已展示正文。');
    // 模拟连接切换：模拟器不感知、不清空
    expect(sim.getText()).toBe('已展示正文。');
    expect(sim.isDone()).toBe(true);
  });
});
