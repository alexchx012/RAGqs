import { describe, expect, it } from 'vitest';
import { CSRF_COOKIE_NAME } from '../auth/cookies';
import { resolveUrl } from '../api/client';
import { MockHttpError } from './auth-contract';
import { getMockCookie, REFRESH_COOKIE_NAME, setMockCookie } from './dev-cookies';
import { mockAuth, mockSettings } from './testing';

function bearerOf(username = 'zhangsan', password = 'password123'): string {
  const { accessToken } = mockAuth.login(username, password, 'vitest');
  return `Bearer ${accessToken}`;
}

function expectHttpError(fn: () => unknown, status: number, code: string): void {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(MockHttpError);
    const httpError = error as MockHttpError;
    expect(httpError.status).toBe(status);
    expect(httpError.code).toBe(code);
    return;
  }
  throw new Error(`expected MockHttpError ${status} ${code}`);
}

async function errorCode(response: Response): Promise<string> {
  const body = (await response.json()) as { error: { code: string } };
  return body.error.code;
}

async function profileRequest(token: string, body: unknown): Promise<Response> {
  return fetch(resolveUrl('/v1/users/me/profile'), {
    method: 'PATCH',
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function passwordRequest(token: string, body: unknown): Promise<Response> {
  return fetch(resolveUrl('/v1/users/me/password'), {
    method: 'PUT',
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function avatarRequest(token: string, file: File): Promise<Response> {
  const form = new FormData();
  form.set('file', file);
  return fetch(resolveUrl('/v1/users/me/avatar'), {
    method: 'POST',
    headers: { Authorization: token },
    body: form,
  });
}

async function preferencesRequest(token: string, body?: unknown): Promise<Response> {
  return fetch(resolveUrl('/v1/users/me/preferences'), {
    method: body === undefined ? 'GET' : 'PUT',
    headers: body === undefined
      ? { Authorization: token }
      : { Authorization: token, 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

async function quotaRequest(token: string): Promise<Response> {
  return fetch(resolveUrl('/v1/quota/me'), { headers: { Authorization: token } });
}

async function requestMoreQuota(
  token: string,
  body: unknown,
  idempotencyKey = 'idem-quota-1',
): Promise<Response> {
  return fetch(resolveUrl('/v1/quota-requests'), {
    method: 'POST',
    headers: {
      Authorization: token,
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify(body),
  });
}

/**
 * Build a raw multipart body so empty/zero-byte file parts stay empty.
 * jsdom FormData rewrites `new File([], ...)` into the literal bytes "undefined".
 */
function avatarMultipartRequest(
  token: string,
  options: { fileName: string; contentType: string; content: Uint8Array },
): Promise<Response> {
  const boundary = '----ragqsAvatarBoundary7MA4YWxk';
  const encoder = new TextEncoder();
  const header = encoder.encode(
    `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="file"; filename="${options.fileName}"\r\n` +
      `Content-Type: ${options.contentType}\r\n\r\n`,
  );
  const footer = encoder.encode(`\r\n--${boundary}--\r\n`);
  const body = new Uint8Array(header.length + options.content.length + footer.length);
  body.set(header, 0);
  body.set(options.content, header.length);
  body.set(footer, header.length + options.content.length);

  return fetch(resolveUrl('/v1/users/me/avatar'), {
    method: 'POST',
    headers: {
      Authorization: token,
      'Content-Type': `multipart/form-data; boundary=${boundary}`,
    },
    body,
  });
}

describe('settings contract mock', () => {
  it('updates only the authenticated profile display name and stores the returned value', async () => {
    const token = bearerOf();
    const response = await profileRequest(token, { display_name: '新名字' });

    expect(response.status).toBe(200);
    expect((await response.json()) as { display_name: string }).toMatchObject({
      display_name: '新名字',
    });
    expect(mockAuth.me(token).display_name).toBe('新名字');
  });

  it('rejects blank, overlong, and extra-field profile bodies with validation_error', async () => {
    const token = bearerOf();

    const blank = await profileRequest(token, { display_name: '   ' });
    expect(blank.status).toBe(422);
    expect(await errorCode(blank)).toBe('validation_error');

    const overlong = await profileRequest(token, { display_name: '名'.repeat(257) });
    expect(overlong.status).toBe(422);
    expect(await errorCode(overlong)).toBe('validation_error');

    const extra = await profileRequest(token, { display_name: '合法名', role: 'admin' });
    expect(extra.status).toBe(422);
    expect(await errorCode(extra)).toBe('validation_error');

    expect(mockAuth.me(token).display_name).toBe('zhangsan');
  });

  it('trims a valid profile display_name before writing and returning it', async () => {
    const token = bearerOf();
    const response = await profileRequest(token, { display_name: '  修剪后  ' });

    expect(response.status).toBe(200);
    expect((await response.json()) as { display_name: string }).toMatchObject({
      display_name: '修剪后',
    });
    expect(mockAuth.me(token).display_name).toBe('修剪后');
  });

  it('accepts one multipart avatar file and returns a stored avatar URL', async () => {
    const token = bearerOf();
    const response = await avatarRequest(
      token,
      new File(['avatar'], 'new-avatar.png', { type: 'image/png' }),
    );

    expect(response.status).toBe(200);
    const body = (await response.json()) as { avatar_url: string };
    expect(body.avatar_url).toBeTruthy();
    expect(mockAuth.me(token).avatar_url).toBe(body.avatar_url);
  });

  it('rejects empty, non-image, and oversized avatar uploads with validation_error', async () => {
    const token = bearerOf();

    const empty = await avatarMultipartRequest(token, {
      fileName: 'empty.png',
      contentType: 'image/png',
      content: new Uint8Array(0),
    });
    expect(empty.status).toBe(422);
    expect(await errorCode(empty)).toBe('validation_error');

    const nonImage = await avatarMultipartRequest(token, {
      fileName: 'notes.txt',
      contentType: 'text/plain',
      content: new TextEncoder().encode('not-an-image'),
    });
    expect(nonImage.status).toBe(422);
    expect(await errorCode(nonImage)).toBe('validation_error');

    // Raw multipart avoids jsdom FormData rewriting large/empty File parts.
    const oversized = await avatarMultipartRequest(token, {
      fileName: 'big.png',
      contentType: 'image/png',
      content: new Uint8Array(5 * 1024 * 1024 + 1),
    });
    expect(oversized.status).toBe(422);
    expect(await errorCode(oversized)).toBe('validation_error');

    expect(mockAuth.me(token).avatar_url).toBeNull();
  });

  it('returns the specified password error codes from the public endpoint', async () => {
    const token = bearerOf();

    const invalid = await passwordRequest(token, {
      old_password: 'password123',
      new_password: 'letters-only',
    });
    expect(invalid.status).toBe(400);
    expect(await errorCode(invalid)).toBe('invalid_password_rule');

    const wrongOldPassword = await passwordRequest(token, {
      old_password: 'not-the-current-password',
      new_password: 'newpassword1',
    });
    expect(wrongOldPassword.status).toBe(403);
    expect(await errorCode(wrongOldPassword)).toBe('wrong_old_password');
  });

  it('rejects invalid password body shapes with validation_error', async () => {
    const token = bearerOf();

    const missing = await passwordRequest(token, { old_password: 'password123' });
    expect(missing.status).toBe(422);
    expect(await errorCode(missing)).toBe('validation_error');

    const extra = await passwordRequest(token, {
      old_password: 'password123',
      new_password: 'newpassword1',
      confirm: 'newpassword1',
    });
    expect(extra.status).toBe(422);
    expect(await errorCode(extra)).toBe('validation_error');

    const emptyOld = await passwordRequest(token, {
      old_password: '',
      new_password: 'newpassword1',
    });
    expect(emptyOld.status).toBe(422);
    expect(await errorCode(emptyOld)).toBe('validation_error');
  });

  it('returns 204, clears auth cookies, and revokes every session on password success', async () => {
    const first = mockAuth.login('zhangsan', 'password123', 'first-device');
    const second = mockAuth.login('zhangsan', 'password123', 'second-device');
    const firstBearer = `Bearer ${first.accessToken}`;
    const secondBearer = `Bearer ${second.accessToken}`;

    setMockCookie(REFRESH_COOKIE_NAME, first.refreshToken);
    setMockCookie(CSRF_COOKIE_NAME, first.csrfToken);
    expect(getMockCookie(REFRESH_COOKIE_NAME)).toBe(first.refreshToken);
    expect(getMockCookie(CSRF_COOKIE_NAME)).toBe(first.csrfToken);

    const response = await passwordRequest(firstBearer, {
      old_password: 'password123',
      new_password: 'newpassword1',
    });

    expect(response.status).toBe(204);
    expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeNull();
    expect(getMockCookie(CSRF_COOKIE_NAME)).toBeNull();
    expectHttpError(() => mockAuth.me(firstBearer), 401, 'session_revoked');
    expectHttpError(() => mockAuth.me(secondBearer), 401, 'session_revoked');
    expect(() => mockAuth.login('zhangsan', 'newpassword1', 'new-device')).not.toThrow();
  });

  it('uses the controller password rule and successful password changes revoke every device session', () => {
    const first = mockAuth.login('zhangsan', 'password123', 'first-device');
    const second = mockAuth.login('zhangsan', 'password123', 'second-device');
    const firstBearer = `Bearer ${first.accessToken}`;
    const secondBearer = `Bearer ${second.accessToken}`;

    expectHttpError(
      () =>
        mockSettings.changePassword(firstBearer, {
          old_password: 'password123',
          new_password: 'letters-only',
        }),
      400,
      'invalid_password_rule',
    );

    expectHttpError(
      () =>
        mockSettings.changePassword(firstBearer, {
          old_password: 'not-the-current-password',
          new_password: 'newpassword1',
        }),
      403,
      'wrong_old_password',
    );

    mockSettings.changePassword(firstBearer, {
      old_password: 'password123',
      new_password: 'newpassword1',
    });

    expectHttpError(() => mockAuth.me(firstBearer), 401, 'session_revoked');
    expectHttpError(() => mockAuth.me(secondBearer), 401, 'session_revoked');
    expect(() => mockAuth.login('zhangsan', 'newpassword1', 'new-device')).not.toThrow();
  });

  it('reads and persists a complete preferences snapshot per authenticated user', async () => {
    const token = bearerOf('zhangsan');
    const initial = await preferencesRequest(token);
    expect(initial.status).toBe(200);
    expect(await initial.json()).toEqual({
      theme: 'system',
      chat_font_size: 'standard',
      ab_opt_out: false,
    });

    const next = { theme: 'dark', chat_font_size: 'large', ab_opt_out: true };
    const updated = await preferencesRequest(token, next);
    expect(updated.status).toBe(200);
    expect(await updated.json()).toEqual(next);

    expect(await (await preferencesRequest(token)).json()).toEqual(next);
    const otherToken = bearerOf('minister-li');
    expect(await (await preferencesRequest(otherToken)).json()).toEqual({
      theme: 'system',
      chat_font_size: 'standard',
      ab_opt_out: false,
    });
  });

  it('rejects incomplete or invalid preferences bodies', async () => {
    const token = bearerOf();
    const missing = await preferencesRequest(token, { theme: 'dark' });
    expect(missing.status).toBe(422);
    expect(await errorCode(missing)).toBe('validation_error');

    const invalid = await preferencesRequest(token, {
      theme: 'sepia',
      chat_font_size: 'standard',
      ab_opt_out: false,
    });
    expect(invalid.status).toBe(422);
    expect(await errorCode(invalid)).toBe('validation_error');
  });

  it('returns a normal user snapshot and makes a successful request visible as pending', async () => {
    const token = bearerOf('zhangsan');
    expect((await quotaRequest(token)).status).toBe(200);

    const created = await requestMoreQuota(token, { requested_pages: 100 });
    expect(created.status).toBe(201);
    expect(await errorCode(await requestMoreQuota(token, { requested_pages: 100 }, 'idem-2'))).toBe(
      'pending_request_exists',
    );
    expect((await (await quotaRequest(token)).json()).pending_request).toMatchObject({
      requested_pages: 100,
      quota_period: '2026-08',
    });
  });

  it('returns unlimited quota for ops and exposes no request mutation to that role', async () => {
    const token = bearerOf('ops-wang');
    expect((await (await quotaRequest(token)).json()).unlimited).toBe(true);
    expect((await requestMoreQuota(token, { requested_pages: 10 })).status).toBe(403);
  });

  it('rejects invalid body shapes and preserves idempotent replay semantics', async () => {
    const token = bearerOf('minister-li');
    expect(await errorCode(await requestMoreQuota(token, { requested_pages: 1.5 }))).toBe(
      'validation_error',
    );
    expect(
      await errorCode(await requestMoreQuota(token, { requested_pages: 50, extra: true }, 'extra-key')),
    ).toBe('validation_error');
    expect(await errorCode(await requestMoreQuota(token, { requested_pages: 50 }, ' '))).toBe(
      'validation_error',
    );

    const first = await requestMoreQuota(token, { requested_pages: 50 }, 'repeat-key');
    const second = await requestMoreQuota(token, { requested_pages: 50 }, 'repeat-key');
    expect(first.status).toBe(201);
    expect(await second.json()).toEqual(await first.clone().json());
    expect(await errorCode(await requestMoreQuota(token, { requested_pages: 51 }, 'repeat-key'))).toBe(
      'idempotency_key_conflict',
    );
  });
});
