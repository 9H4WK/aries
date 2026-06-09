from aries.config import load_config
from aries.strategy import AriesBot


def main() -> None:
    config = load_config("config.yaml")
    bot = AriesBot(config)
    bot.run()


if __name__ == "__main__":
    main()
