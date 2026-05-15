"""ZeroClawPlugin unit tests."""

from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.bot.plugin import ZeroClawPlugin


def _settings() -> Settings:
    return Settings(
        mattermost_url="http://mm",
        mattermost_team="t",
        mattermost_bot_token="tok",
        mattermost_bot_username="bot",
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
    plugin.driver.create_post.return_value = {"id": "post-123"}
    plugin.handle_message.plugin = plugin
    return plugin, runtime


def _make_message(text="hello", user_id="user1"):
    msg = MagicMock()
    msg.text = text
    msg.user_id = user_id
    msg.channel_id = "ch1"
    msg.reply_id = ""
    return msg


def _frames(*frames):
    return iter(frames)


class TestHandleMessage:
    def test_replies_with_chat_response(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "response text"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_message(msg)
        plugin.driver.posts.patch_post.assert_called_with("post-123", {"message": "response text"})

    def test_ignores_bot_own_messages(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(user_id="bot-id")
        plugin.handle_message(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_ignores_empty_text(self):
        plugin, runtime = _make_plugin()
        msg = _make_message(text="")
        plugin.handle_message(msg)
        runtime.ensure_runtime.assert_not_called()

    def test_posts_initial_message(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_message(msg)
        call_msg = plugin.driver.create_post.call_args[1]["message"]
        assert "Пожалуйста" in call_msg

    def test_patches_cold_start_message(self):
        plugin, runtime = _make_plugin(is_ready=False)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "resp"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_message(msg)
        patch_calls = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
        assert any("Запуск" in m for m in patch_calls)

    def test_patches_error_on_chat_failure(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        with patch("app.bot.plugin.chat_stream", side_effect=RuntimeError("boom")):
            plugin.handle_message(msg)
        last_patch = plugin.driver.posts.patch_post.call_args[0][1]["message"]
        assert "ошибка" in last_patch.lower()

    def test_patches_timeout_on_pod_startup_timeout(self):
        plugin, runtime = _make_plugin(is_ready=False)
        runtime.wait_ready.side_effect = TimeoutError
        msg = _make_message()
        plugin.handle_message(msg)
        last_patch = plugin.driver.posts.patch_post.call_args[0][1]["message"]
        assert "ожидания" in last_patch.lower() or "timeout" in last_patch.lower()

    def test_updates_last_activity_on_success(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames({"type": "done", "full_response": "ok"})
        with patch("app.bot.plugin.chat_stream", return_value=frames):
            plugin.handle_message(msg)
        assert runtime.update_last_activity.call_count == 2

    def test_streams_chunks_with_cursor(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        frames = _frames(
            {"type": "chunk", "content": "Hello"},
            {"type": "done", "full_response": "Hello world"},
        )
        with patch("app.bot.plugin.chat_stream", return_value=frames), \
             patch("app.bot.plugin.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 2.0, 2.0]
            plugin.handle_message(msg)
        patch_messages = [c[0][1]["message"] for c in plugin.driver.posts.patch_post.call_args_list]
        assert any("▌" in m for m in patch_messages)
        assert patch_messages[-1] == "Hello world"
