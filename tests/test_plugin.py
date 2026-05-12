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


def _make_plugin(is_ready=True, chat_response="hello"):
    settings = _settings()
    runtime = MagicMock()
    runtime.ensure_runtime.return_value = "zc-abc.ns.svc.cluster.local"
    runtime.is_ready.return_value = is_ready

    plugin = ZeroClawPlugin(settings=settings, runtime=runtime)
    plugin.driver = MagicMock()
    plugin.driver.client.userid = "bot-id"
    # mmpy_bot stores the plugin instance on the Function descriptor;
    # must be set manually when testing outside a running Bot.
    plugin.handle_message.plugin = plugin
    return plugin, runtime


def _make_message(text="hello", user_id="user1"):
    msg = MagicMock()
    msg.text = text
    msg.user_id = user_id
    msg.channel_id = "ch1"
    return msg


class TestHandleMessage:
    def test_replies_with_chat_response(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        with patch("app.bot.plugin.chat", return_value="response text"):
            plugin.handle_message(msg)
        plugin.driver.reply_to.assert_called_once_with(msg, "response text")

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

    def test_sends_starting_message_on_cold_start(self):
        plugin, runtime = _make_plugin(is_ready=False)
        msg = _make_message()
        with patch("app.bot.plugin.chat", return_value="resp"):
            plugin.handle_message(msg)
        calls = [c[0][1] for c in plugin.driver.reply_to.call_args_list]
        assert any("Starting" in c for c in calls)

    def test_replies_error_on_chat_failure(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        with patch("app.bot.plugin.chat", side_effect=RuntimeError("boom")):
            plugin.handle_message(msg)
        reply = plugin.driver.reply_to.call_args[0][1]
        assert "error" in reply.lower()

    def test_replies_timeout_on_pod_startup_timeout(self):
        plugin, runtime = _make_plugin(is_ready=False)
        runtime.wait_ready.side_effect = TimeoutError
        msg = _make_message()
        plugin.handle_message(msg)
        reply = plugin.driver.reply_to.call_args[0][1]
        assert "timed out" in reply.lower() or "timeout" in reply.lower()

    def test_updates_last_activity_on_success(self):
        plugin, runtime = _make_plugin(is_ready=True)
        msg = _make_message()
        with patch("app.bot.plugin.chat", return_value="ok"):
            plugin.handle_message(msg)
        assert runtime.update_last_activity.call_count == 2
