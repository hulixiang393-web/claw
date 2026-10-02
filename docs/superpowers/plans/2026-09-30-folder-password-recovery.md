# 收藏夹密码恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为收藏夹密码增加安全的恢复码重置流程，用户忘记密码时可通过恢复码设置新密码。

**Architecture:** 在现有 `framework/folder_lock.py` 中生成、哈希和校验恢复码；在收藏夹存储记录中增加恢复码哈希字段；在 `LibraryPage` 中增加“显示/复制恢复码”和“忘记密码”交互。旧收藏夹没有恢复码时只能在已验证当前密码后生成，恢复成功后轮换恢复码并保留原有收藏内容。

**Tech Stack:** Python 3、PySide6、现有收藏夹存储层、PBKDF2-HMAC-SHA256、pytest。

## Global Constraints

- 不保存明文密码或明文恢复码。
- 恢复码只在生成后显示一次，不能写入日志。
- 恢复流程必须轮换恢复码，旧恢复码立即失效。
- 旧收藏夹数据必须继续可读取；缺少恢复码字段时按未配置处理。
- 不修改收藏内容、收藏夹名称或现有密码校验语义。
- 不新增第三方依赖。
- 不提交无关的工作区变更。

---

### Task 1: Add recovery-code primitives

**Files:**
- Modify: `framework/folder_lock.py:12-41`
- Create: `tests/test_folder_lock.py`

**Interfaces:**
- Produces `new_recovery_code() -> str`.
- Produces `hash_recovery_code(code: str, salt_hex: str) -> str`.
- Produces `verify_recovery_code(code: str, code_b64: str, salt_hex: str) -> bool`.
- Produces `recovery_code_display(code: str) -> str` only if formatting is needed by the UI; otherwise the UI uses the generated code directly.

- [ ] **Step 1: Write failing tests**

```python
from framework.folder_lock import (
    hash_recovery_code,
    new_recovery_code,
    new_salt,
    verify_recovery_code,
)


def test_recovery_code_round_trip():
    code = new_recovery_code()
    salt = new_salt()
    stored = hash_recovery_code(code, salt)
    assert len(code) >= 24
    assert verify_recovery_code(code, stored, salt)
    assert not verify_recovery_code(code + "x", stored, salt)


def test_recovery_code_hash_is_not_plaintext():
    code = new_recovery_code()
    salt = new_salt()
    assert code not in hash_recovery_code(code, salt)


def test_invalid_recovery_code_metadata_fails_closed():
    assert not verify_recovery_code("abc", "", "bad-salt")
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m pytest tests/test_folder_lock.py -q`

Expected: FAIL because recovery-code functions do not exist.

- [ ] **Step 3: Implement the minimal primitives**

Use `secrets` for a high-entropy, copyable code such as grouped uppercase hex or base32; hash it with the same PBKDF2-HMAC-SHA256 parameters already used for passwords, with a fresh salt. Use `hmac.compare_digest` and return `False` for malformed metadata or empty input.

- [ ] **Step 4: Run the focused test and verify success**

Run: `python -m pytest tests/test_folder_lock.py -q`

Expected: PASS.

- [ ] **Step 5: Run existing password-lock tests**

Run: `python -m pytest tests/test_shelf_service.py tests/test_favorite_flow.py -q`

Expected: PASS; existing password behavior remains unchanged.

### Task 2: Persist recovery-code metadata

**Files:**
- Modify: `framework/library_store.py:105-202`
- Test: `tests/test_folder_lock.py`

**Interfaces:**
- `folder_info(name)` returns optional `recovery_pw` and `recovery_salt` fields without breaking records that lack them.
- Folder creation and password changes accept optional recovery metadata while preserving existing callers.
- A successful password reset can atomically update `pw`, `salt`, `recovery_pw`, and `recovery_salt` in one storage write.

- [ ] **Step 1: Add storage-level failing tests**

Cover these cases:

```python
def test_new_locked_folder_stores_recovery_metadata():
    info = store.folder_info("Private")
    assert info["recovery_pw"]
    assert info["recovery_salt"]


def test_legacy_folder_without_recovery_metadata_remains_readable():
    info = store.folder_info("Legacy")
    assert info.get("recovery_pw") in (None, "")
    assert info.get("recovery_salt") in (None, "")
```

- [ ] **Step 2: Run the focused storage tests and verify failure**

Run: `python -m pytest tests/test_folder_lock.py -q`

Expected: FAIL because folder records do not yet persist recovery metadata.

- [ ] **Step 3: Add backward-compatible metadata fields**

Keep legacy JSON/SQLite records valid. Add recovery fields only when supplied. Ensure `clear_folder_lock` removes both password and recovery metadata. Add a dedicated update path for password reset so a reset cannot leave the old recovery hash behind.

- [ ] **Step 4: Run storage and shelf regression tests**

Run: `python -m pytest tests/test_folder_lock.py tests/test_shelf_service.py tests/test_favorite_flow.py -q`

Expected: PASS.

### Task 3: Add UI generation and recovery flows

**Files:**
- Modify: `gui/pages/library_page.py:407-424`
- Modify: `gui/pages/library_page.py:803-821`
- Modify: `gui/pages/library_page.py:927-957`
- Modify: `gui/pages/library_page.py:909-923`
- Test: `tests/test_folder_lock.py` or a new focused GUI test matching the repository's existing Qt smoke-test style

**Interfaces:**
- Add `_generate_folder_recovery_code(name: str, require_old_password: bool) -> bool`.
- Add `_prompt_recover_folder_password(name: str) -> bool`.
- Add a context-menu action named `忘记密码 / 使用恢复码` for locked folders.
- Add a one-time dialog after password creation/change that displays the recovery code and provides copy capability.

- [ ] **Step 1: Write UI behavior tests or headless helper tests**

Verify that:

```python
def test_reset_with_valid_recovery_code_rotates_recovery_code():
    old_code = info["shown_code"]
    assert reset_with_code("Private", old_code, "new-password")
    assert verify_password("new-password", new_info["pw"], new_info["salt"])
    assert not verify_recovery_code(old_code, new_info["recovery_pw"], new_info["recovery_salt"])
```

Also cover cancellation, mismatched new passwords, wrong recovery code, and a legacy folder with no recovery metadata.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/test_folder_lock.py -q`

Expected: FAIL until the UI-facing reset helpers and storage update path exist.

- [ ] **Step 3: Implement the minimum UI flow**

When creating or changing a password, generate a fresh recovery code and persist only its hash/salt. Show the plaintext code once in a message box with a copy button or a clearly selectable text field. In the locked-folder menu, add the recovery action. The recovery dialog requests the code, new password, and confirmation; on success it unlocks the folder in memory, rebuilds the list, and shows the newly generated recovery code. Do not reveal whether a guessed password or recovery code was close to valid.

- [ ] **Step 4: Verify the Qt-facing behavior**

Run the focused folder-lock tests plus the repository's existing GUI smoke tests that cover library/favorite flows.

Expected: PASS without changing ordinary lock/unlock behavior.

### Task 4: Full verification and review

**Files:**
- Modify only files from Tasks 1-3.

- [ ] **Step 1: Run all folder and shelf tests**

Run: `python -m pytest tests/test_folder_lock.py tests/test_shelf_service.py tests/test_favorite_flow.py tests/test_library_series_gui.py -q`

Expected: PASS.

- [ ] **Step 2: Run lint/type/compile checks available in the repository**

Run: `python -m compileall -q framework gui tests` and the repository's configured lint/typecheck commands if present in project metadata.

Expected: PASS.

- [ ] **Step 3: Review the diff and security properties**

Run: `git diff --check`, `git diff --stat`, and `git status --short`.

Confirm no plaintext password/recovery code is written to logs, fixtures, or persistent records; no unrelated files are changed; and legacy folder records still load.

- [ ] **Step 4: Leave changes uncommitted unless explicitly requested**

Do not create a commit automatically.
