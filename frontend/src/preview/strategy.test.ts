import { describe, expect, it } from 'vitest';
import { previewStrategy } from './strategy';

/*
 * 策略矩阵（fe-doc-preview）：media_kind + has_text_layer + tree_indexed → 渲染/定位行为。
 * 不按扩展名；未知载体兜底只读文本流。
 */

describe('previewStrategy 行为矩阵', () => {
  it('有文本层 PDF：跳页 + snippet 文本层匹配高亮', () => {
    expect(previewStrategy({ media_kind: 'pdf', has_text_layer: true, tree_indexed: false })).toEqual({
      renderer: 'pdf',
      locate: 'page-and-snippet',
      textKind: null,
    });
  });

  it('扫描件 PDF（无文本层）：只跳页', () => {
    expect(previewStrategy({ media_kind: 'pdf', has_text_layer: false, tree_indexed: false })).toEqual({
      renderer: 'pdf',
      locate: 'page-only',
      textKind: null,
    });
  });

  it('建树 Word：section_path 锚点定位；basic Word 仅打开', () => {
    expect(previewStrategy({ media_kind: 'word', has_text_layer: false, tree_indexed: true })).toEqual({
      renderer: 'word',
      locate: 'section',
      textKind: null,
    });
    expect(previewStrategy({ media_kind: 'word', has_text_layer: false, tree_indexed: false })).toEqual({
      renderer: 'word',
      locate: 'none',
      textKind: null,
    });
  });

  it('md / txt：文本流 snippet 匹配', () => {
    for (const mediaKind of ['md', 'txt'] as const) {
      expect(previewStrategy({ media_kind: mediaKind, has_text_layer: false, tree_indexed: false })).toEqual({
        renderer: 'text',
        locate: 'snippet',
        textKind: 'plain',
      });
    }
  });

  it('code / data：文本流 + 各自形态', () => {
    expect(previewStrategy({ media_kind: 'code', has_text_layer: false, tree_indexed: false })).toEqual({
      renderer: 'text',
      locate: 'snippet',
      textKind: 'code',
    });
    expect(previewStrategy({ media_kind: 'data', has_text_layer: false, tree_indexed: false })).toEqual({
      renderer: 'text',
      locate: 'snippet',
      textKind: 'data',
    });
  });

  it('Excel / CSV：只读表格 + a1 定位', () => {
    for (const mediaKind of ['excel', 'csv'] as const) {
      expect(previewStrategy({ media_kind: mediaKind, has_text_layer: false, tree_indexed: false })).toEqual({
        renderer: 'sheet',
        locate: 'a1',
        textKind: null,
      });
    }
  });

  it('图片：原图居中、无锚点', () => {
    expect(previewStrategy({ media_kind: 'image', has_text_layer: false, tree_indexed: false })).toEqual({
      renderer: 'image',
      locate: 'none',
      textKind: null,
    });
  });

  it('未知载体：只读文本流兜底，不做位置推断', () => {
    expect(previewStrategy({ media_kind: 'hologram', has_text_layer: true, tree_indexed: true })).toEqual({
      renderer: 'text',
      locate: 'none',
      textKind: 'plain',
    });
  });
});
