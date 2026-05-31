import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from mmpy_bot.wrappers import Message

from app.k8s.skills import SKILL_NAME_RE, PodNotRunningError, SkillError, SkillManager
from app.k8s.user_state import UserStateManager

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    stop_event: threading.Event | None = None
    generation: int = 0


class SessionCommandHandler:
    def __init__(self, get_driver: Callable, sessions: dict[str, _SessionState]) -> None:
        self._get_driver = get_driver
        self._sessions = sessions

    @property
    def _driver(self):
        return self._get_driver()

    def handle(self, message: Message, scope: str, root_id: str) -> bool:
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

        return False


class EnvCommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        base_url: str,
        user_state: UserStateManager,
    ) -> None:
        self._get_driver = get_driver
        self._base_url = base_url
        self._user_state = user_state

    @property
    def _driver(self):
        return self._get_driver()

    def handle(self, message: Message, root_id: str, runtime_key: str | None = None) -> None:
        pod_key = runtime_key or message.user_id
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
                                                "pod_key": pod_key,
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
                keys = self._user_state.list_user_envs(pod_key)
                reply = (
                    ("Переменные окружения:\n" + "\n".join(f"- `{k}`" for k in keys))
                    if keys
                    else "Переменные не заданы."
                )
            except Exception:
                logger.exception("env_list_failed", extra={"mm_user_id": pod_key})
                reply = "Ошибка при получении списка переменных."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "del" and len(parts) == 3:
            key = parts[2]
            try:
                found = self._user_state.delete_user_env(pod_key, key)
                reply = (
                    f"✅ `{key}` удалён. Сессия будет перезапущена."
                    if found
                    else f"`{key}` не найден."
                )
            except Exception:
                logger.exception("env_del_failed key=%s", key, extra={"mm_user_id": pod_key})
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


class ModelCommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        user_state: UserStateManager,
        allowed_models: list[str],
    ) -> None:
        self._get_driver = get_driver
        self._user_state = user_state
        self._allowed_models = allowed_models

    @property
    def _driver(self):
        return self._get_driver()

    def handle(self, message: Message, root_id: str, runtime_key: str | None = None) -> None:
        pod_key = runtime_key or message.user_id
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        allowed = self._allowed_models

        if sub == "show":
            try:
                model = self._user_state.get_user_model(pod_key)
                reply = f"Текущая модель: `{model}`"
            except Exception:
                logger.exception("model_show_failed", extra={"mm_user_id": pod_key})
                reply = "Ошибка при получении текущей модели."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "list":
            try:
                current = self._user_state.get_user_model(pod_key)
                lines = [
                    f"- `{model}`" + (" — текущая" if model == current else "") for model in allowed
                ]
                reply = "Доступные модели:\n" + "\n".join(lines)
            except Exception:
                logger.exception("model_list_failed", extra={"mm_user_id": pod_key})
                reply = "Ошибка при получении списка моделей."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "set" and len(parts) == 3:
            model = parts[2]
            try:
                saved = self._user_state.set_user_model(pod_key, model)
                if saved:
                    reply = f"✅ Модель `{model}` сохранена. Сессия будет перезапущена."
                else:
                    reply = f"Модель `{model}` недоступна. Доступные модели: " + ", ".join(
                        f"`{m}`" for m in allowed
                    )
            except Exception:
                logger.exception("model_set_failed model=%s", model, extra={"mm_user_id": pod_key})
                reply = "Ошибка при сохранении модели."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "reset":
            try:
                changed = self._user_state.reset_user_model(pod_key)
                reply = (
                    f"✅ Модель сброшена к `{allowed[0]}`. Сессия будет перезапущена."
                    if changed
                    else f"Модель уже использует значение по умолчанию: `{allowed[0]}`."
                )
            except Exception:
                logger.exception("model_reset_failed", extra={"mm_user_id": pod_key})
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


class WorkspaceFileCommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        base_url: str,
        user_state: UserStateManager,
    ) -> None:
        self._get_driver = get_driver
        self._base_url = base_url
        self._user_state = user_state

    @property
    def _driver(self):
        return self._get_driver()

    def handle(
        self, message: Message, root_id: str, filename: str, runtime_key: str | None = None
    ) -> None:
        pod_key = runtime_key or message.user_id
        parts = message.text.strip().split(maxsplit=1)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        cmd = filename.replace(".md", "").lower()

        if sub == "show":
            content = self._user_state.get_workspace_file(pod_key, filename)
            reply = (
                f"`{filename}` не переопределён — используется глобальный файл по умолчанию."
                if content is None
                else f"**{filename}** (пользовательская версия):\n```\n{content}\n```"
            )
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "set":
            current = self._user_state.get_workspace_file(pod_key, filename) or ""
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
                                            "url": f"{self._base_url}/hooks/workspace_file_dialog",
                                            "context": {
                                                "filename": filename,
                                                "current": current,
                                                "pod_key": pod_key,
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
                found = self._user_state.reset_workspace_file(pod_key, filename)
                reply = (
                    f"✅ `{filename}` был сброшен. Сессия будет перезапущена."
                    if found
                    else f"`{filename}` не был переопределён."
                )
            except Exception:
                logger.exception(
                    "workspace_file_reset_failed filename=%s",
                    filename,
                    extra={"mm_user_id": pod_key},
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


class SkillCommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        base_url: str,
        skill_manager: SkillManager,
    ) -> None:
        self._get_driver = get_driver
        self._base_url = base_url
        self._skill_manager = skill_manager

    @property
    def _driver(self):
        return self._get_driver()

    def _reject_if_invalid_name(self, name: str, channel_id: str, root_id: str) -> bool:
        if SKILL_NAME_RE.match(name):
            return False
        self._driver.create_post(
            channel_id=channel_id,
            message=(
                f"Некорректное имя навыка: `{name}`. Используйте строчные буквы, цифры и дефисы."
            ),
            root_id=root_id,
        )
        return True

    def handle(self, message: Message, root_id: str, runtime_key: str | None = None) -> None:
        pod_key = runtime_key or message.user_id
        parts = message.text.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "list":
            try:
                output = self._skill_manager.list_skills(pod_key)
                reply = output if output else "Навыков не установлено."
            except PodNotRunningError:
                reply = "Сессия не запущена. Запустите сессию, отправив любое сообщение."
            except SkillError:
                logger.exception("skill_list_failed", extra={"mm_user_id": pod_key})
                reply = "Ошибка при получении списка навыков."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "show" and len(parts) >= 3:
            name = parts[2].strip()
            if self._reject_if_invalid_name(name, message.channel_id, root_id):
                return
            try:
                content = self._skill_manager.show_skill(pod_key, name)
                reply = (
                    f"**{name}** (SKILL.md):\n```\n{content}\n```"
                    if content is not None
                    else f"`{name}` не найден."
                )
            except PodNotRunningError:
                reply = "Сессия не запущена. Запустите сессию, отправив любое сообщение."
            except SkillError:
                logger.exception("skill_show_failed name=%s", name, extra={"mm_user_id": pod_key})
                reply = "Ошибка при получении навыка."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        if sub == "create" and len(parts) >= 3:
            name = parts[2].strip()
            if self._reject_if_invalid_name(name, message.channel_id, root_id):
                return
            self._driver.posts.create_post(
                options={
                    "channel_id": message.channel_id,
                    "root_id": root_id,
                    "props": {
                        "attachments": [
                            {
                                "text": f"Нажмите кнопку для создания навыка `{name}`:",
                                "actions": [
                                    {
                                        "id": "trigger",
                                        "name": "✏️ Создать навык",
                                        "type": "button",
                                        "integration": {
                                            "url": f"{self._base_url}/hooks/skill_create_dialog",
                                            "context": {
                                                "name": name,
                                                "pod_key": pod_key,
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

        if sub == "remove" and len(parts) >= 3:
            name = parts[2].strip()
            if self._reject_if_invalid_name(name, message.channel_id, root_id):
                return
            try:
                found = self._skill_manager.remove_skill(pod_key, name)
                reply = f"✅ Навык `{name}` удалён." if found else f"`{name}` не найден."
            except PodNotRunningError:
                reply = "Сессия не запущена. Запустите сессию, отправив любое сообщение."
            except SkillError:
                logger.exception("skill_remove_failed name=%s", name, extra={"mm_user_id": pod_key})
                reply = "Ошибка при удалении навыка."
            self._driver.create_post(channel_id=message.channel_id, message=reply, root_id=root_id)
            return

        self._driver.create_post(
            channel_id=message.channel_id,
            message=(
                "Использование:\n"
                "- `!skill list` — список установленных навыков\n"
                "- `!skill show <name>` — показать содержимое навыка\n"
                "- `!skill create <name>` — создать новый навык\n"
                "- `!skill remove <name>` — удалить навык"
            ),
            root_id=root_id,
        )


class CommandHandler:
    def __init__(
        self,
        get_driver: Callable,
        session: SessionCommandHandler,
        env: EnvCommandHandler,
        model: ModelCommandHandler,
        workspace: WorkspaceFileCommandHandler,
        skill: SkillCommandHandler,
    ) -> None:
        self._get_driver = get_driver
        self._session = session
        self._env = env
        self._model = model
        self._workspace = workspace
        self._skill = skill

    @property
    def _driver(self):
        return self._get_driver()

    def handle(
        self, message: Message, scope: str, root_id: str, runtime_key: str | None = None
    ) -> bool:
        command = message.text.strip().split(maxsplit=1)[0].lower()

        if self._session.handle(message, scope, root_id):
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
                    "**Навыки**\n"
                    "- `!skill list` — список установленных навыков\n"
                    "- `!skill show <name>` — показать содержимое навыка\n"
                    "- `!skill create <name>` — создать новый навык\n"
                    "- `!skill remove <name>` — удалить навык\n"
                    "\n"
                    "- `!help` — показать эту справку"
                ),
                root_id=root_id,
            )
            return True

        if command == "!env":
            self._env.handle(message, root_id, runtime_key)
            return True

        if command == "!model":
            self._model.handle(message, root_id, runtime_key)
            return True

        if command == "!soul":
            self._workspace.handle(message, root_id, "SOUL.md", runtime_key)
            return True

        if command == "!identity":
            self._workspace.handle(message, root_id, "IDENTITY.md", runtime_key)
            return True

        if command == "!skill":
            self._skill.handle(message, root_id, runtime_key)
            return True

        return False
