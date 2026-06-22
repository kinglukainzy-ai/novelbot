"""
scheduler.py - runs the brain's check cycle on a timer in the background.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("scheduler")


def start_scheduler(brain, interval_minutes: int):
    scheduler = BackgroundScheduler()

    def job():
        logger.info("Running scheduled check cycle...")
        try:
            results = brain.run_check_cycle()
            logger.info(f"Check cycle complete, {len(results)} update(s) found.")
        except Exception as e:
            logger.error(f"Check cycle failed: {e}")

    scheduler.add_job(job, "interval", minutes=interval_minutes)
    scheduler.start()
    return scheduler
