import { describe, expect, it } from 'vitest';
import { formatDrawerLocation, parseDrawerLocation } from './drawer-params';

describe('drawer-params（路径段式 URL 表达抽屉开关与无限下钻层级）', () => {
  it('非抽屉路径表示抽屉关闭', () => {
    expect(parseDrawerLocation('/')).toEqual({ open: false, segment: null, drill: [] });
    expect(parseDrawerLocation('/chat/abc')).toEqual({ open: false, segment: null, drill: [] });
  });

  it('根路径段表达抽屉开关与所在段', () => {
    expect(parseDrawerLocation('/settings')).toEqual({ open: true, segment: 'personal', drill: [] });
    expect(parseDrawerLocation('/settings/')).toEqual({ open: true, segment: 'personal', drill: [] });
    expect(parseDrawerLocation('/admin')).toEqual({ open: true, segment: 'admin', drill: [] });
  });

  it('路径段有序表达无限下钻层级', () => {
    const parsed = parseDrawerLocation('/settings/knowledge/uploads/deep/deeper');
    expect(parsed.open).toBe(true);
    expect(parsed.segment).toBe('personal');
    expect(parsed.drill).toEqual(['knowledge', 'uploads', 'deep', 'deeper']);
  });

  it('parse 与 format 互逆', () => {
    const location = { open: true, segment: 'admin', drill: ['spaces', 'public'] } as const;
    const path = formatDrawerLocation(location);
    expect(path).toBe('/admin/spaces/public');
    expect(parseDrawerLocation(path)).toEqual(location);
  });

  it('抽屉关闭时忽略下钻层级', () => {
    expect(formatDrawerLocation({ open: false, segment: null, drill: ['knowledge'] })).toBe('/');
  });

  it('顶层与深层格式稳定', () => {
    expect(formatDrawerLocation({ open: true, segment: 'personal', drill: [] })).toBe('/settings');
    expect(formatDrawerLocation({ open: true, segment: 'personal', drill: ['knowledge'] })).toBe(
      '/settings/knowledge',
    );
  });
});
