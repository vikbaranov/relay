import json
import logging
import traceback


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = traceback.format_exception(*record.exc_info)
        for key in ("runtime_key", "mm_user_id_hash", "post_id", "channel_id", "session_id"):
            if (val := record.__dict__.get(key)) is not None:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.root.setLevel(level)
    logging.root.handlers = [handler]
