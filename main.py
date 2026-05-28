import logging
import time
import traceback

from config import LOG_LEVEL
from orchestrator import run_bot

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger("main")


if __name__ == "__main__":
    logger.info("Starting Blockchain Daily bot (watchdog)")

    while True:
        try:
            run_bot()
            logger.info("run_bot() returned normally (shutdown requested). Exiting watchdog.")
            break
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received. Exiting watchdog.")
            break
        except RuntimeError:
            logger.exception("Runtime error in run_bot; restarting in 10 seconds")
            time.sleep(10)
        except Exception:
            logger.error("Unhandled error in run_bot")
            traceback.print_exc()
            logger.info("Sleeping 10 seconds before restart")
            time.sleep(10)
