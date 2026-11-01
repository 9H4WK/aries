import sys

from aries.config import load_config
from aries.strategy import AriesBot


def main() -> None:
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    bot = AriesBot(config)
    bot.run()


if __name__ == "__main__":
    main()
