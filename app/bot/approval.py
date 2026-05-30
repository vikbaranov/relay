import logging
import threading
import time
from collections.abc import Callable

from app import metrics
from app.bot.formatting import patch_post, patch_props
from app.types import ApprovalDecision

logger = logging.getLogger(__name__)


class ApprovalManager:
    def __init__(self, get_driver: Callable, base_url: str) -> None:
        self._get_driver = get_driver
        self._base_url = base_url
        self._pending: dict[str, dict] = {}

    @property
    def _driver(self):
        return self._get_driver()

    def _build_payload(
        self,
        request_id: str,
        tool: str,
        summary: str,
        channel_id: str,
        root_id: str,
        main_post_id: str,
    ) -> dict:
        webhook_url = f"{self._base_url}/hooks/approval"

        def _action(id_: str, name: str, decision: str) -> dict:
            return {
                "id": id_,
                "name": name,
                "type": "button",
                "integration": {
                    "url": webhook_url,
                    "context": {
                        "request_id": request_id,
                        "decision": decision,
                        "main_post_id": main_post_id,
                    },
                },
            }

        return {
            "channel_id": channel_id,
            "root_id": root_id,
            "props": {
                "attachments": [
                    {
                        "text": (
                            f"**Подтверждение действия**\nИнструмент: `{tool}`\n```\n{summary}\n```"
                        ),
                        "actions": [
                            _action("approve", "✅ Разрешить один раз", "approve"),
                            _action("always", "✅ Всегда разрешать", "always"),
                            _action("deny", "❌ Отклонить", "deny"),
                        ],
                    }
                ]
            },
        }

    def request(
        self, frame: dict, channel_id: str, root_id: str, main_post_id: str
    ) -> ApprovalDecision:
        request_id = frame["request_id"]
        tool = frame["tool"]
        summary = frame.get("arguments_summary", "")
        timeout = frame.get("timeout_secs", 120)

        patch_post(self._driver, main_post_id, "_Ожидание подтверждения..._")

        approval_post = self._driver.posts.create_post(
            options=self._build_payload(
                request_id, tool, summary, channel_id, root_id, main_post_id
            )
        )
        approval_post_id = approval_post["id"]
        logger.info(
            "approval_requested request_id=%s tool=%s timeout_secs=%s",
            request_id,
            tool,
            timeout,
        )

        event = threading.Event()
        self._pending[request_id] = {
            "event": event,
            "decision": "deny",
            "approval_post_id": approval_post_id,
            "tool": tool,
            "summary": summary,
        }

        t0 = time.monotonic()
        if event.wait(timeout=timeout):
            decision = self._pending.pop(request_id, {}).get("decision", "deny")
            metrics.approvals_total.labels(decision=decision).inc()
            metrics.approval_wait_seconds.observe(time.monotonic() - t0)
        else:
            self._pending.pop(request_id, None)
            decision = "timeout"
            metrics.approvals_total.labels(decision="timeout").inc()
            logger.warning(
                "approval_timeout request_id=%s tool=%s",
                request_id,
                tool,
                extra={"channel_id": channel_id, "post_id": main_post_id},
            )
            patch_props(
                self._driver, approval_post_id, "⏱ Таймаут. Действие отклонено автоматически."
            )
        return decision

    def resolve(self, event) -> dict | None:
        """Process an approval webhook. Returns the respond_to_web body, or None if not found."""
        context = event.context or {}
        request_id = context.get("request_id")
        decision = context.get("decision")
        if decision not in ("approve", "deny", "always"):
            decision = "approve" if bool(context.get("approved", False)) else "deny"

        pending = self._pending.get(request_id) if request_id else None
        if not pending:
            return None

        tool = pending.get("tool", "?")
        summary = pending.get("summary", "")
        pending["decision"] = decision
        pending["event"].set()

        approval_post_id = pending.get("approval_post_id")
        if approval_post_id:
            try:
                self._driver.posts.delete_post(approval_post_id)
            except OSError:
                logger.error(
                    "approval_delete_failed request_id=%s post_id=%s",
                    request_id,
                    approval_post_id,
                    exc_info=True,
                )

        logger.info(
            "approval_decision request_id=%s tool=%s decision=%s user=%s",
            request_id,
            tool,
            decision,
            event.user_name,
        )

        status = {
            "approve": "✅ Разрешено один раз",
            "always": "✅ Всегда разрешено",
            "deny": "❌ Отклонено",
        }[decision]
        header = f"**Подтверждение действия**: `{tool}`"
        if summary:
            header += f"\n```\n{summary}\n```"
        return {
            "update": {
                "props": {
                    "attachments": [
                        {"text": f"{header}\n{status} пользователем @{event.user_name}"}
                    ]
                }
            }
        }
