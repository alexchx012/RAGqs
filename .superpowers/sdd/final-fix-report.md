# Final Review Fixes Report

**Branch:** `feature/20260714/frontend-spa-migration`
**Date:** 2026-07-14

## Summary

All 4 review findings fixed. TypeScript compilation passes. All 101 tests pass.

## Fix 1 [CRITICAL] ChatHistorySidebar: wire loadHistory onClick

**File:** `frontend/src/features/history/ChatHistorySidebar.tsx`

- Added `addMessage` to `useChat()` destructuring
- Added `loadHistory` to `useChatHistory()` destructuring
- Added `handleLoadHistory` callback that:
  1. Calls `loadHistory(h)` to load full message history
  2. Calls `clearChat()` to clear current chat
  3. Calls `regenerateSessionId()` to get a fresh session
  4. Iterates loaded messages and calls `addMessage(msg)` for each
- Wired `onClick` to each `.history-item` div calling `handleLoadHistory(h)`

## Fix 2 [IMPORTANT] useChatStream: progressive rendering

**Files:**
- `frontend/src/features/chat/ChatContext.tsx` -- Added `replaceLastMessage(msg)` to context interface and implementation
- `frontend/src/features/chat/useChatStream.ts` -- Progressive rendering during streaming

Implementation:
- On first content chunk: calls `addMessage()` with partial text
- On subsequent content chunks: calls `replaceLastMessage()` to update in-place
- On stream complete: finalizes with `replaceLastMessage()` (if streaming) or `addMessage()` (if no chunks received)

## Fix 3 [IMPORTANT] renderMarkdown: headerIds + mangle

**File:** `frontend/src/markdown/renderMarkdown.ts`

Investigation result: `headerIds` and `mangle` options do not exist in marked v11.
- `headerIds`: Heading IDs are only generated when the `marked-gfm-heading-id` extension is explicitly loaded. Not loaded here, so no IDs are generated (desired behavior).
- `mangle`: Email address mangling was removed entirely in marked v11 (desired behavior).

Added a comment documenting this rather than trying to set non-existent options.

## Fix 4 [IMPORTANT] apiJson/FileUpload: check res.ok before res.json()

**Files:**
- `frontend/src/api/client.ts` -- `apiJson` now checks `!res.ok` before `res.json()`
- `frontend/src/features/upload/FileUpload.tsx` -- `handleFileChange` now checks `res.ok` before `res.json()`

Error handling pattern in both:
1. Check `!res.ok` and get `res.status`
2. Try to parse JSON for `detail`/`message`
3. Fall back to `res.statusText` if JSON parse fails

**Test fix:** `frontend/src/features/upload/FileUpload.test.tsx`
- Updated mock fetch responses to include `ok: true` (success) and `ok: false` (error) properties

## Test Summary

- **Test files:** 11 passed / 0 failed
- **Tests:** 101 passed / 0 failed
- **TypeScript:** No compilation errors (`npx tsc --noEmit`)

## Concerns

None. All fixes are backward-compatible and tests pass.
