from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import random
import time
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Callable, Optional, Tuple

from pydantic import ValidationError


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    jitter_ratio: float = 0.2
    timeout_seconds: float = 90.0
    fallback_after_attempt: int = 2


class RetryExhaustedError(RuntimeError):
    """Raised when all retry attempts fail."""


def _looks_non_retryable(exc: Exception) -> bool:
    """Best-effort filter to avoid retrying obvious credential/configuration failures."""
    text = str(exc).lower()
    non_retryable_markers = (
        "accessdenied",
        "unauthorized",
        "invalidsignature",
        "unrecognizedclient",
        "security token",
        "validationexception",
        "missing credentials",
    )
    return any(marker in text for marker in non_retryable_markers)


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (ValidationError, JSONDecodeError, TimeoutError)):
        return True
    if _looks_non_retryable(exc):
        return False
    return True


def _compute_backoff_seconds(attempt_number: int, config: RetryConfig) -> float:
    # attempt_number is 1-indexed for readability in logs/errors.
    expo = config.backoff_base_seconds * (2 ** max(0, attempt_number - 1))
    bounded = min(expo, config.backoff_max_seconds)
    jitter = bounded * config.jitter_ratio * random.random()
    return bounded + jitter


def _truncate(text: str, max_len: int = 220) -> str:
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def _run_with_timeout(callable_fn: Callable[[], Any], timeout_seconds: float) -> Any:
    """Run a callable with an upper time bound for a single attempt."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(callable_fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"attempt timed out after {timeout_seconds:.1f}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def invoke_with_retry(
    *,
    invoke_fn: Callable[[str], Any],
    prompt: str,
    validator: Optional[Callable[[Any], Any]] = None,
    fallback_prompt: Optional[str] = None,
    config: Optional[RetryConfig] = None,
    node_name: str = "unknown",
) -> Tuple[Any, int, bool]:
    """Invoke an LLM call with retries, timeout budget, and optional fallback prompt.

    Returns a tuple of:
    - validated (or raw) result
    - attempts used
    - whether fallback prompt was used
    """
    settings = config or RetryConfig()
    started_at = time.monotonic()
    current_prompt = prompt
    fallback_used = False
    last_exception: Optional[Exception] = None

    for attempt in range(1, settings.max_attempts + 1):
        elapsed = time.monotonic() - started_at
        if elapsed >= settings.timeout_seconds:
            raise TimeoutError(
                f"{node_name} exceeded timeout budget of {settings.timeout_seconds:.1f}s "
                f"before attempt {attempt}."
            )

        try:
            remaining_budget = settings.timeout_seconds - (time.monotonic() - started_at)
            if remaining_budget <= 0:
                raise TimeoutError(
                    f"{node_name} exceeded timeout budget of {settings.timeout_seconds:.1f}s "
                    f"before attempt {attempt}."
                )

            raw = _run_with_timeout(lambda: invoke_fn(current_prompt), remaining_budget)
            validated = validator(raw) if validator else raw
            return validated, attempt, fallback_used
        except Exception as exc:
            if not _is_retryable_exception(exc):
                raise

            last_exception = exc
            if attempt >= settings.max_attempts:
                break

            if (
                fallback_prompt
                and not fallback_used
                and attempt >= settings.fallback_after_attempt
            ):
                # Keep full context from the original prompt and append stricter
                # fallback instructions instead of replacing context entirely.
                current_prompt = f"{prompt}\n\n{fallback_prompt}"
                fallback_used = True

            backoff = _compute_backoff_seconds(attempt, settings)
            remaining = settings.timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                raise TimeoutError(
                    f"{node_name} exceeded timeout budget of {settings.timeout_seconds:.1f}s "
                    f"after attempt {attempt}."
                )
            time.sleep(min(backoff, max(0.0, remaining)))

    detail = _truncate(str(last_exception or "unknown failure"))
    raise RetryExhaustedError(
        f"{node_name} failed after {settings.max_attempts} attempts. Last error: {detail}"
    )
