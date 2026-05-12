from app.bot.runner import run_bot
from app.config import get_settings


def main() -> None:
    run_bot(get_settings())


if __name__ == "__main__":
    main()
