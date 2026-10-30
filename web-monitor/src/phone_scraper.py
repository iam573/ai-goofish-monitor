"""
iPhone App 搜索执行器
用已登录 iPhone 上的闲鱼 App 代替网页搜索，避开网页端频繁出现的滑块验证；
商品去重、AI/关键词分析、结果落库与通知沿用网页任务的同一套链路。
"""
import asyncio
import os
import threading
from collections import Counter
from datetime import datetime
from typing import Optional

from src.ai_handler import (
    cleanup_task_images,
    download_all_images,
    get_ai_analysis,
    send_ntfy_notification,
)
from src.config import IMAGE_SAVE_DIR, SKIP_AI_ANALYSIS, TASK_IMAGE_DIR_PREFIX
from src.infrastructure.external.iphone.device_lock import any_iphone_device_lock
from src.infrastructure.external.iphone.devices import candidate_devices, discover_devices
from src.infrastructure.external.iphone.page_source import PhoneCard, normalize_search_text
from src.infrastructure.external.iphone.proxy_search_client import IPhoneProxySearchClient
from src.infrastructure.external.iphone.search_client import IPhoneListing, IPhoneSearchClient
from src.infrastructure.external.iphone.settings import IPhoneSettings
from src.infrastructure.persistence.iphone_device_selection import (
    iphone_device_channel,
    load_selected_iphone_udids,
)
from src.keyword_rule_engine import build_search_text, match_keywords
from src.scraper import (
    FAILURE_GUARD,
    _get_ai_analysis_concurrency,
    _normalize_price_bound,
    _notify_task_failure,
    _should_analyze_images,
)
from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher, ItemAnalysisJob
from src.services.price_history_service import (
    build_market_reference,
    load_price_snapshots,
    record_market_snapshots,
)
from src.services.result_storage_service import (
    load_processed_link_keys,
    load_processed_title_prices,
)
from src.utils import get_link_unique_key, log_time, save_to_jsonl

SEARCH_SOURCE_IPHONE = "iphone"
FINGERPRINT_TITLE_LENGTH = 30


def item_link(item_id: str) -> str:
    return f"https://www.goofish.com/item?id={item_id}"


def card_fingerprint(title: str, price: float) -> str:
    return f"{normalize_search_text(title)[:FINGERPRINT_TITLE_LENGTH]}|{float(price):g}"


def card_rejection_reason(
    card: PhoneCard,
    *,
    min_price=None,
    max_price=None,
    decision_mode: str = "ai",
    keyword_rules=(),
    exclude_keyword_rules=(),
    known_fingerprints: frozenset[str] | set[str] = frozenset(),
) -> Optional[str]:
    """列表卡片是否值得进入详情；返回拒绝原因，None 表示是候选。"""
    min_bound = _normalize_price_bound(min_price)
    max_bound = _normalize_price_bound(max_price)
    if min_bound is not None and card.price < min_bound:
        return "价格低于下限"
    if max_bound is not None and card.price > max_bound:
        return "价格高于上限"
    search_text = build_search_text({"商品信息": {"商品标题": card.title}, "卖家信息": {}})
    if exclude_keyword_rules and match_keywords(tuple(exclude_keyword_rules), search_text):
        return "命中忽略关键词"
    if decision_mode == "keyword" and keyword_rules and not match_keywords(tuple(keyword_rules), search_text):
        return "未命中关键词"
    if card_fingerprint(card.title, card.price) in known_fingerprints:
        return "已处理过"
    return None


def build_item_data(listing: IPhoneListing, device_name: str) -> dict:
    card = listing.card
    item_id = listing.item_id
    item = {
        "商品标题": card.title,
        "当前售价": f"¥{card.price:g}",
        "商品原价": "暂无",
        "“想要”人数": "NaN",
        "商品标签": [],
        "发货地区": card.region or "地区未知",
        "卖家昵称": listing.seller_name or "未识别",
        "商品链接": item_link(item_id),
        "发布时间": listing.publish_time or "未知时间",
        "商品ID": item_id,
        "采集来源": f"iPhone App（{device_name}{' · 代理' if listing.channel == 'proxy' else ''}）",
    }
    if listing.image_urls:
        item["商品图片列表"] = list(listing.image_urls)
        item["商品主图链接"] = listing.image_urls[0]
    if listing.description:
        item["商品描述"] = listing.description
    if listing.parameters:
        item["商品参数"] = listing.parameters
    return item


def _save_screenshot(task_name: str, item_id: str, png: bytes) -> Optional[str]:
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    os.makedirs(task_image_dir, exist_ok=True)
    path = os.path.join(task_image_dir, f"iphone_{item_id}.png")
    try:
        with open(path, "wb") as handle:
            handle.write(png)
        return path
    except OSError as exc:
        print(f"   [图片] 保存 iPhone 详情截图失败: {exc}")
        return None


async def _no_seller_profile(_seller_id: str) -> dict:
    return {}


def _log_ignored_filters(task_config: dict) -> None:
    ignored = []
    if task_config.get("personal_only"):
        ignored.append("仅个人闲置")
    if task_config.get("free_shipping"):
        ignored.append("包邮")
    new_publish = str(task_config.get("new_publish_option") or "").strip()
    if new_publish and new_publish != "__none__":
        ignored.append(f"新发布范围({new_publish})")
    region = str(task_config.get("region") or "").strip()
    if region:
        ignored.append(f"区域({region})")
    if ignored:
        print(f"LOG: iPhone 搜索暂不支持 {'、'.join(ignored)} 筛选，已忽略；结果固定按「新发」排序。")


async def _run_phone_search(task_config: dict, debug_limit: int) -> int:
    keyword = task_config["keyword"]
    task_name = task_config.get("task_name", "Untitled Task")
    decision_mode = str(task_config.get("decision_mode", "ai")).strip().lower()
    keyword_rules = tuple(task_config.get("keyword_rules") or ())
    exclude_keyword_rules = tuple(task_config.get("exclude_keyword_rules") or ())
    analyze_images = _should_analyze_images(task_config)
    capture_screenshot = decision_mode == "ai" and analyze_images and not SKIP_AI_ANALYSIS

    settings = IPhoneSettings.from_env()
    selected_udids = await asyncio.to_thread(load_selected_iphone_udids)
    candidates = candidate_devices(await asyncio.to_thread(discover_devices), selected_udids, settings.udid)
    devices_by_udid = {device.udid: device for device in candidates}
    device = candidates[0]
    screens = max(1, int(task_config.get("max_pages") or 1)) * settings.screens_per_page
    _log_ignored_filters(task_config)

    processed_links = await asyncio.to_thread(load_processed_link_keys, keyword)
    known_fingerprints = {
        card_fingerprint(title, price)
        for title, price in await asyncio.to_thread(load_processed_title_prices, keyword)
    }
    historical_snapshots = load_price_snapshots(keyword)
    history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    history_seen_item_ids: set[str] = set()
    market_items: list[dict] = []
    rejected: Counter = Counter()
    counters = {"candidates": 0, "submitted": 0}
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()

    dispatcher = ItemAnalysisDispatcher(
        concurrency=_get_ai_analysis_concurrency(task_config),
        skip_ai_analysis=SKIP_AI_ANALYSIS,
        seller_loader=_no_seller_profile,
        image_downloader=download_all_images,
        ai_analyzer=get_ai_analysis,
        notifier=send_ntfy_notification,
        saver=save_to_jsonl,
    )

    def is_candidate(card: PhoneCard) -> bool:
        if debug_limit > 0 and counters["candidates"] >= debug_limit:
            log_time(f"已达到调试上限 ({debug_limit})，停止获取新商品。")
            stop_event.set()
            return False
        if card.item_id and get_link_unique_key(item_link(card.item_id)) in processed_links:
            rejected["已处理过"] += 1
            return False
        reason = card_rejection_reason(
            card,
            min_price=task_config.get("min_price"),
            max_price=task_config.get("max_price"),
            decision_mode=decision_mode,
            keyword_rules=keyword_rules,
            exclude_keyword_rules=exclude_keyword_rules,
            known_fingerprints=known_fingerprints,
        )
        if reason:
            rejected[reason] += 1
            return False
        counters["candidates"] += 1
        log_time(f"[iPhone] 候选 ¥{card.price:g} {card.region or '地区未显示'} {card.title[:30]}，进入详情")
        return True

    def on_card(card: PhoneCard) -> None:
        market_items.append({"当前售价": f"¥{card.price:g}"})

    def handle_listing(listing: IPhoneListing) -> None:
        item_data = build_item_data(listing, device.name)
        known_fingerprints.add(card_fingerprint(listing.card.title, listing.card.price))
        unique_key = get_link_unique_key(item_data["商品链接"])
        if unique_key in processed_links:
            log_time(f"[iPhone] 商品 {listing.item_id} 已存在，跳过。")
            return
        processed_links.add(unique_key)
        image_paths: tuple[str, ...] = ()
        if capture_screenshot and listing.screenshot_png:
            saved = _save_screenshot(task_name, listing.item_id, listing.screenshot_png)
            image_paths = (saved,) if saved else ()
        historical_snapshots.extend(
            record_market_snapshots(
                keyword=keyword,
                task_name=task_name,
                items=[item_data],
                run_id=history_run_id,
                snapshot_time=datetime.now().isoformat(),
                seen_item_ids=history_seen_item_ids,
            )
        )
        final_record = {
            "爬取时间": datetime.now().isoformat(),
            "搜索关键字": keyword,
            "任务名称": task_name,
            "商品信息": item_data,
            "卖家信息": {},
        }
        price_reference = build_market_reference(
            keyword=keyword,
            item=item_data,
            current_market_items=list(market_items),
            historical_snapshots=historical_snapshots,
        )
        final_record["价格参考"] = price_reference
        final_record["price_insight"] = price_reference.get("本商品价格位置", {})
        dispatcher.submit(
            ItemAnalysisJob(
                keyword=keyword,
                task_name=task_name,
                decision_mode=decision_mode,
                analyze_images=analyze_images,
                prompt_text=task_config.get("ai_prompt_text", ""),
                keyword_rules=keyword_rules,
                exclude_keyword_rules=exclude_keyword_rules,
                final_record=final_record,
                seller_id=None,
                zhima_credit_text=None,
                registration_duration_text="",
                local_image_paths=image_paths,
            )
        )
        counters["submitted"] += 1
        log_time(f"[iPhone] 商品 {listing.item_id} 已提交后台分析。累计 {counters['submitted']} 个新商品。")

    def on_listing(listing: IPhoneListing) -> None:
        loop.call_soon_threadsafe(handle_listing, listing)

    def run_blocking():
        nonlocal device
        names = "、".join(item.name for item in candidates)
        with any_iphone_device_lock(
            list(devices_by_udid),
            settings.lock_timeout_seconds,
            stop_event,
            on_wait=lambda: log_time(f"[iPhone] {names} 都在被其他任务使用，排队等待..."),
        ) as udid:
            device = devices_by_udid[udid]
            channel = iphone_device_channel(udid)
            proxy = channel["channel"] == "proxy" and bool(channel["client_ip"])
            log_time(
                f"[iPhone] 使用 {device.name}（{'代理采集' if proxy else '原有采集'}）搜索 '{keyword}'，"
                f"按「新发」排序，最多读取 {screens} 屏"
            )
            # 按勾选顺序固定端口偏移，不同手机并行时转发端口不冲突
            port_offset = selected_udids.index(udid) if udid in selected_udids else 0
            options = {"stop_event": stop_event, "log": print, "port_offset": port_offset}
            if proxy:
                client = IPhoneProxySearchClient(settings, device, client_ip=channel["client_ip"], **options)
            else:
                client = IPhoneSearchClient(settings, device, **options)
            return client.search(
                keyword,
                screens,
                is_candidate,
                on_listing,
                on_card=on_card,
                capture_screenshot=capture_screenshot,
            )

    try:
        summary = await asyncio.to_thread(run_blocking)
    except asyncio.CancelledError:
        stop_event.set()
        raise
    finally:
        # 线程里交出的商品先排进事件循环，再等待它们的分析完成。
        await asyncio.sleep(0)
        log_time("等待后台分析任务完成...")
        await dispatcher.join()

    skipped = "，".join(f"{reason} {count}" for reason, count in rejected.items()) or "无"
    log_time(
        f"[iPhone] 本次看到 {summary.cards_seen} 张卡片，候选 {summary.candidates}，"
        f"取得链接 {summary.linked}，提交分析 {counters['submitted']}；未进详情：{skipped}"
    )
    for warning in summary.warnings:
        print(f"LOG: [iPhone] 警告 {warning}")
    if summary.cards_seen == 0:
        print("LOG: [iPhone] 结果页没有读到商品卡片，请确认手机已解锁、闲鱼已登录且关键词有结果。")
    return counters["submitted"]


async def scrape_xianyu_via_phone(task_config: dict, debug_limit: int = 0) -> int:
    task_name = task_config.get("task_name", "未命名任务")
    decision = FAILURE_GUARD.should_skip_start(task_name)
    if decision.skip:
        print(
            f"[FailureGuard] 任务 '{task_name}' 已暂停重试 "
            f"(连续失败 {decision.consecutive_failures}/{FAILURE_GUARD.threshold})"
        )
        return 0
    try:
        processed = await _run_phone_search(task_config, debug_limit)
    except asyncio.CancelledError:
        log_time("收到取消信号，正在终止 iPhone 搜索...")
        raise
    except Exception as exc:
        reason = f"iPhone 搜索失败: {exc}"
        print(reason)
        await _notify_task_failure(task_config, reason, cookie_path=None)
        return 0
    finally:
        cleanup_task_images(task_name)
    FAILURE_GUARD.record_success(task_name)
    return processed
