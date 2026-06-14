"""TokenCommandHandler unit tests."""

from unittest.mock import MagicMock

from app.bot.commands import TokenCommandHandler


def _make_handler():
    driver = MagicMock()
    user_state = MagicMock()
    restart_fn = MagicMock()
    handler = TokenCommandHandler(
        get_driver=lambda: driver,
        base_url="http://localhost:8579",
        user_state=user_state,
        restart_fn=restart_fn,
    )
    return handler, driver, user_state, restart_fn


def _msg(text, user_id="user1", channel_id="ch1"):
    msg = MagicMock()
    msg.text = text
    msg.user_id = user_id
    msg.channel_id = channel_id
    return msg


class TestTokenCommandHandlerShow:
    def test_show_reports_not_set_when_no_override(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.get_user_token.return_value = None

        handler.handle(_msg("!token show"), root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert "не установлен" in reply

    def test_show_displays_masked_token_when_set(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.get_user_token.return_value = "sk-my-test-key"

        handler.handle(_msg("!token show"), root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert "sk-...-key" in reply
        assert "sk-my-test-key" not in reply  # full key must not appear

    def test_show_uses_runtime_key_over_user_id(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.get_user_token.return_value = None

        handler.handle(_msg("!token show"), root_id="", runtime_key="channel-key")

        user_state.get_user_token.assert_called_once_with("channel-key")

    def test_bare_token_command_defaults_to_show(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.get_user_token.return_value = None

        handler.handle(_msg("!token"), root_id="")

        # bare !token should behave like !token show
        driver.create_post.assert_called_once()
        reply = driver.create_post.call_args[1]["message"]
        assert "не установлен" in reply

    def test_unknown_subcommand_returns_usage(self):
        handler, driver, user_state, restart_fn = _make_handler()

        handler.handle(_msg("!token bogus"), root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert "!token set" in reply


class TestTokenCommandHandlerSet:
    def test_set_posts_button_with_dialog_url(self):
        handler, driver, user_state, restart_fn = _make_handler()

        handler.handle(_msg("!token set"), root_id="root1")

        driver.posts.create_post.assert_called_once()
        opts = driver.posts.create_post.call_args[1]["options"]
        attachments = opts["props"]["attachments"]
        action_url = attachments[0]["actions"][0]["integration"]["url"]
        assert "token_set_dialog" in action_url

    def test_set_embeds_pod_key_in_context(self):
        handler, driver, user_state, restart_fn = _make_handler()

        handler.handle(_msg("!token set", user_id="alice"), root_id="", runtime_key="rkey1")

        opts = driver.posts.create_post.call_args[1]["options"]
        ctx = opts["props"]["attachments"][0]["actions"][0]["integration"]["context"]
        assert ctx["pod_key"] == "rkey1"


class TestTokenCommandHandlerReset:
    def test_reset_confirms_when_token_was_set(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.reset_user_token.return_value = True

        handler.handle(_msg("!token reset"), root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert "✅" in reply

    def test_reset_reports_not_set_when_no_override(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.reset_user_token.return_value = False

        handler.handle(_msg("!token reset"), root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert "не был установлен" in reply

    def test_reset_uses_runtime_key_over_user_id(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.reset_user_token.return_value = False

        handler.handle(_msg("!token reset"), root_id="", runtime_key="rkey")

        user_state.reset_user_token.assert_called_once_with("rkey")

    def test_reset_calls_restart_fn_when_token_was_set(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.reset_user_token.return_value = True

        handler.handle(_msg("!token reset", user_id="user1"), root_id="")

        restart_fn.assert_called_once_with("user1")

    def test_reset_does_not_call_restart_fn_when_not_set(self):
        handler, driver, user_state, restart_fn = _make_handler()
        user_state.reset_user_token.return_value = False

        handler.handle(_msg("!token reset"), root_id="")

        restart_fn.assert_not_called()


class TestEnvListFiltersTokenKey:
    def test_env_list_hides_token_key(self):
        from app.bot.commands import EnvCommandHandler
        from app.k8s.user_state import TOKEN_KEY

        driver = MagicMock()
        user_state = MagicMock()
        user_state.list_user_envs.return_value = ["MY_VAR", TOKEN_KEY]
        handler = EnvCommandHandler(
            get_driver=lambda: driver,
            base_url="http://localhost:8579",
            user_state=user_state,
            restart_fn=MagicMock(),
        )
        msg = _msg("!env list")

        handler.handle(msg, root_id="")

        reply = driver.create_post.call_args[1]["message"]
        assert TOKEN_KEY not in reply
        assert "MY_VAR" in reply


class TestTokenDialogHandler:
    def _make_dialog_handler(self):
        from app.bot.dialogs import TokenDialogHandler

        driver = MagicMock()
        user_state = MagicMock()
        handler = TokenDialogHandler(
            get_driver=lambda: driver,
            user_state=user_state,
            base_url="http://localhost:8579",
            restart_fn=MagicMock(),
        )
        return handler, driver, user_state

    def _make_event(self, context=None, post_id="post-1", trigger_id="trig-1"):
        event = MagicMock()
        event.context = context or {}
        event.post_id = post_id
        event.trigger_id = trigger_id
        event.body = {}
        return event

    def test_open_calls_open_interactive_dialog(self):
        handler, driver, _ = self._make_dialog_handler()
        event = self._make_event(context={"pod_key": "user1", "root_id": "root1"})

        handler.open(event)

        driver.integration_actions.open_interactive_dialog.assert_called_once()
        call_arg = driver.integration_actions.open_interactive_dialog.call_args[0][0]
        assert "token_set_submit" in call_arg["url"]
        assert call_arg["dialog"]["elements"][0]["subtype"] == "password"

    def test_submit_saves_token_and_confirms(self):
        import json

        handler, driver, user_state = self._make_dialog_handler()
        event = self._make_event()
        event.body = {
            "state": json.dumps({"pod_key": "user1", "root_id": "root1", "prompt_post_id": ""}),
            "submission": {"value": "sk-new-key"},
            "channel_id": "ch1",
        }

        handler.submit(event)

        user_state.set_user_token.assert_called_once_with("user1", "sk-new-key")
        driver.create_post.assert_called_once()
        assert "✅" in driver.create_post.call_args[1]["message"]

    def test_submit_rejects_empty_value(self):
        import json

        handler, driver, user_state = self._make_dialog_handler()
        event = self._make_event()
        event.body = {
            "state": json.dumps({"pod_key": "user1", "root_id": "", "prompt_post_id": ""}),
            "submission": {"value": ""},
            "channel_id": "ch1",
        }

        handler.submit(event)

        user_state.set_user_token.assert_not_called()
        driver.respond_to_web.assert_called_with(
            event, {"errors": {"value": "API-ключ не может быть пустым."}}
        )

    def test_submit_handles_cancel(self):
        import json

        handler, driver, user_state = self._make_dialog_handler()
        event = self._make_event()
        event.body = {
            "state": json.dumps({"pod_key": "user1", "root_id": "", "prompt_post_id": ""}),
            "cancelled": True,
            "channel_id": "ch1",
        }

        handler.submit(event)

        user_state.set_user_token.assert_not_called()
        driver.respond_to_web.assert_called_with(event, {})
