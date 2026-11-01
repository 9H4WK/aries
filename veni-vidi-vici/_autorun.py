import asyncio
import sys
import time

from aries.config import load_config
from aries.strategy import AriesBot


def _log(msg: str) -> None:
    line = f"[autorun] {time.time():.3f} {msg}"
    print(line, flush=True)


async def _run_for(bot: AriesBot, seconds: float) -> None:
    task = asyncio.create_task(bot._run())
    done, pending = await asyncio.wait({task}, timeout=seconds)
    if pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        exc = task.exception()
        if exc is not None:
            _log(f"BOT EXITED EARLY WITH EXCEPTION: {exc!r}")


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    log_path = sys.argv[2] if len(sys.argv) > 2 else "logs/autorun.log"
    with open(log_path, "a", encoding="utf-8") as fh:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = fh
        try:
            _log(f"START duration={seconds}s")
            bot = AriesBot(load_config("config.yaml"))
            asyncio.run(_run_for(bot, seconds))
            _log("END graceful")
        except BaseException as exc:  # noqa: BLE001
            _log(f"FATAL {exc!r}")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
