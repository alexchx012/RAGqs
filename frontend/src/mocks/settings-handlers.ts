import { http, HttpResponse } from 'msw';
import type { UserPreferences } from '../settings/types';
import { MockHttpError } from './auth-contract';
import { clearAuthCookies } from './dev-cookies';
import { type MockSettingsController } from './settings-contract';

const AVATAR_MAX_BYTES = 5 * 1024 * 1024;
const DISPLAY_NAME_MAX_LEN = 256;
const PASSWORD_MAX_LEN = 1024;

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
        request_id: `req_mock_settings_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

async function jsonObject(request: Request): Promise<Record<string, unknown>> {
  const body = await request.json().catch(() => null);
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new MockHttpError(422, 'validation_error');
  }
  return body as Record<string, unknown>;
}

function requireExactKeys(body: Record<string, unknown>, keys: readonly string[]): void {
  const actual = Object.keys(body);
  if (actual.length !== keys.length || !keys.every((key) => Object.hasOwn(body, key))) {
    throw new MockHttpError(422, 'validation_error');
  }
}

/** Profile display_name: raw string length 1..256; trim must stay non-empty and ≤256. */
function parseDisplayName(value: unknown): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > DISPLAY_NAME_MAX_LEN) {
    throw new MockHttpError(422, 'validation_error');
  }
  const trimmed = value.trim();
  if (trimmed.length < 1 || trimmed.length > DISPLAY_NAME_MAX_LEN) {
    throw new MockHttpError(422, 'validation_error');
  }
  return trimmed;
}

/** Password fields: raw string length 1..1024; no trim. */
function parsePasswordField(value: unknown): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > PASSWORD_MAX_LEN) {
    throw new MockHttpError(422, 'validation_error');
  }
  return value;
}

function parsePreferences(body: Record<string, unknown>): UserPreferences {
  requireExactKeys(body, ['theme', 'chat_font_size', 'ab_opt_out']);
  const theme = body['theme'];
  const chatFontSize = body['chat_font_size'];
  const abOptOut = body['ab_opt_out'];
  if (theme !== 'light' && theme !== 'dark' && theme !== 'system') {
    throw new MockHttpError(422, 'validation_error');
  }
  if (chatFontSize !== 'standard' && chatFontSize !== 'large') {
    throw new MockHttpError(422, 'validation_error');
  }
  if (typeof abOptOut !== 'boolean') {
    throw new MockHttpError(422, 'validation_error');
  }
  return { theme, chat_font_size: chatFontSize, ab_opt_out: abOptOut };
}

function parseRequestedPages(value: unknown): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 500) {
    throw new MockHttpError(422, 'validation_error');
  }
  return value;
}

function requireIdempotencyKey(request: Request): string {
  const key = request.headers.get('Idempotency-Key');
  if (key === null || key.trim() === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
  }
  return key;
}

function isAvatarFile(entry: FormDataEntryValue | null): entry is File {
  // Duck-type across MSW/jsdom File realms; plain strings and missing parts are invalid.
  return (
    entry !== null &&
    typeof entry !== 'string' &&
    typeof entry === 'object' &&
    'name' in entry &&
    'size' in entry &&
    'type' in entry &&
    typeof entry.name === 'string' &&
    typeof entry.size === 'number' &&
    typeof entry.type === 'string' &&
    typeof (entry as File).arrayBuffer === 'function'
  );
}

async function parseAvatarFile(entry: FormDataEntryValue | null): Promise<File> {
  if (!isAvatarFile(entry)) {
    throw new MockHttpError(422, 'validation_error');
  }
  // Read content length as well as metadata: some test environments rewrite empty File size.
  const bytes = await entry.arrayBuffer();
  if (
    bytes.byteLength < 1 ||
    entry.size < 1 ||
    entry.size > AVATAR_MAX_BYTES ||
    bytes.byteLength > AVATAR_MAX_BYTES ||
    !entry.type.startsWith('image/')
  ) {
    throw new MockHttpError(422, 'validation_error');
  }
  return entry;
}

export function createSettingsHandlers(controller: MockSettingsController) {
  return [
    http.patch('/v1/users/me/profile', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['display_name']);
        const user = controller.updateProfile(request.headers.get('Authorization'), {
          display_name: parseDisplayName(body['display_name']),
        });
        return HttpResponse.json(user);
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/users/me/avatar', async ({ request }) => {
      try {
        const form = await request.formData();
        const file = await parseAvatarFile(form.get('file'));
        return HttpResponse.json(
          controller.uploadAvatar(request.headers.get('Authorization'), file.name),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.put('/v1/users/me/password', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['old_password', 'new_password']);
        controller.changePassword(request.headers.get('Authorization'), {
          old_password: parsePasswordField(body['old_password']),
          new_password: parsePasswordField(body['new_password']),
        });
        // Successful password change has already invalidated every session, including this browser.
        clearAuthCookies();
        return new HttpResponse(null, { status: 204 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/users/me/preferences', ({ request }) => {
      try {
        return HttpResponse.json(controller.getPreferences(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.put('/v1/users/me/preferences', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        return HttpResponse.json(
          controller.updatePreferences(request.headers.get('Authorization'), parsePreferences(body)),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/quota/me', ({ request }) => {
      try {
        return HttpResponse.json(controller.getQuota(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/quota-requests', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['requested_pages']);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.requestQuota(
            request.headers.get('Authorization'),
            parseRequestedPages(body['requested_pages']),
            idempotencyKey,
          ),
          { status: 201 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
