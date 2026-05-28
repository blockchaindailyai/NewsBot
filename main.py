# main.py
import time
from orchestrator import run_bot
import traceback


if __name__ == "__main__":
    print("[MAIN] Starting Blockchain Daily bot (watchdog)...")

    while True:
        try:
            # run_bot() contains the real loop and only returns if:
            #  - you type 'q/quit/exit/stop' in the console, or
            #  - it raises an unhandled exception.
            run_bot()
            print("[MAIN] run_bot() returned normally (shutdown requested). Exiting watchdog.")
            break

        except KeyboardInterrupt:
            print("[MAIN] KeyboardInterrupt received. Exiting watchdog.")
            break

        except Exception as e:
            
            print("[MAIN] Unhandled error in run_bot:")
            traceback.print_exc()

            print("[MAIN] Sleeping 10 seconds before restart...")
            time.sleep(10)
            print("[MAIN] Restarting bot...")
