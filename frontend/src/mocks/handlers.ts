/*
 * MSW handlers：把《前端接口需求.md》§1–§2、§5（本 change 范围）的 HTTP 边界接到契约 mock 核心。
 * 错误体固定为 { error: { code, message, details, request_id } }，details 始终为对象。
 */

import { http, HttpResponse } from 'msw';
import { CSRF_COOKIE_NAME } from '../auth/cookies';
import { MockHttpError, type MockAuthController } from './auth-contract';
import { clearAuthCookies, getMockCookie, REFRESH_COOKIE_NAME, setMockCookie } from './dev-cookies';
import type { MockNotificationsController } from './notifications-contract';

let requestSeq = 0;

function errorResponse(error: unknown) {
  const normalized =
    error instanceof MockHttpError ? error : new MockHttpError(500, 'internal_error');
  requestSeq += 1;
  return HttpResponse.json(
    {
      error: {
        code: normalized.code,
        message: normalized.code,
        details: normalized.details,
        request_id: `req_mock_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

/** 会话列表的设备摘要（共用基座 §5.4「设备/浏览器」）：UA 解析为「浏览器 · 系统」，解析不出回退通用名。 */
function deviceOf(request: Request): string {
  const ua = request.headers.get('User-Agent') ?? '';
  if (ua === '') {
    return '未知设备';
  }
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /OPR\//.test(ua)
      ? 'Opera'
      : /Chrome\//.test(ua)
        ? 'Chrome'
        : /Firefox\//.test(ua)
          ? 'Firefox'
          : /Safari\//.test(ua)
            ? 'Safari'
            : '浏览器';
  const os = /Windows/.test(ua)
    ? 'Windows'
    : /Mac OS X/.test(ua)
      ? 'macOS'
      : /Android/.test(ua)
        ? 'Android'
        : /iPhone|iPad/.test(ua)
          ? 'iOS'
          : /Linux/.test(ua)
            ? 'Linux'
            : '';
  return os === '' ? browser : `${browser} · ${os}`;
}

export function createAuthHandlers(controller: MockAuthController) {
  return [
    http.post('/v1/auth/login', async ({ request }) => {
      const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
      try {
        const result = controller.login(
          String(body['username'] ?? ''),
          String(body['password'] ?? ''),
          deviceOf(request),
        );
        // 成功响应由服务端设置 refresh Cookie 与前端可读的 CSRF Cookie（契约 §2.1）
        setMockCookie(REFRESH_COOKIE_NAME, result.refreshToken);
        setMockCookie(CSRF_COOKIE_NAME, result.csrfToken);
        return HttpResponse.json({ token: result.accessToken, user: result.user });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/auth/logout', ({ request }) => {
      // 只退出当前设备：撤销当前 access token 绑定的设备会话，幂等 204（契约 §2.2）
      controller.logout(request.headers.get('Authorization'));
      clearAuthCookies();
      return new HttpResponse(null, { status: 204 });
    }),

    http.post('/v1/auth/refresh', ({ request }) => {
      try {
        const result = controller.refresh(
          getMockCookie(REFRESH_COOKIE_NAME),
          request.headers.get('X-CSRF-Token'),
          getMockCookie(CSRF_COOKIE_NAME),
        );
        // 同步轮换 refresh Cookie（契约 §2.10）
        setMockCookie(REFRESH_COOKIE_NAME, result.refreshToken);
        return HttpResponse.json({ token: result.accessToken });
      } catch (error) {
        if (error instanceof MockHttpError && (error.status === 401 || error.status === 403)) {
          clearAuthCookies();
        }
        return errorResponse(error);
      }
    }),

    http.get('/v1/auth/me', ({ request }) => {
      try {
        return HttpResponse.json(controller.me(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/auth/sessions', ({ request }) => {
      try {
        return HttpResponse.json({ items: controller.listSessions(request.headers.get('Authorization')) });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/auth/sessions/:id', ({ request, params }) => {
      try {
        const { current } = controller.revokeSession(
          request.headers.get('Authorization'),
          String(params['id']),
        );
        // 目标为当前设备时同时清除当前浏览器的 refresh 与 CSRF Cookie（契约 §2.8）
        if (current) {
          clearAuthCookies();
        }
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/auth/sessions', ({ request }) => {
      try {
        controller.revokeAllSessions(request.headers.get('Authorization'));
        clearAuthCookies();
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}

function parseLimitParam(url: string): number | undefined {
  const raw = new URL(url).searchParams.get('limit');
  return raw === null ? undefined : Number(raw);
}

/*
 * 站内提醒 HTTP 边界（契约 §5）：鉴权在控制器内经构造注入的 validateAuth 完成，
 * 该回调在装配处（testing.ts / start.ts）用 authController.me 实现——无有效 Bearer
 * 时 me 抛 MockHttpError(401, 'invalid_token')；authController 形参显式表达这一装配依赖。
 */
export function createNotificationHandlers(
  controller: MockNotificationsController,
  authController: MockAuthController,
) {
  void authController;
  return [
    http.get('/v1/notifications', ({ request }) => {
      try {
        const items = controller.list(
          request.headers.get('Authorization'),
          parseLimitParam(request.url),
        );
        return HttpResponse.json({ items });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/notifications/unread-count', ({ request }) => {
      try {
        const count = controller.unreadCount(request.headers.get('Authorization'));
        return HttpResponse.json({ count });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/notifications/:id/read', ({ request, params }) => {
      try {
        controller.markRead(request.headers.get('Authorization'), String(params['id']));
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    // read-all 不接收请求体（契约 §5.3）：忽略 body，一律按水位推进处理
    http.post('/v1/notifications/read-all', ({ request }) => {
      try {
        controller.markAllRead(request.headers.get('Authorization'));
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/notifications/events/:eventId/ack', ({ request, params }) => {
      try {
        controller.ack(request.headers.get('Authorization'), String(params['eventId']));
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
