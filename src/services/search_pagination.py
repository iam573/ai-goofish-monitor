import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.utils import log_time, random_sleep

NEXT_PAGE_SELECTOR = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
SEARCH_RESULTS_API_MARKER = "idlemtopsearch.pc.search"
SEARCH_RESULTS_SHADE_API_MARKER = "idlemtopsearch.pc.search.shade"
SEARCH_RESULTS_METHODS = {"GET", "POST"}
INITIAL_SEARCH_REQUEST_TIMEOUT_MS = 30_000
INITIAL_SEARCH_RETRY_COUNT = 2
INITIAL_SEARCH_RETRY_DELAY_SECONDS = 5
PAGE_REQUEST_TIMEOUT_MS = 20_000
PAGE_CLICK_TIMEOUT_MS = 10_000
PAGE_RETRY_DELAY_SECONDS = 5
PAGE_RETRY_COUNT = 2
PAGE_CLICK_SLEEP_MIN_SECONDS = 2
PAGE_CLICK_SLEEP_MAX_SECONDS = 5


@dataclass(frozen=True)
class PageAdvanceResult:
    advanced: bool
    response: Optional[Any] = None
    stop_reason: Optional[str] = None


def is_search_results_response(
    response: Any,
    api_url_marker: str = SEARCH_RESULTS_API_MARKER,
    shade_api_url_marker: str = SEARCH_RESULTS_SHADE_API_MARKER,
) -> bool:
    request = getattr(response, "request", None)
    request_method = getattr(request, "method", None)
    response_url = getattr(response, "url", "").lower()
    normalized_method = str(request_method or "").upper()
    return (
        api_url_marker in response_url
        and shade_api_url_marker not in response_url
        and normalized_method in SEARCH_RESULTS_METHODS
    )


async def _cancel_response_wait(response_task: asyncio.Task) -> None:
    if response_task.done():
        try:
            await response_task
        except (asyncio.CancelledError, PlaywrightTimeoutError):
            pass
        except Exception:
            pass
        return

    response_task.cancel()
    try:
        await response_task
    except (asyncio.CancelledError, PlaywrightTimeoutError):
        pass
    except Exception:
        pass


async def wait_for_search_response_after_action(
    *,
    page: Any,
    action: Callable[[], Awaitable[None]],
    logger: Callable[[str], None] = log_time,
    retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout: int = INITIAL_SEARCH_REQUEST_TIMEOUT_MS,
    max_retries: int = INITIAL_SEARCH_RETRY_COUNT,
    retry_delay_seconds: float = INITIAL_SEARCH_RETRY_DELAY_SECONDS,
    should_abort_wait: Optional[Callable[[], bool]] = None,
) -> Optional[Any]:
    for retry_index in range(max_retries):
        response_task = asyncio.create_task(
            page.wait_for_event(
                "response",
                predicate=is_search_results_response,
                timeout=timeout,
            )
        )
        try:
            await action()
        except Exception:
            await _cancel_response_wait(response_task)
            raise

        if should_abort_wait is not None and should_abort_wait():
            await _cancel_response_wait(response_task)
            return None

        try:
            return await response_task
        except PlaywrightTimeoutError:
            if retry_index < max_retries - 1:
                logger(
                    "等待初始搜索接口响应超时，"
                    f"{retry_delay_seconds:g}秒后重试导航..."
                )
                await retry_sleep(retry_delay_seconds)
                continue

            logger("等待初始搜索接口响应超时，继续检查页面是否已加载。")
            return None

    return None


async def advance_search_page(
    *,
    page: Any,
    page_num: int,
    logger: Callable[[str], None] = log_time,
    wait_after_click: Callable[[float, float], Awaitable[None]] = random_sleep,
    retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_retries: int = PAGE_RETRY_COUNT,
) -> PageAdvanceResult:
    next_button = page.locator(NEXT_PAGE_SELECTOR).first
    if not await next_button.count():
        logger("已到达最后一页，未找到可用的'下一页'按钮，停止翻页。")
        return PageAdvanceResult(advanced=False, stop_reason="no_next_button")

    for retry_index in range(max_retries):
        try:
            await next_button.scroll_into_view_if_needed()
            async with page.expect_response(
                is_search_results_response,
                timeout=PAGE_REQUEST_TIMEOUT_MS,
            ) as response_info:
                try:
                    await next_button.click(timeout=PAGE_CLICK_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    logger(f"第 {page_num} 页下一页按钮点击超时，停止翻页。")
                    return PageAdvanceResult(
                        advanced=False,
                        stop_reason="click_timeout",
                    )
            await wait_after_click(
                PAGE_CLICK_SLEEP_MIN_SECONDS,
                PAGE_CLICK_SLEEP_MAX_SECONDS,
            )
            return PageAdvanceResult(
                advanced=True,
                response=await response_info.value,
            )
        except PlaywrightTimeoutError:
            if retry_index < max_retries - 1:
                logger(
                    f"等待第 {page_num} 页搜索响应超时，"
                    f"{PAGE_RETRY_DELAY_SECONDS}秒后重试..."
                )
                await retry_sleep(PAGE_RETRY_DELAY_SECONDS)
                continue

            logger(f"等待第 {page_num} 页搜索响应超时 {max_retries} 次，停止翻页。")
            return PageAdvanceResult(advanced=False, stop_reason="response_timeout")

    return PageAdvanceResult(advanced=False, stop_reason="unknown")
