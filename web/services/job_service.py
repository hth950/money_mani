"""Background job execution and tracking service."""
import asyncio
import logging
import traceback
from datetime import datetime
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.job")

# Max 3 concurrent background jobs (12GB RAM server)
_job_semaphore = asyncio.Semaphore(3)


class JobService:
    """Track and execute background jobs."""

    def create_job(self, job_name: str) -> int:
        """Create a new job_runs entry with status='running'. Returns job ID."""
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO job_runs (job_name, status) VALUES (?, 'running')",
                (job_name,),
            )
            return cursor.lastrowid

    def complete_job(self, job_id: int, summary: str = ""):
        """Mark job as success."""
        with get_db() as db:
            db.execute(
                "UPDATE job_runs SET status='success', result_summary=?, finished_at=datetime('now') WHERE id=?",
                (summary, job_id),
            )

    def fail_job(self, job_id: int, error: str):
        """Mark job as failed."""
        with get_db() as db:
            db.execute(
                "UPDATE job_runs SET status='failed', error_message=?, finished_at=datetime('now') WHERE id=?",
                (error, job_id),
            )

    def get_job(self, job_id: int) -> dict | None:
        """Get job status."""
        with get_db() as db:
            row = db.execute("SELECT * FROM job_runs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        """List recent jobs."""
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def run_background(self, job_name: str, func, *args, **kwargs) -> int:
        """Run a function in a background thread with job tracking.

        Returns job_id immediately. The function runs in asyncio.to_thread().
        """
        job_id = self.create_job(job_name)

        async def _run():
            async with _job_semaphore:
                try:
                    logger.info(f"Job {job_name} (#{job_id}) started (waiting jobs queued)")
                    result = await asyncio.to_thread(func, *args, **kwargs)
                    summary = str(result)[:500] if result else "Completed"
                    self.complete_job(job_id, summary)
                except Exception as e:
                    logger.error(f"Job {job_name} (#{job_id}) failed: {e}")
                    self.fail_job(job_id, f"{type(e).__name__}: {str(e)}")

        asyncio.create_task(_run())
        return job_id
