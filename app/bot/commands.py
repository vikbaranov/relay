import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from mmpy_bot.wrappers import Message

from app.k8s.lifecycle import LifecycleManager
from app.k8s.user_state import UserStateManager

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    stop_event: threading.Event | None = None
    generation: int = 0


class CommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        lifecycle: LifecycleManager,
        user_state: UserStateManager,
        base_url: str,
        allowed_models: list[str],
        sessions: dict[str, _SessionState],
    ) -> None:
        self._get_driver = get_driver
        self._lifecycle = lifecycle
        self._user_state = user_state
        self._base_url = base_url
        self._allowed_models = allowed_models
        self._sessions = sessions

    @property
    def _driver(self):
        return self._get_driver()

    def handle(
        self, message: Message, scope: str, root_id: str, runtime_key: str | None = None
    ) -> bool:
        command = message.text.strip().split(maxsplit=1)[0].lower()

        if command in ("!new", "!clear"):
            self._sessions.setdefault(scope, _SessionState()).generation += 1
            msg = (
                "Контекст очищен."
                if command == "!clear"
                else "Новый контекст начат для текущей ветки."
            )
            self._driver.create_post(channel_id=message.channel_id, message=msg, root_id=root_id)
            return True

        if command == "!help":
            self._driver.create_post(
                channel_id=message.channel_id,
                message=(
                    "**Доступные команды:**\n"
                    "\n"
                    "**Контекст**\n"
                    "- `!new` — начать новый контекст разговора\n"
                    "- `!clear` — очистить контекст (аналог `!new`)\n"
                    "- `!stop` — остановить текущее выполнение\n"
                    "\n"
                    "**Переменные**\n"
                    "- `!env set KEY` — сохранить переменную окружения\n"
                    "- `!env list` — список переменных окружения\n"
                    "- `!env del KEY` — удалить переменную окружения\n"
                    "\n"
                    "**Модель**\n"
                    "- `!model list` — список доступных моделей\n"
                    "- `!model show` — показать текущую модель\n"
                    "- `!model set MODEL` — выбрать модель\n"
                    "- `!model reset` — сбросить модель к умолчанию\n"
                    "\n"
                    "**Soul / Identity**\n"
                    "- `!soul show` — показать текущий SOUL.md\n"
                    "- `!soul set` — изменить SOUL.md\n"
                    "- `!soul reset` — сбросить SOUL.md к умолчанию\n"
                    "- `!identity show` — показать текущий IDENTITY.md\n"
                    "- `!identity set` — изменить IDENTITY.md\n"
                    "- `!identity reset` — сбросить IDENTITY.md к умолчанию\n"
                    "\n"
                    "- `!help` — показать эту справку"
                ),
                root_id=root_id,
            )
            return True

        if command == "!stop":
            state = self._sessions.get(scope)
            if state and state.stop_event:
                state.stop_event.set()
                self._driver.create_post(
                    channel_id=message.channel_id,
                    message="Выполнение остановлено.",
                    root_id=root_id,
                )
            else:
                self._driver.create_post(
                    channel_id=message.channel_id,
                    message="Нет активного выполнения.",
                    root_id=root_id,
                )
            return True

        if command == "!env":
            self._handle_env(message, root_id, runtime_key)
            return True

        if command == "!model":
            self._handle_model(message, root_id, runtime_key)
            return True

        if command == "!soul":
            self._handle_workspace_file(message, root_id, "SOUL.md", runtime_key)
            return True

        if command == "!identity":
            self._handle_workspace_file(message, root_id, "IDENTITY.md", runtime_key)
            return True

        return False

    def _restart_channel_runtime(
        self, runtime_key: str | None, user_id: str, *, did_change: bool
    ) -> None:
        if not (did_change and runtime_key and runtime_key != user_id):
            return
        try:
            self._lifecycle.restart_if_running(runtime_key, model_user_id=user_id)
        except Exception:
            logger.exception(
                "channel_runtime_restart_failed runtime_key=%s",
                runtime_key,
                extra={"mm_user_id": user_id},
            )

    def _handle_env(self, message: Message, root_id: str, runtime_key: str | None = None) -> None:
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "set" and len(parts) == 3:
            key = parts[2]
            if not key.isidentifier():
                self._driver.create_post(
                    channel_id=message.channel_id,
                    message=f"Некорректное имя переменной: `{key}`",
                    root_id=root_id,
                )
                return
            self._driver.posts.create_post(
                options={
                    "channel_id": message.channel_id,
                    "root_id": root_id,
                    "props": {
                        "attachments": [
                            {
                                "text": f"Нажмите кнопку для ввода значения `{key}`:",
                                "actions": [
                                    {
                                        "id": "trigger",
                                        "name": "🔒 Ввести значение",
                                        "type": "button",
                                        "integration": {
                                            "url": f"{self._base_url}/hooks/env_set_dialog",
                                            "context": {
                                                "key": key,
                                                "user_id": message.user_id,
                                                "root_id": root_id,
                                            },
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            )
            return

        if sub == "list":
            try:
                keys = self._user_state.list_user_envs(message.user_id)
                reply = (
                    ("Переменные окружения:\n" + "\n".join(f"- `{k}`" for k in keys))
                    if keys
                    else "Переменные не заданы."
                )
            except Exception:
                logger.exception("env_list_failed", extra={"mm_user_id": message.user_id})
                reply = "Ошибка при получении списка переменных."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "del" and len(parts) == 3:
            key = parts[2]
            try:
                found = self._user_state.delete_user_env(message.user_id, key)
                self._restart_channel_runtime(runtime_key, message.user_id, did_change=found)
                reply = (
                    f"✅ `{key}` удалён. Сессия будет перезапущена."
                    if found
                    else f"`{key}` не найден."
                )
            except Exception:
                logger.exception(
                    "env_del_failed key=%s", key, extra={"mm_user_id": message.user_id}
                )
                reply = "Ошибка при удалении переменной."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        self._driver.create_post(
            channel_id=message.channel_id,
            message=(
                "Использование:\n"
                "- `!env set KEY` — сохранить переменную через защищённый диалог\n"
                "- `!env list` — список переменных\n"
                "- `!env del KEY` — удалить переменную"
            ),
            root_id=root_id,
        )

    def _handle_model(self, message: Message, root_id: str, runtime_key: str | None = None) -> None:
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        allowed = self._allowed_models

        if sub == "show":
            try:
                model = self._user_state.get_user_model(message.user_id)
                reply = f"Текущая модель: `{model}`"
            except Exception:
                logger.exception("model_show_failed", extra={"mm_user_id": message.user_id})
                reply = "Ошибка при получении текущей модели."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "list":
            try:
                current = self._user_state.get_user_model(message.user_id)
                lines = [
                    f"- `{model}`" + (" — текущая" if model == current else "") for model in allowed
                ]
                reply = "Доступные модели:\n" + "\n".join(lines)
            except Exception:
                logger.exception("model_list_failed", extra={"mm_user_id": message.user_id})
                reply = "Ошибка при получении списка моделей."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "set" and len(parts) == 3:
            model = parts[2]
            try:
                saved = self._user_state.set_user_model(message.user_id, model)
                self._restart_channel_runtime(runtime_key, message.user_id, did_change=saved)
                if saved:
                    reply = f"✅ Модель `{model}` сохранена. Сессия будет перезапущена."
                else:
                    reply = f"Модель `{model}` недоступна. Доступные модели: " + ", ".join(
                        f"`{m}`" for m in allowed
                    )
            except Exception:
                logger.exception(
                    "model_set_failed model=%s", model, extra={"mm_user_id": message.user_id}
                )
                reply = "Ошибка при сохранении модели."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "reset":
            try:
                changed = self._user_state.reset_user_model(message.user_id)
                self._restart_channel_runtime(runtime_key, message.user_id, did_change=changed)
                reply = (
                    f"✅ Модель сброшена к `{allowed[0]}`. Сессия будет перезапущена."
                    if changed
                    else f"Модель уже использует значение по умолчанию: `{allowed[0]}`."
                )
            except Exception:
                logger.exception("model_reset_failed", extra={"mm_user_id": message.user_id})
                reply = "Ошибка при сбросе модели."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        self._driver.create_post(
            channel_id=message.channel_id,
            message=(
                "Использование:\n"
                "- `!model list` — список доступных моделей\n"
                "- `!model show` — показать текущую модель\n"
                "- `!model set MODEL` — выбрать модель\n"
                "- `!model reset` — сбросить модель к умолчанию"
            ),
            root_id=root_id,
        )

    def _handle_workspace_file(
        self, message: Message, root_id: str, filename: str, runtime_key: str | None = None
    ) -> None:
        parts = message.text.strip().split(maxsplit=1)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        cmd = filename.replace(".md", "").lower()

        if sub == "show":
            content = self._user_state.get_workspace_file(message.user_id, filename)
            if content is None:
                reply = (
                    f"`{filename}` не переопределён — используется глобальный файл по умолчанию."
                )
            else:
                reply = f"**{filename}** (пользовательская версия):\n```\n{content}\n```"
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "set":
            current = self._user_state.get_workspace_file(message.user_id, filename) or ""
            self._driver.posts.create_post(
                options={
                    "channel_id": message.channel_id,
                    "root_id": root_id,
                    "props": {
                        "attachments": [
                            {
                                "text": f"Нажмите кнопку для редактирования `{filename}`:",
                                "actions": [
                                    {
                                        "id": "trigger",
                                        "name": f"✏️ Редактировать {filename}",
                                        "type": "button",
                                        "integration": {
                                            "url": (
                                                f"{self._base_url}/hooks/workspace_file_dialog"
                                            ),
                                            "context": {
                                                "filename": filename,
                                                "current": current,
                                                "user_id": message.user_id,
                                                "root_id": root_id,
                                            },
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
            return

        if sub == "reset":
            try:
                found = self._user_state.reset_workspace_file(message.user_id, filename)
                self._restart_channel_runtime(runtime_key, message.user_id, did_change=found)
                reply = (
                    f"✅ `{filename}` был сброшен. Сессия будет перезапущена."
                    if found
                    else f"`{filename}` не был переопределён."
                )
            except Exception:
                logger.exception(
                    "workspace_file_reset_failed filename=%s",
                    filename,
                    extra={"mm_user_id": message.user_id},
                )
                reply = f"Ошибка при сбросе `{filename}`."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        self._driver.create_post(
            channel_id=message.channel_id,
            message=(
                f"Использование:\n"
                f"- `!{cmd} show` — показать текущее содержимое\n"
                f"- `!{cmd} set` — открыть редактор\n"
                f"- `!{cmd} reset` — сбросить к глобальному умолчанию"
            ),
            root_id=root_id,
        )
