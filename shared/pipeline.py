import logging
import time
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass
from functools import wraps

# ==========================================
# 1. Configuration & Logging Setup
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | [%(name)s] %(message)s"
)
logger = logging.getLogger("EnterprisePipeline")

# ==========================================
# 2. Custom Exception Hierarchy
# ==========================================
class PipelineError(Exception):
    """Base exception for all pipeline errors."""
    pass

class TransientError(PipelineError):
    """Errors that might resolve if retried (e.g., network timeouts, rate limits)."""
    pass

class FatalDataError(PipelineError):
    """Errors that will never resolve (e.g., corrupted data, missing schema)."""
    pass

# ==========================================
# 3. Resiliency Decorators
# ==========================================
def with_exponential_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """
    Retries the execution of a function if it raises a TransientError.
    Fails immediately on FatalDataError.
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except TransientError as e:
                    if attempt == max_retries:
                        logger.error(f"Max retries reached for {func.__name__}. Failing.")
                        raise
                    logger.warning(f"Transient failure in {func.__name__} (Attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                except FatalDataError as e:
                    logger.critical(f"Fatal error in {func.__name__}. Halting execution for this item: {e}")
                    raise
                except Exception as e:
                    logger.exception(f"Unexpected unhandled exception in {func.__name__}: {e}")
                    raise
        return wrapper
    return decorator

# ==========================================
# 4. Data Models (Strict Validation)
# ==========================================
@dataclass
class Payload:
    id: str
    raw_data: dict
    processed_data: Optional[dict] = None
    status: str = "PENDING"

# ==========================================
# 5. Core Pipeline Stages
# ==========================================
class RobustPipeline:
    def __init__(self):
        self.dead_letter_queue: List[Payload] = []
        self.successful_records: List[Payload] = []

    @with_exponential_backoff(max_retries=3)
    def extract(self, source_id: str) -> Payload:
        """Simulates extracting data from an unreliable source."""
        logger.info(f"Extracting data for source ID: {source_id}")
        # Simulated failure logic
        if source_id == "error_timeout":
            raise TransientError("Connection to source database timed out.")
        if source_id == "error_corrupted":
            raise FatalDataError("Source payload is missing required schema headers.")
            
        return Payload(id=source_id, raw_data={"val": 42, "user": "admin"})

    def transform(self, payload: Payload) -> Payload:
        """Transforms data safely."""
        logger.info(f"Transforming payload: {payload.id}")
        try:
            # Simulated complex transformation
            value = payload.raw_data.get("val")
            if not isinstance(value, (int, float)):
                raise ValueError(f"Expected numeric value, got {type(value)}")
            
            payload.processed_data = {"calculated_metric": value * 3.14}
            return payload
        except Exception as e:
            raise FatalDataError(f"Transformation failed: {e}")

    @with_exponential_backoff(max_retries=5)
    def load(self, payload: Payload) -> None:
        """Simulates loading data to a highly contested destination."""
        logger.info(f"Loading payload {payload.id} to destination.")
        if payload.id == "error_db_lock":
            raise TransientError("Destination database is currently locked.")
        payload.status = "COMPLETED"

    def handle_dead_letter(self, payload: Payload, error: Exception) -> None:
        """Routes failed records to a safe storage area for human review."""
        payload.status = "FAILED"
        logger.error(f"Routing payload {payload.id} to Dead Letter Queue. Reason: {error}")
        self.dead_letter_queue.append({
            "payload": payload,
            "error_msg": str(error),
            "timestamp": time.time()
        })

    # ==========================================
    # 6. The Execution Orchestrator
    # ==========================================
    def run_batch(self, source_ids: List[str]):
        """Runs the pipeline over a batch of IDs, ensuring one failure doesn't crash the batch."""
        logger.info(f"Starting batch run for {len(source_ids)} items.")
        
        for source_id in source_ids:
            logger.info("-" * 40)
            payload = None
            try:
                # Step 1: Extract
                payload = self.extract(source_id)
                
                # Step 2: Transform
                payload = self.transform(payload)
                
                # Step 3: Load
                self.load(payload)
                
                # Success Route
                self.successful_records.append(payload)
                logger.info(f"Successfully processed {source_id}.")
                
            except PipelineError as e:
                # Controlled Failure Route
                if payload is None:
                    payload = Payload(id=source_id, raw_data={}, status="FAILED_ON_EXTRACT")
                self.handle_dead_letter(payload, e)
            except Exception as e:
                # Catastrophic Unknown Failure Route
                logger.critical(f"UNHANDLED EXCEPTION processing {source_id}: {e}")
                if payload:
                    self.handle_dead_letter(payload, e)

        logger.info("=" * 40)
        logger.info(f"Batch Complete. Success: {len(self.successful_records)}, DLQ (Failed): {len(self.dead_letter_queue)}")

# ==========================================
# 7. Execution Entrypoint
# ==========================================
if __name__ == "__main__":
    pipeline = RobustPipeline()
    # Testing standard flow, a transient error that resolves, a transient error that exhausts retries, and a fatal error.
    test_batch = ["item_001", "error_corrupted", "item_002", "error_timeout"]
    pipeline.run_batch(test_batch)