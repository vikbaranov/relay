# Agent UX Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Mattermost bot interactions feel responsive by posting immediately, reporting cold-start readiness duration, and removing approval forms after resolution.

**Architecture:** Keep the existing single-post streaming model. `ZeroClawPlugin.handle_message()` will create the reply post before runtime provisioning, then patch that post during cold-start readiness and streaming. `ApprovalManager.resolve()` will delete the approval post after a valid decision while preserving approval resolution even if deletion fails.

**Tech Stack:** Python, mmpy_bot Mattermost driver mocks, pytest, Kubernetes runtime abstraction.

---

## File Structure

- Modify `app/bot/plugin.py`: create the response post before `ensure_runtime()`, patch it with ready duration after cold starts, and reuse it for streaming.
- Modify `app/bot/approval.py`: delete the approval Mattermost post after a valid approval decision.
- Modify `tests/test_plugin.py`: cover immediate first response, ready-duration patching, and approval post deletion.

---

### Task 1: Immediate Reply And Ready Duration

**Files:**
- Modify: `tests/test_plugin.py`
- Modify: `app/bot/plugin.py`

- [ ] **Step 1: Write failing tests**

Add or update tests in `tests/test_plugin.py`:

```python
def test_posts_before_ensuring_runtime(self):
    plugin, runtime = _make_plugin(is_ready=True)
    msg = _make_message()
    frames = _frames({"type": "done", "full_response": "resp"})

    def ensure_runtime(_user_id):
        assert plugin.driver.create_post.called
        return "zc-abc.ns.svc.cluster.local"

    runtime.ensure_runtime.side_effect = ensure_runtime
    with patch("app.bot.plugin.chat_stream", return_value=frames):
        plugin.handle_message(msg)

    assert plugin.driver.create_post.call_args_list[0][1]["message"] == "_Запрос получен. Готовлю сессию..._"


def test_patches_ready_duration_after_cold_start(self):
    plugin, runtime = _make_plugin(is_ready=False)
    msg = _make_message()
    frames = _frames({"type": "done", "full_response": "resp"})
    with (
        patch("app.bot.plugin.chat_stream", return_value=frames),
        patch("app.bot.plugin.time") as mock_time,
    ):
        mock_time.monotonic.side_effect = [100.0, 100.0, 100.0, 110.2, 110.2, 111.0]
        plugin.handle_message(msg)

    patch_messages = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
    assert "Готов. Заняло 10с" in patch_messages
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_plugin.py::TestHandleMessage::test_posts_before_ensuring_runtime tests/test_plugin.py::TestHandleMessage::test_patches_ready_duration_after_cold_start -v`

Expected: at least one test fails because the response post is currently created after `ensure_runtime()` and the ready-duration patch does not exist.

- [ ] **Step 3: Implement minimal plugin change**

In `app/bot/plugin.py`, create the post immediately after logging message receipt:

```python
post = self.driver.create_post(
    channel_id=message.channel_id,
    message="_Запрос получен. Готовлю сессию..._",
    root_id=root_id,
)
post_id = post["id"]
```

Then call `ensure_runtime()`. If runtime is cold, measure `wait_ready()` elapsed time and patch the existing post:

```python
ready_start = time.monotonic()
self._runtime.wait_ready(service_dns)
ready_elapsed = round(time.monotonic() - ready_start)
patch_post(self.driver, post_id, f"Готов. Заняло {ready_elapsed}с")
```

Remove the old branches that created separate startup/cursor posts after readiness checks. Keep timeout handling patching the same `post_id`.

- [ ] **Step 4: Run targeted tests and verify pass**

Run: `pytest tests/test_plugin.py::TestHandleMessage::test_posts_before_ensuring_runtime tests/test_plugin.py::TestHandleMessage::test_patches_ready_duration_after_cold_start -v`

Expected: both tests pass.

---

### Task 2: Delete Approval Form After Resolution

**Files:**
- Modify: `tests/test_plugin.py`
- Modify: `app/bot/approval.py`

- [ ] **Step 1: Write failing test**

Add this assertion to `TestApprovalRequests.test_returns_always_decision` after `plugin.driver.respond_to_web.assert_called_once()`:

```python
plugin.driver.posts.delete_post.assert_called_once_with("approval-post")
```

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/test_plugin.py::TestApprovalRequests::test_returns_always_decision -v`

Expected: FAIL because `delete_post()` is not called today.

- [ ] **Step 3: Implement minimal approval deletion**

In `app/bot/approval.py`, read `approval_post_id` from the pending approval and call `self._driver.posts.delete_post(approval_post_id)` after `pending["event"].set()`. Wrap deletion in `try/except OSError` so a Mattermost API failure does not prevent approval resolution.

- [ ] **Step 4: Run targeted approval test and verify pass**

Run: `pytest tests/test_plugin.py::TestApprovalRequests::test_returns_always_decision -v`

Expected: PASS.

---

### Task 3: Full Regression

**Files:**
- Verify: `tests/test_plugin.py`

- [ ] **Step 1: Run plugin tests**

Run: `pytest tests/test_plugin.py -v`

Expected: all plugin tests pass.

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`

Expected: all tests pass.

---

## Self-Review

- Spec coverage: immediate first response is covered by Task 1; cold-start ready duration is covered by Task 1; approval form deletion is covered by Task 2.
- Placeholder scan: no placeholders remain.
- Type consistency: all paths, method names, and mock calls match current code structure.
