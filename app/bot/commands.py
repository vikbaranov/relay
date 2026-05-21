import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    stop_event: threading.Event | None = None
    generation: int = 0


class CommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        runtime,
        base_url: str,
        sessions: dict,
    ) -> None:
        self._get_driver = get_driver
        self._runtime = runtime
        self._base_url = base_url
        self._sessions = sessions

    @property
    def _driver(self):
        return self._get_driver()

    def handle(self, message, scope: str, root_id: str) -> bool:
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
                    "- `!new` — начать новый контекст разговора\n"
                    "- `!clear` — очистить контекст (аналог `!new`)\n"
                    "- `!stop` — остановить текущее выполнение\n"
                    "- `!env set KEY` — сохранить переменную окружения\n"
                    "- `!env list` — список переменных окружения\n"
                    "- `!env del KEY` — удалить переменную окружения\n"
                    "- `!soul show` — показать текущий SOUL.md\n"
                    "- `!soul set` — изменить SOUL.md\n"
                    "- `!soul reset` — сбросить SOUL.md к умолчанию\n"
                    "- `!identity show` — показать текущий IDENTITY.md\n"
                    "- `!identity set` — изменить IDENTITY.md\n"
                    "- `!identity reset` — сбросить IDENTITY.md к умолчанию\n"
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
            self._handle_env(message, root_id)
            return True

        if command == "!soul":
            self._handle_workspace_file(message, root_id, "SOUL.md")
            return True

        if command == "!identity":
            self._handle_workspace_file(message, root_id, "IDENTITY.md")
            return True

        return False

    def _handle_env(self, message, root_id: str) -> None:
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
                keys = self._runtime.list_user_envs(message.user_id)
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
                found = self._runtime.delete_user_env(message.user_id, key)
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

    def _handle_workspace_file(self, message, root_id: str, filename: str) -> None:
        parts = message.text.strip().split(maxsplit=1)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        cmd = filename.replace(".md", "").lower()

        if sub == "show":
            content = self._runtime.get_workspace_file(message.user_id, filename)
            if content is None:
                reply = (
                    f"`{filename}` не переопределён — используется глобальный файл по умолчанию."
                )
            else:
                reply = f"**{filename}** (пользовательская версия):\n```\n{content}\n```"
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "set":
            current = self._runtime.get_workspace_file(message.user_id, filename) or ""
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
                found = self._runtime.reset_workspace_file(message.user_id, filename)
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
