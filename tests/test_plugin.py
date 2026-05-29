"""ZeroClawPlugin unit tests."""

import threading
import time
from unittest.mock import MagicMock, patch

from app.bot.plugin import ZeroClawPlugin, _SessionState
from app.config import Settings


def _settings() -> Settings:
    return Settings(
        mattermost_url="http://mm",
        mattermost_team="t",
        mattermost_bot_token="tok",
        mattermost_bot_username="bot",
        mattermost_thread_replies=True,
        allowed_models="gpt-4o-mini,gpt-4o",
        k8s_name_secret="test-secret",
        k8s_mode="kubeconfig",
    )


def _make_plugin(is_ready=True):
    settings = _settings()
    runtime = MagicMock()
    runtime.ensure_runtime.return_value = "zc-abc.ns.svc.cluster.local"
    runtime.is_ready.return_value = is_ready

    plugin = ZeroClawPlugin(settings=settings, runtime=runtime)
    plugin.driver = MagicMock()
    plugin.driver.client.userid = "bot-id"
    plugin.driver.user_id = "bot-id"
    plugin.driver.create_post.return_value = {"id": "post-123"}
    plugin.handle_dm.plugin = plugin
    plugin.handle_channel_mention.plugin = plugin
    plugin.handle_approval.plugin = plugin
    return plugin, runtime


def _make_message(text="hello", user_id="user1", is_direct_message=True, mentions=None):
    msg = MagicMock()
    msg.id = "post-1"
    msg.text = text
    msg.user_id = user_id
    msg.channel_id = "ch1"
    msg.root_id = ""
    msg.reply_id = ""
    msg.is_direct_message = is_direct_message
    msg.mentions = mentions or []
    return msg


def _frames(*frames):
    return iter(frames)


class TestHandleMessage:
    def test_replies_with_chat_response(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "response text"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        plugin.driver.posts.patch_post.assert_called_with("post-123", {"message": "response text"})

    def test_top_level_message_replies_in_thread_by_default(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "response text"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        assert plugin.driver.create_post.call_args[1]["root_id"] == "post-1"

    def test_thread_reply_uses_root_thread_for_scope_and_routing(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        msg.root_id = "root-1"
        msg.reply_id = "root-1"
        frames = _frames({"type": "done", "full_response": "response text"})
        with patch("app.bot.plugin.chat_stream", return_value=frames) as stream:
            plugin.handle_dm(msg)
        assert plugin.driver.create_post.call_args[1]["root_id"] == "root-1"
        ws_url = stream.call_args[0][0]
        assert "session_id=mm-" in ws_url

    def test_channel_scoped_when_thread_replies_disabled(self):
        plugin, runtime = _make_plugin(is_ready=True)
        plugin._settings.mattermost_thread_replies = False
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "response text"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        assert plugin.driver.create_post.call_args[1]["root_id"] == ""

    def test_new_command_starts_new_context_without_chat_stream(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(text="!new")
        with patch("app.bot.plugin.chat_stream") as stream:
            plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        stream.assert_not_called()
        assert (
            plugin.driver.create_post.call_args[1]["message"]
            == "Новый контекст начат для текущей ветки."
        )

    def test_new_command_changes_session_generation(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "one"})
        with patch("app.bot.plugin.chat_stream", return_value=frames) as stream:
            plugin.handle_dm(msg)
        first_url = stream.call_args[0][0]

        plugin.handle_dm(_make_message(text="!new"))

        frames = _frames({"type": "done", "full_response": "two"})
        with patch("app.bot.plugin.chat_stream", return_value=frames) as stream:
            plugin.handle_dm(msg)
        second_url = stream.call_args[0][0]
        assert first_url != second_url

    def test_clear_command_starts_new_context_without_chat_stream(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(text="!clear")
        with patch("app.bot.plugin.chat_stream") as stream:
            plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        stream.assert_not_called()
        assert plugin.driver.create_post.call_args[1]["message"] == "Контекст очищен."

    def test_clear_command_changes_session_generation(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "one"})
        with patch("app.bot.plugin.chat_stream", return_value=frames) as stream:
            plugin.handle_dm(msg)
        first_url = stream.call_args[0][0]

        plugin.handle_dm(_make_message(text="!clear"))

        frames = _frames({"type": "done", "full_response": "two"})
        with patch("app.bot.plugin.chat_stream", return_value=frames) as stream:
            plugin.handle_dm(msg)
        second_url = stream.call_args[0][0]
        assert first_url != second_url

    def test_help_command_returns_help_text(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(text="!help")
        with patch("app.bot.plugin.chat_stream") as stream:
            plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        stream.assert_not_called()
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "!new" in reply
        assert "!clear" in reply
        assert "!stop" in reply
        assert "!env" in reply

    def test_stop_command_signals_active_stream(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="!stop")
        scope = plugin._session_scope(msg)
        stop_event = threading.Event()
        plugin._sessions[scope] = _SessionState(stop_event=stop_event)
        plugin.handle_dm(msg)
        assert stop_event.is_set()
        runtime.ensure_runtime.assert_not_called()
        plugin.driver.create_post.assert_called_with(
            channel_id="ch1", message="Выполнение остановлено.", root_id="post-1"
        )

    def test_stop_command_with_no_active_stream(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="!stop")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        plugin.driver.create_post.assert_called_with(
            channel_id="ch1", message="Нет активного выполнения.", root_id="post-1"
        )

    def test_ignores_bot_own_messages(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(user_id="bot-id")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_ignores_empty_text(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_posts_initial_message(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        call_msg = plugin.driver.create_post.call_args[1]["message"]
        assert call_msg == "_Запрос получен. Готовлю сессию..._"

    def test_posts_before_ensuring_runtime(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})

        def ensure_runtime(_user_id, *, user_id=None):
            assert plugin.driver.create_post.called
            return "zc-abc.ns.svc.cluster.local"

        runtime.ensure_runtime.side_effect = ensure_runtime
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)

        assert (
            plugin.driver.create_post.call_args_list[0][1]["message"]
            == "_Запрос получен. Готовлю сессию..._"
        )

    def test_patches_ready_duration_after_cold_start(self):
        plugin, runtime = _make_plugin(is_ready=False)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})
        with (
            patch("app.bot.plugin.chat_stream", return_value=frames),
            patch("app.bot.plugin.time") as mock_time,
        ):
            mock_time.monotonic.side_effect = [100.0, 100.0, 100.0, 100.0, 110.2, 111.0]
            plugin.handle_dm(msg)

        patch_messages = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
        assert "Готов. Заняло 10с" in patch_messages

    def test_patches_cold_start_message(self):
        plugin, runtime = _make_plugin(is_ready=False)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        patch_messages = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
        assert any("Готов. Заняло" in message for message in patch_messages)

    def test_patches_error_on_chat_failure(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        with patch("app.bot.plugin.chat_stream", side_effect=RuntimeError("boom")):
            plugin.handle_dm(msg)
        last_patch = plugin.driver.posts.patch_post.call_args[0][1]["message"]
        assert "ошибка" in last_patch.lower()

    def test_patches_timeout_on_pod_startup_timeout(self):
        plugin, runtime = _make_plugin(is_ready=False)
        runtime.wait_ready.side_effect = TimeoutError
        msg = _make_message()
        plugin.handle_dm(msg)
        last_patch = plugin.driver.posts.patch_post.call_args[0][1]["message"]
        assert "ожидания" in last_patch.lower() or "timeout" in last_patch.lower()

    def test_updates_last_activity_on_success(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "ok"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        assert runtime.update_last_activity.call_count == 1

    def test_streams_chunks_with_cursor(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames(
            {"type": "chunk", "content": "Hello"},
            {"type": "done", "full_response": "Hello world"},
        )
        with (
            patch("app.bot.plugin.chat_stream", return_value=frames),
            patch("app.bot.stream_handler.time") as mock_time,
        ):
            mock_time.monotonic.side_effect = [0.0, 2.0, 3.0]
            plugin.handle_dm(msg)
        patch_messages = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
        assert any("▌" in m for m in patch_messages)
        assert patch_messages[-1] == "Hello world"


class TestSoulIdentityCommands:
    def test_soul_show_with_override(self):
        plugin, runtime = _make_plugin()
        runtime.get_workspace_file.return_value = "# Custom SOUL"
        msg = _make_message(text="!soul show")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "Custom SOUL" in reply

    def test_soul_show_without_override(self):
        plugin, runtime = _make_plugin()
        runtime.get_workspace_file.return_value = None
        msg = _make_message(text="!soul show")
        plugin.handle_dm(msg)
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "умолчанию" in reply.lower() or "default" in reply.lower()

    def test_soul_bare_command_acts_as_show(self):
        plugin, runtime = _make_plugin()
        runtime.get_workspace_file.return_value = None
        msg = _make_message(text="!soul")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_soul_set_posts_edit_button(self):
        plugin, runtime = _make_plugin()
        runtime.get_workspace_file.return_value = ""
        msg = _make_message(text="!soul set")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        call_options = plugin.driver.posts.create_post.call_args[1]["options"]
        actions = call_options["props"]["attachments"][0]["actions"]
        assert any("SOUL.md" in a["name"] for a in actions)

    def test_soul_reset_found(self):
        plugin, runtime = _make_plugin()
        runtime.reset_workspace_file.return_value = True
        msg = _make_message(text="!soul reset")
        plugin.handle_dm(msg)
        runtime.reset_workspace_file.assert_called_once_with("user1", "SOUL.md")
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "✅" in reply

    def test_soul_reset_not_found(self):
        plugin, runtime = _make_plugin()
        runtime.reset_workspace_file.return_value = False
        msg = _make_message(text="!soul reset")
        plugin.handle_dm(msg)
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "✅" not in reply

    def test_identity_set_posts_edit_button(self):
        plugin, runtime = _make_plugin()
        runtime.get_workspace_file.return_value = ""
        msg = _make_message(text="!identity set")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        call_options = plugin.driver.posts.create_post.call_args[1]["options"]
        actions = call_options["props"]["attachments"][0]["actions"]
        assert any("IDENTITY.md" in a["name"] for a in actions)

    def test_identity_reset_calls_correct_filename(self):
        plugin, runtime = _make_plugin()
        runtime.reset_workspace_file.return_value = True
        msg = _make_message(text="!identity reset")
        plugin.handle_dm(msg)
        runtime.reset_workspace_file.assert_called_once_with("user1", "IDENTITY.md")

    def test_soul_unknown_subcommand_returns_usage(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="!soul bogus")
        plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_not_called()
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "!soul show" in reply

    def test_help_includes_soul_and_identity(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="!help")
        plugin.handle_dm(msg)
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "!soul" in reply
        assert "!identity" in reply


class TestModelCommands:
    def test_model_show_returns_current_model(self):
        plugin, runtime = _make_plugin()
        runtime.get_user_model.return_value = "gpt-4o"
        msg = _make_message(text="!model show")

        plugin.handle_dm(msg)

        runtime.ensure_runtime.assert_not_called()
        runtime.get_user_model.assert_called_once_with("user1")
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "gpt-4o" in reply

    def test_model_list_marks_current_model(self):
        plugin, runtime = _make_plugin()
        runtime.get_user_model.return_value = "gpt-4o"
        msg = _make_message(text="!model list")

        plugin.handle_dm(msg)

        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "gpt-4o-mini" in reply
        assert "gpt-4o" in reply
        assert "current" in reply.lower() or "текущ" in reply.lower()

    def test_model_set_accepts_allowed_model(self):
        plugin, runtime = _make_plugin()
        runtime.set_user_model.return_value = True
        msg = _make_message(text="!model set gpt-4o")

        plugin.handle_dm(msg)

        runtime.set_user_model.assert_called_once_with("user1", "gpt-4o")
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "gpt-4o" in reply
        assert "✅" in reply

    def test_model_set_rejects_unknown_model(self):
        plugin, runtime = _make_plugin()
        runtime.set_user_model.return_value = False
        msg = _make_message(text="!model set bad-model")

        plugin.handle_dm(msg)

        runtime.set_user_model.assert_called_once_with("user1", "bad-model")
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "bad-model" in reply
        assert "gpt-4o-mini" in reply

    def test_model_reset_calls_runtime(self):
        plugin, runtime = _make_plugin()
        runtime.reset_user_model.return_value = True
        msg = _make_message(text="!model reset")

        plugin.handle_dm(msg)

        runtime.reset_user_model.assert_called_once_with("user1")
        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "✅" in reply

    def test_help_includes_model(self):
        plugin, _ = _make_plugin()
        msg = _make_message(text="!help")

        plugin.handle_dm(msg)

        reply = plugin.driver.create_post.call_args[1]["message"]
        assert "!model" in reply


class TestApprovalRequests:
    def test_returns_always_decision(self):
        plugin, _ = _make_plugin()
        plugin.driver.posts.create_post.return_value = {"id": "approval-post"}
        frame = {
            "request_id": "req-1",
            "tool": "shell",
            "arguments_summary": "ls",
            "timeout_secs": 1,
        }
        result = {}

        thread = threading.Thread(
            target=lambda: result.setdefault(
                "decision", plugin._request_approval(frame, "ch1", "root1", "post-123")
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while "req-1" not in plugin._pending_approvals and time.monotonic() < deadline:
            time.sleep(0.01)

        event = MagicMock()
        event.context = {"request_id": "req-1", "decision": "always"}
        event.user_name = "alice"
        plugin.handle_approval(event)

        thread.join(timeout=1)
        assert result["decision"] == "always"
        plugin.driver.respond_to_web.assert_called_once()
        plugin.driver.posts.delete_post.assert_called_once_with("approval-post")

    def test_returns_timeout_decision(self):
        plugin, _ = _make_plugin()
        plugin.driver.posts.create_post.return_value = {"id": "approval-post"}
        frame = {"request_id": "req-2", "tool": "shell", "timeout_secs": 0.01}

        decision = plugin._request_approval(frame, "ch1", "root1", "post-123")

        assert decision == "timeout"
        assert "req-2" not in plugin._pending_approvals


class TestChannelHandling:
    def test_channel_handler_skips_direct_messages(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(is_direct_message=True)
        plugin.handle_channel_mention(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_channel_handler_uses_channel_id_as_runtime_key(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(is_direct_message=False, mentions=["bot-id"])
        msg.channel_id = "channel-abc"
        frames = iter([{"type": "done", "full_response": "ok"}])
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_channel_mention(msg)
        runtime.ensure_runtime.assert_called_once_with("channel-abc", user_id="user1")

    def test_channel_scope_omits_user_id(self):
        plugin, _ = _make_plugin()
        msg = _make_message(user_id="user1", is_direct_message=False)
        msg.channel_id = "channel-abc"
        scope = plugin._session_scope(msg, is_channel=True)
        assert "user1" not in scope
        assert "channel-abc" in scope
        assert scope.startswith("mattermost_channel_")

    def test_dm_scope_includes_user_id(self):
        plugin, _ = _make_plugin()
        msg = _make_message(user_id="user1")
        scope = plugin._session_scope(msg, is_channel=False)
        assert "user1" in scope
        assert not scope.startswith("mattermost_channel_")

    def test_two_users_share_channel_scope(self):
        plugin, _ = _make_plugin()
        msg1 = _make_message(user_id="user1", is_direct_message=False)
        msg1.channel_id = "channel-abc"
        msg2 = _make_message(user_id="user2", is_direct_message=False)
        msg2.channel_id = "channel-abc"
        assert plugin._session_scope(msg1, is_channel=True) == plugin._session_scope(
            msg2, is_channel=True
        )

    def test_channel_handler_updates_activity_with_channel_id(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(is_direct_message=False, mentions=["bot-id"])
        msg.channel_id = "channel-abc"
        frames = iter([{"type": "done", "full_response": "ok"}])
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_channel_mention(msg)
        runtime.update_last_activity.assert_called_with("channel-abc")

    def test_channel_handler_always_replies_in_thread(self):
        plugin, runtime = _make_plugin(is_ready=True)
        plugin._settings.mattermost_thread_replies = False  # even when DM threading is off
        msg = _make_message(is_direct_message=False, mentions=["bot-id"])
        msg.channel_id = "channel-abc"
        msg.id = "mention-post-id"
        msg.root_id = ""
        frames = iter([{"type": "done", "full_response": "ok"}])
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_channel_mention(msg)
        assert plugin.driver.create_post.call_args[1]["root_id"] == "mention-post-id"

    def test_dm_handler_uses_user_id_as_runtime_key(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message(user_id="user1", is_direct_message=True)
        frames = iter([{"type": "done", "full_response": "ok"}])
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_dm(msg)
        runtime.ensure_runtime.assert_called_once_with("user1", user_id="user1")
