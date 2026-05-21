import json
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class DialogHandler:
    def __init__(self, get_driver: Callable, runtime, base_url: str) -> None:
        self._get_driver = get_driver
        self._runtime = runtime
        self._base_url = base_url

    @property
    def _driver(self):
        return self._get_driver()

    def _patch_props(self, post_id: str, text: str) -> None:
        self._driver.posts.patch_post(post_id, {"props": {"attachments": [{"text": text}]}})

    def open_workspace_file_dialog(self, event) -> None:
        context = event.context or {}
        filename = context.get("filename", "")
        current = context.get("current", "")
        user_id = context.get("user_id", "")
        root_id = context.get("root_id", "")
        state = json.dumps(
            {
                "filename": filename,
                "user_id": user_id,
                "root_id": root_id,
                "prompt_post_id": event.post_id or "",
            }
        )
        self._driver.integration_actions.open_interactive_dialog(
            {
                "trigger_id": event.trigger_id,
                "url": f"{self._base_url}/hooks/workspace_file_submit",
                "dialog": {
                    "title": f"Редактировать {filename}",
                    "submit_label": "Сохранить",
                    "notify_on_cancel": True,
                    "state": state,
                    "elements": [
                        {
                            "display_name": "Содержимое",
                            "name": "content",
                            "type": "textarea",
                            "default": current,
                            "placeholder": f"Введите содержимое {filename}...",
                        }
                    ],
                },
            }
        )
        self._driver.respond_to_web(event, {})

    def submit_workspace_file(self, event) -> None:
        body = event.body
        state = json.loads(body.get("state") or "{}")
        filename = state.get("filename", "")
        user_id = state.get("user_id", "")
        root_id = state.get("root_id", "")
        prompt_post_id = state.get("prompt_post_id", "")
        channel_id = body.get("channel_id", "")

        if body.get("cancelled"):
            if prompt_post_id:
                self._patch_props(prompt_post_id, f"❌ Редактирование `{filename}` отменено.")
            self._driver.respond_to_web(event, {})
            return

        content = (body.get("submission") or {}).get("content", "")
        if not content:
            self._driver.respond_to_web(
                event, {"errors": {"content": "Содержимое не может быть пустым."}}
            )
            return

        try:
            self._runtime.set_workspace_file(user_id, filename, content)
            result = f"✅ `{filename}` сохранён. Сессия будет перезапущена автоматически."
        except Exception:
            logger.exception(
                "workspace_file_set_failed filename=%s", filename, extra={"mm_user_id": user_id}
            )
            result = f"❌ Ошибка при сохранении `{filename}`."

        if prompt_post_id:
            self._patch_props(prompt_post_id, result)
        else:
            self._driver.create_post(channel_id=channel_id, message=result, root_id=root_id)
        self._driver.respond_to_web(event, {})

    def open_env_set_dialog(self, event) -> None:
        context = event.context or {}
        key = context.get("key", "")
        user_id = context.get("user_id", "")
        root_id = context.get("root_id", "")
        state = json.dumps(
            {
                "key": key,
                "user_id": user_id,
                "root_id": root_id,
                "prompt_post_id": event.post_id or "",
            }
        )
        self._driver.integration_actions.open_interactive_dialog(
            {
                "trigger_id": event.trigger_id,
                "url": f"{self._base_url}/hooks/env_set_submit",
                "dialog": {
                    "title": f"Установить {key}",
                    "submit_label": "Сохранить",
                    "notify_on_cancel": True,
                    "state": state,
                    "elements": [
                        {
                            "display_name": "Значение",
                            "name": "value",
                            "type": "text",
                            "subtype": "password",
                            "placeholder": "Введите значение...",
                        }
                    ],
                },
            }
        )
        self._driver.respond_to_web(event, {})

    def submit_env_set(self, event) -> None:
        body = event.body
        state = json.loads(body.get("state") or "{}")
        key = state.get("key", "")
        user_id = state.get("user_id", "")
        root_id = state.get("root_id", "")
        prompt_post_id = state.get("prompt_post_id", "")
        channel_id = body.get("channel_id", "")

        if body.get("cancelled"):
            if prompt_post_id:
                self._patch_props(prompt_post_id, f"❌ Ввод `{key}` отменён.")
            self._driver.respond_to_web(event, {})
            return

        value = (body.get("submission") or {}).get("value", "")
        if not value:
            self._driver.respond_to_web(
                event, {"errors": {"value": "Значение не может быть пустым."}}
            )
            return

        try:
            self._runtime.set_user_env(user_id, key, value)
            result = f"✅ `{key}` сохранён. Сессия будет перезапущена автоматически."
        except Exception:
            logger.exception("env_set_failed key=%s", key, extra={"mm_user_id": user_id})
            result = f"❌ Ошибка при сохранении `{key}`."

        if prompt_post_id:
            self._patch_props(prompt_post_id, result)
        else:
            self._driver.create_post(channel_id=channel_id, message=result, root_id=root_id)
        self._driver.respond_to_web(event, {})
