# ElectroGrader — Phase 1 Security Hardening: Final Report

**Scope:** authorization, session handling, and tenant isolation only — no new
features, no UI redesign, no workflow changes (per Task 7). Companion to
`TECHNICAL_AUDIT.md` §11, which this phase directly remediates.

**Status: all automated security tests pass.** 59/59 checks green in
`scripts/verify_tenant_isolation.py`; every changed file compiles; the app
boots cleanly under `streamlit run app.py` post-change (smoke-tested).

---

## 1. Authorization audit

Every action that modifies, deletes, exports, syncs, or downloads data now
has a server-side role check, not just a disabled UI hint. Pattern used
throughout (already the codebase's own established convention):

```python
try:
    auth.require_role(current_user, auth.ROLE_ADMIN)  # or ROLE_ADMIN, ROLE_REVIEWER
except PermissionError:
    st.error(T("common.admins_only")); st.stop()
```

### Final permission matrix

| Action | Admin | Reviewer | Employee |
|---|---|---|---|
| Delete product (bulk and single/card-view) | ✅ | ❌ | ❌ |
| Export to marketplace (bulk, and single "Push to BaseLinker") | ✅ | ✅ | ❌ |
| CSV / Excel Download | ✅ | ✅ | ❌ |
| Bulk Edit | ✅ | ❌ | ❌ |
| Change Status (bulk) | ✅ | ❌ | ❌ |
| Sync now / Pull now (single product) | ✅ | ✅ | ❌ |
| Preview export (read-only) | ✅ | ✅ | ✅ (deliberately open — exposes nothing beyond what's already on the product card) |
| Manage Users, Settings/Integrations, Import Manifest | ✅ | ❌ | ❌ (already correct pre-Phase-1, unchanged) |
| Individual product edit/Save, Add photos, repair events, New Item wizard Save | ✅ | ✅ | ✅ (unchanged — daily inventory workflow, out of scope) |

### Vulnerabilities found and fixed

| # | File:line | Vulnerability | Fix |
|---|---|---|---|
| A1 | `app.py:2682` `_confirm_export_dialog` | No server-side role check — only a `disabled=` hint on the trigger button | Added `require_role(ADMIN, REVIEWER)` at function top |
| A2 | `app.py:2763` `_confirm_delete_dialog` | Same — no server-side check | Added `require_role(ADMIN)` at function top |
| A3 | `app.py:2789` `_confirm_bulk_edit_dialog` | **Zero gate of any kind** — any role could bulk-edit every field on every selected product | Added `require_role(ADMIN)` at function top |
| A4 | `app.py:3019` `_confirm_change_status_dialog` | **Zero gate of any kind** | Added `require_role(ADMIN)` at function top |
| A5 | `app.py:4110-4138` Download popover | `st.download_button`'s `disabled=` does not stop Streamlit from computing `data=` and embedding the file bytes in the page payload — an `st.popover`'s body runs on every rerun regardless of open/disabled state. Every role's browser received the full export bytes, disabled button or not. | Restructured so `export.to_excel_bytes()`/`to_csv_bytes()` are only ever called inside `if can_export:` (Admin/Reviewer); unauthorized roles get only a caption, the export functions are never invoked |
| A6 | `app.py:3178` card-view Delete button | **No gate at all** — worse than the bulk Delete dialog, which at least had a disabled hint | Added `_can_delete_product = current_user.role == ROLE_ADMIN`, `disabled=`, and a server-side check in the click handler |
| A7 | `app.py:3561` "Push to BaseLinker" / "Sync now" / "Pull now" (single product) | **No gate at all** on any of the three | Added `_can_marketplace_action = role in (ADMIN, REVIEWER)`, `disabled=`, and server-side checks on all three; Preview export deliberately left open |

A5–A7 were not in the original `TECHNICAL_AUDIT.md` — found during this
phase's fresh, independent re-inventory of every action, which the original
audit's pass had missed (it caught the *bulk* actions' disabled-button
pattern but not that the single-product equivalents had no gate whatsoever).

---

## 2. Tenant isolation audit

Searched every `modules/*.py` for `company_id`/`tenant_id` usage in every
`DELETE`/`UPDATE`/filesystem write. Three gaps found, all fixed; two
call patterns explicitly reviewed and confirmed already safe.

### Vulnerabilities found and fixed

| # | File:line | Vulnerability | Fix |
|---|---|---|---|
| T1 | `modules/inventory_store.py` `delete_products_by_manifest` | Bulk `DELETE FROM products WHERE id = ?` had no `company_id` in the `WHERE` clause | Added `AND company_id = ?`, matching every other delete in the file |
| T2 | `modules/inventory_store.py:293` `delete_product`, and the loop inside `delete_products_by_manifest` | Both unconditionally cascaded into `product_translation_store.delete_translations_for_product(product_id)` with **no company check**, even though `product_translations` already has an indexed `company_id` column | `delete_translation()`/`delete_translations_for_product()` now require `company_id` and filter on it (`modules/product_translation_store.py:224-247`); both callers pass it through |
| T3 | `modules/auth_store.py` `update_user` | `UPDATE users SET email = ?, data = ? WHERE id = ?` — no `company_id`, and this is the one function that can flip `role`/`active` | Added `AND company_id = ?` using `user.company_id` (`modules/auth_store.py:155-168`) |
| T4 | `app.py` — `_save_imported_photo` (line 237), New Item wizard Save (line 2313), card-view Add Photos (line 3367) | **New finding, outside the original plan's known list**: uploaded-photo folders were built as `UPLOAD_DIR / sku_folder_name(sku, product_id)` with no company scoping. `inventory_store.next_sku_batch()` restarts auto-numbering from 2000 independently *per company* (`modules/inventory_store.py:444-461`), so two unrelated companies' first auto-numbered products both resolve to `data/uploads/2000/` — the *same physical folder*. If brand+model also match (plausible for common electronics — e.g. both listing "Apple iPhone 11"), the generated filename collides too, and one company's photo file is silently overwritten by the other's. This is a real, easily-reachable cross-tenant data-integrity gap, found while building the photo-isolation regression test that Task 4 asked for. | All three call sites now build `UPLOAD_DIR / product.company_id / sku_folder_name(...)`. Cascades correctly into the existing thumbnail/card-photo cache logic (`_ensure_thumbnail`/`_ensure_card_photo`, `app.py:124-184`) since those derive their own paths from the relative path under `UPLOAD_DIR`. Non-breaking: existing `image_paths` already store absolute paths, so previously-uploaded photos remain valid at their original location — only new uploads get the company-scoped path. |

### Verified safe, no change needed

- `sync/engine.py`'s `process_sync_queue()` and `sync_job_store.claim_due_jobs()`
  / `sync_queue_store.claim_pending()` intentionally operate across every
  company's queue rows in one call — correct shape for a single background
  worker draining a shared queue; each row still carries and is processed
  under its own `company_id`.
- `IntegrationManager.get()` (`integrations/manager.py:140-150`) resolves
  credentials strictly through `integration_store.get_integration(company_id, ...)`
  — no path for company-id confusion to leak another tenant's credentials;
  confirmed by test.

---

## 3. Session security

| Area | Status | Detail |
|---|---|---|
| Token storage | **Fixed** | `sessions.token` was plaintext; now stores `sha256(token)` only (`modules/auth_store.py:171-178`, applied in `create_session_row`/`get_session`/`delete_session`). One-time, expected side effect: every previously-live session was invalidated the moment this shipped (a hash can't reverse back into a browser's existing cookie) — accepted as the one-time cost of closing a plaintext-secret-at-rest gap. |
| Session invalidation on deactivation | **Fixed** | Deactivating a user in Manage Users previously left their session row sitting in the table until natural 14-day expiry (functionally blocked by `validate_session()`'s live re-check, but not actually revoked). Now calls `auth_store.delete_sessions_for_user(user_id)` (`app.py:4527`) — the row is immediately gone. |
| Expiration validation | Verified already correct | `validate_session()` checks `expires_at` on every call, independent of the opportunistic cleanup sweep. No change. |
| Logout | Verified already correct | Clears both the DB session row and the browser cookie. No change. |
| Cookie `HttpOnly` | Known, carried-forward limitation | Fixing this requires a reverse-proxy/ASGI layer in front of Streamlit — an infrastructure change outside this phase's code-only scope. Not silently dropped; flagged here explicitly. |

---

## 4. Login rate limiting — implementation

Added to `User` (`modules/auth_store.py:37-38`), zero-migration JSON-blob fields:
```python
failed_login_attempts: int = 0
locked_until: float = 0.0
```

`modules/auth.py:79-118` `verify_login()`: tracked **per user row**, not per
raw email string (email is only unique per-company — `get_users_by_email()`
can return several unrelated accounts across different companies sharing an
address). A locked candidate's password is never even checked. 5
(`LOCKOUT_THRESHOLD`) consecutive wrong-password attempts against a given
account locks it for 15 minutes (`LOCKOUT_SECONDS`); a successful login
resets both fields to zero.

`app.py:1220-1222` — the login form now shows a specific "too many attempts,
try again in N minutes" message (`login.locked_out` in `modules/i18n.py`,
EN+LV) instead of the generic wrong-password error, only on the failure path.

**Known edge case, tested and documented rather than silently accepted:**
because login has no company selector, a wrong-password attempt against a
shared email is checked against *every* account with that email, so a
determined attacker submitting wrong passwords against a shared address can
still lock out multiple companies' accounts together, not just their
intended target — each account's counter is independent, but they get
incremented in lockstep by the same attempts. Fixing this fully would
require adding a company selector to login, which is a workflow change and
therefore out of Phase 1's scope. The isolation suite's rate-limiting test
was designed around a non-shared email specifically to prove true per-account
isolation for the common case; this shared-email nuance is called out here
rather than glossed over.

---

## 5. Automated tests

`scripts/verify_tenant_isolation.py`, extended (not replaced) — same
scratch-PostgreSQL, two-real-company pattern already in use. **59/59 checks
pass.** New coverage added this phase:

- `auth_store.update_user` cross-tenant forgery attempt (confirms T3)
- `delete_products_by_manifest` cross-tenant no-op (confirms T1)
- `delete_product`'s cascade no longer deletes another company's translation row (confirms T2)
- Full `product_translation_store` cross-tenant matrix (get/list/delete)
- `integrations.manager.get()` raises `IntegrationNotConnectedError` for a company with no connection, and resolves only the calling company's own credentials
- Login rate limiting: lockout after 5 failures, locked account rejects even the correct password, an unrelated company's account is unaffected, lockout expires and the counter resets on success
- Photo-folder company scoping (T4) — **implemented as a static source-level check, not a dynamic one**: `app.py` cannot be safely `import`ed outside a live Streamlit `ScriptRunContext` (confirmed — it executes UI/session-state logic at module scope and raises `AttributeError` on a bare `import app`), so this check reads `app.py`'s source and asserts all three `UPLOAD_DIR / ... / sku_folder_name(...)` call sites include `.company_id`. This will catch a regression that removes the fix, but does not exercise the real filesystem write path end-to-end. Flagged here rather than presented as equivalent to the dynamic tests above.

Full run output (abbreviated — see terminal history for the complete 59-line list):
```
59 check(s) passed, 0 failed.
```

---

## 6. Verification performed

- ✅ `python -m compileall .` — whole repo, zero errors
- ✅ Extended `scripts/verify_tenant_isolation.py` — 59/59 pass
- ✅ `python -m streamlit run app.py` smoke test — server starts, login page returns HTTP 200, no startup errors in logs
- ⚠️ **Not performed in this session**: a manual, click-through pass logging in as each of Admin/Reviewer/Employee against a real browser session to visually confirm every gated button's error message and the full Import Manifest → New Item wizard → Save → edit → BaseLinker export happy path end-to-end. Every server-side check added was verified by direct code reading against the approved permission matrix, and is exercised indirectly by the automated suite, but a live UI walkthrough was not executed. Recommended as a final manual check before treating this as fully production-verified.

---

## 7. Summary

- **7 authorization gaps** closed (2 already-partially-gated dialogs hardened, 2 previously fully-open bulk dialogs gated, 1 popover fixed to stop computing export bytes for unauthorized roles, 2 single-product action groups gated to match their bulk equivalents).
- **4 tenant-isolation gaps** closed (1 originally known, 2 found via a fresh independent grep during planning, 1 filesystem-layer gap found while building this phase's own test suite).
- **Session tokens** now hashed at rest; **deactivation** now immediately revokes live sessions instead of relying solely on the passive re-check.
- **Login rate limiting** implemented, per-account, with a documented shared-email edge case.
- **59 automated cross-tenant regression tests**, all passing.
- No feature, workflow, or UI surface was redesigned; every fix either replaces a silent no-op/misleading UI with a clear permission error, or is invisible to a user who already has the required role.
