import { describe, expect, it } from 'vitest';
import { formatDrawerLocation, parseDrawerLocation } from './drawer-params';

describe('drawer-params（URL 表达抽屉开关与无限下钻层级）', () => {
  it('无参数表示抽屉关闭', () => {
    expect(parseDrawerLocation('')).toEqual({ drawer: null, drill: [] });
    expect(parseDrawerLocation('?foo=1')).toEqual({ drawer: null, drill: [] });
  });

  it('drawer 参数表达抽屉开关', () => {
    expect(parseDrawerLocation('?drawer=settings')).toEqual({ drawer: 'settings', drill: [] });
  });

  it('drill 有序可重复参数表达无限下钻层级', () => {
    const parsed = parseDrawerLocation('?drawer=settings&drill=personal&drill=profile&drill=security');
    expect(parsed.drawer).toBe('settings');
    expect(parsed.drill).toEqual(['personal', 'profile', 'security']);
  });

  it('parse 与 format 互逆', () => {
    const location = { drawer: 'settings', drill: ['personal', 'profile'] } as const;
    const search = formatDrawerLocation(location);
    expect(search).toBe('?drawer=settings&drill=personal&drill=profile');
    expect(parseDrawerLocation(search)).toEqual(location);
  });

  it('抽屉关闭时忽略下钻层级', () => {
    expect(formatDrawerLocation({ drawer: null, drill: ['personal'] })).toBe('');
  });

  it('空层级被过滤；特殊字符正确编码', () => {
    expect(parseDrawerLocation('?drawer=settings&drill=')).toEqual({ drawer: 'settings', drill: [] });
    const search = formatDrawerLocation({ drawer: 'admin', drill: ['section/sub'] });
    expect(parseDrawerLocation(search)).toEqual({ drawer: 'admin', drill: ['section/sub'] });
  });
});
