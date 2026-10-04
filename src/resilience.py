import time
import logging
import threading
from typing import Callable, Any
from functools import wraps

logger = logging.getLogger(__name__)

class CircuitBreaker:
    """
    State machine for preventing cascading failures.
    CLOSED -> normal operation.
    OPEN -> fails fast, skips execution for a cooldown period.
    HALF_OPEN -> lets a single probe call through to test whether the service has recovered.

    Thread-safe: state changes happen under a lock, and only one caller gets the HALF_OPEN
    probe, so concurrent workers don't all hit a provider that may still be down. A probe
    that never reports back is treated as abandoned after another cooldown period.

    State lives in memory and is per process: it resets when the process restarts and is not
    shared between worker processes.
    """
    def __init__(self, name: str, failure_threshold: int = 3, cooldown_seconds: int = 120):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"
        self._probe_started_at = None
        self._lock = threading.Lock()
    
    def can_execute(self) -> bool:
        with self._lock:
            now = time.time()
            if self.state == "OPEN":
                if now - self.last_failure_time <= self.cooldown_seconds:
                    return False
                self.state = "HALF_OPEN"
                logger.info(f"Circuit Breaker [{self.name}] entered HALF_OPEN state.")
            if self.state == "HALF_OPEN":
                probe_abandoned = (self._probe_started_at is not None
                                   and now - self._probe_started_at > self.cooldown_seconds)
                if self._probe_started_at is not None and not probe_abandoned:
                    return False
                self._probe_started_at = now
            return True

    def record_success(self):
        with self._lock:
            if self.state != "CLOSED":
                logger.info(f"Circuit Breaker [{self.name}] entered CLOSED state (recovered).")
            self.failures = 0
            self.state = "CLOSED"
            self._probe_started_at = None

    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()
            self._probe_started_at = None
            if self.state == "HALF_OPEN" or self.failures >= self.failure_threshold:
                if self.state != "OPEN":
                    logger.warning(f"Circuit Breaker [{self.name}] entered OPEN state. Skipping for {self.cooldown_seconds}s.")
                self.state = "OPEN"


def with_retries(max_retries: int = 3, base_delay: float = 2.0, max_delay: float = 10.0, exceptions=(Exception,),
                 no_retry_on=()):
    """
    Decorator for exponential backoff retries.
    Exceptions matching `no_retry_on` are re-raised immediately: they cannot succeed on a
    retry (e.g. exhausted quota, missing key), so the caller should move on instead of waiting.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if no_retry_on and isinstance(e, no_retry_on):
                        raise
                    retries += 1
                    if retries > max_retries:
                        logger.error(f"Function {func.__name__} failed after {max_retries} retries: {e}")
                        raise
                    
                    delay = min(base_delay * (2 ** (retries - 1)), max_delay)
                    logger.warning(f"Function {func.__name__} failed ({e.__class__.__name__}: {e}). Retrying {retries}/{max_retries} in {delay}s...")
                    time.sleep(delay)
        return wrapper
    return decorator
