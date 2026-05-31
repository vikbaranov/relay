import json
import logging
from collections.abc import Callable

from app.bot.formatting import patch_props
from app.k8s.skills import PodNotRunningError, SkillError, SkillManager
from app.k8s.user_state import UserStateManager

logger = logging.getLogger(__name__)


class DialogHandler:
    def __init__(
        self,
        get_driver: Callable,
        user_state: UserStateManager,
        skill_manager: SkillManager,
        base_url: str,
    ) -> None:
        self._get_driver = get_driver
        self._user_state = user_state
        self._skill_manager = skill_manager
        self._base_url = base_url

    @property
    def _driver(self):
        return self._get_driver()

    def open_workspace_file_dialog(self, event) -> None:
        context = event.context or {}
        filename = context.get("filename", "")
        current = context.get("current", "")
        pod_key = context.get("pod_key", "")
        root_id = context.get("root_id", "")
        state = json.dumps(
            {
                "filename": filename,
                "pod_key": pod_key,
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
        pod_key = state.get("pod_key", "")
        root_id = state.get("root_id", "")
        prompt_post_id = state.get("prompt_post_id", "")
        channel_id = body.get("channel_id", "")

        if body.get("cancelled"):
            if prompt_post_id:
                patch_props(
                    self._driver, prompt_post_id, f"❌ Редактирование `{filename}` отменено."
                )
            self._driver.respond_to_web(event, {})
            return

        content = (body.get("submission") or {}).get("content", "")
        if not content:
            self._driver.respond_to_web(
                event, {"errors": {"content": "Содержимое не может быть пустым."}}
            )
            return

        try:
            self._user_state.set_workspace_file(pod_key, filename, content)
            result = f"✅ `{filename}` сохранён. Сессия будет перезапущена автоматически."
        except Exception:
            logger.exception(
                "workspace_file_set_failed filename=%s", filename, extra={"mm_user_id": pod_key}
            )
            result = f"❌ Ошибка при сохранении `{filename}`."

        if prompt_post_id:
            patch_props(self._driver, prompt_post_id, result)
        else:
            self._driver.create_post(channel_id=channel_id, message=result, root_id=root_id)
        self._driver.respond_to_web(event, {})

    def open_env_set_dialog(self, event) -> None:
        context = event.context or {}
        key = context.get("key", "")
        pod_key = context.get("pod_key", "")
        root_id = context.get("root_id", "")
        state = json.dumps(
            {
                "key": key,
                "pod_key": pod_key,
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
        pod_key = state.get("pod_key", "")
        root_id = state.get("root_id", "")
        prompt_post_id = state.get("prompt_post_id", "")
        channel_id = body.get("channel_id", "")

        if body.get("cancelled"):
            if prompt_post_id:
                patch_props(self._driver, prompt_post_id, f"❌ Ввод `{key}` отменён.")
            self._driver.respond_to_web(event, {})
            return

        value = (body.get("submission") or {}).get("value", "")
        if not value:
            self._driver.respond_to_web(
                event, {"errors": {"value": "Значение не может быть пустым."}}
            )
            return

        try:
            self._user_state.set_user_env(pod_key, key, value)
            result = f"✅ `{key}` сохранён. Сессия будет перезапущена автоматически."
        except Exception:
            logger.exception("env_set_failed key=%s", key, extra={"mm_user_id": pod_key})
            result = f"❌ Ошибка при сохранении `{key}`."

        if prompt_post_id:
            patch_props(self._driver, prompt_post_id, result)
        else:
            self._driver.create_post(channel_id=channel_id, message=result, root_id=root_id)
        self._driver.respond_to_web(event, {})

    def open_skill_create_dialog(self, event) -> None:
        context = event.context or {}
        name = context.get("name", "")
        pod_key = context.get("pod_key", "")
        root_id = context.get("root_id", "")
        state = json.dumps(
            {
                "name": name,
                "pod_key": pod_key,
                "root_id": root_id,
                "prompt_post_id": event.post_id or "",
            }
        )
        self._driver.integration_actions.open_interactive_dialog(
            {
                "trigger_id": event.trigger_id,
                "url": f"{self._base_url}/hooks/skill_create_submit",
                "dialog": {
                    "title": f"Создать навык: {name}",
                    "submit_label": "Создать",
                    "notify_on_cancel": True,
                    "state": state,
                    "elements": [
                        {
                            "display_name": "SKILL.md",
                            "name": "content",
                            "type": "textarea",
                            "placeholder": "Вставьте содержимое SKILL.md...",
                        }
                    ],
                },
            }
        )
        self._driver.respond_to_web(event, {})

    def submit_skill_create(self, event) -> None:
        body = event.body
        state = json.loads(body.get("state") or "{}")
        name = state.get("name", "")
        pod_key = state.get("pod_key", "")
        root_id = state.get("root_id", "")
        prompt_post_id = state.get("prompt_post_id", "")
        channel_id = body.get("channel_id", "")

        if body.get("cancelled"):
            if prompt_post_id:
                patch_props(self._driver, prompt_post_id, f"❌ Создание навыка `{name}` отменено.")
            self._driver.respond_to_web(event, {})
            return

        content = (body.get("submission") or {}).get("content", "")
        if not content:
            self._driver.respond_to_web(
                event, {"errors": {"content": "Содержимое не может быть пустым."}}
            )
            return

        try:
            self._skill_manager.create_skill(pod_key, name, content)
            result = f"✅ Навык `{name}` создан."
        except PodNotRunningError:
            result = "Сессия не запущена. Запустите сессию, отправив любое сообщение."
        except SkillError:
            logger.exception("skill_create_failed name=%s", name, extra={"mm_user_id": pod_key})
            result = f"❌ Ошибка при создании навыка `{name}`."

        if prompt_post_id:
            patch_props(self._driver, prompt_post_id, result)
        else:
            self._driver.create_post(channel_id=channel_id, message=result, root_id=root_id)
        self._driver.respond_to_web(event, {})
