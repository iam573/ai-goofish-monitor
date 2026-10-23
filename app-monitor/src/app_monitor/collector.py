from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, Settings
from .regions import CITY_DISTRICTS, CITY_PROVINCE, PROVINCES, normalize_city

logger = logging.getLogger(__name__)

# 采集过程中的实时回调：on_items(新读到的商品, storage=..., region_applied=..., filter_applied=...,
# stage="当前步骤", pass_info={本轮结果})。回调必须很快返回（只入队），可以从其他线程调用。
ItemCallback = Callable[..., None]
# A verified detail may be rejected before obtaining its public link. None
# means continue; a rejection contains the run-record decision, never an ID.
DetailCheck = Callable[[dict[str, Any], bool, str], dict[str, Any] | None]


def emit_safely(callback: ItemCallback | None, items: list[dict[str, Any]] | None = None, **meta: Any) -> None:
    if callback is None:
        return
    try:
        callback(list(items or []), **meta)
    except Exception:
        logger.exception("实时回调失败 meta=%s", {key: meta[key] for key in meta if key != "pass_info"})


_NOT_REGION = {
    "包邮", "全新", "验货宝", "发货极快", "信用极好", "超赞", "降价", "个人闲置", "自提",
    "小刀", "可小刀", "同城", "附近", "想要", "急出", "国行", "正品", "保真",
}
_REGION_PATTERN = re.compile(r"^[\u4e00-\u9fa5]{2,12}$")


def guess_region(texts: list[str], hint: str = "") -> str:
    """从卡片附近的短文本中找出地区；优先返回包含目标城市的文本。"""
    candidates = [text for text in texts if _REGION_PATTERN.match(text) and text not in _NOT_REGION]
    hint = normalize_city(hint)
    if hint:
        keys = (hint, *CITY_DISTRICTS.get(hint, ()))
        for text in candidates:
            if any(key in text for key in keys):
                return text
    for text in candidates:
        if text.startswith(PROVINCES) or text.endswith(("省", "市", "区", "县", "州", "盟")):
            return text
    return ""


@dataclass(frozen=True)
class VisibleCard:
    title: str
    price: float
    region: str = ""
    tags: tuple[str, ...] = ()


def extract_expdata(raw: str) -> list[dict[str, Any]]:
    marker = "expdata="
    start = raw.find(marker)
    if start < 0:
        return []
    start += len(marker)
    if start >= len(raw) or raw[start] not in "[{":
        return []
    opening = raw[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(raw[start : index + 1])
                except json.JSONDecodeError:
                    return []
                values = payload if isinstance(payload, list) else [payload]
                return [
                    value.get("exargs", value)
                    for value in values
                    if isinstance(value, dict)
                ]
    return []


def _image_urls(value: Any) -> list[str]:
    """从曝光参数中保留明确出现的图片 URL，不根据商品 ID 推测图片地址。"""
    found: list[str] = []

    def visit(current: Any, key: str = "") -> None:
        if isinstance(current, dict):
            for child_key, child in current.items():
                visit(child, str(child_key))
        elif isinstance(current, list):
            for child in current:
                visit(child, key)
        elif isinstance(current, str) and any(token in key.lower() for token in ("pic", "img", "image")):
            url = current.strip()
            if url.startswith(("https://", "http://")) and url not in found:
                found.append(url)

    visit(value)
    return found


def _real_item_id(value: Any) -> str:
    """优先读取商品专属 ID，忽略明显的占位编号。"""
    for key in ("item_id", "itemId", "id"):
        candidate = str(value.get(key) or "").strip()
        if candidate.isdigit() and len(candidate) >= 8:
            return candidate
    return ""


class ExposureReader:
    def __init__(self, path: Path):
        self.path = path

    def max_row_id(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(_id), 0) FROM usertrack").fetchone()
        return int(row[0])

    def read_after(self, row_id: int, keyword: str) -> tuple[list[dict[str, Any]], int]:
        if not self.path.exists():
            raise FileNotFoundError(f"闲鱼曝光数据库不存在：{self.path}")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT _id, args, create_time
                FROM usertrack
                WHERE _id > ? AND page_name='Page_xySearchResult'
                  AND arg1='Page_xySearchResult_Appear-Item' AND event_id=2201
                ORDER BY _id
                """,
                (row_id,),
            ).fetchall()
        items: dict[str, dict[str, Any]] = {}
        max_id = row_id
        for row in rows:
            max_id = max(max_id, int(row["_id"]))
            for value in extract_expdata(row["args"] or ""):
                item_keyword = str(value.get("keyword") or value.get("q") or value.get("original_q") or "")
                if item_keyword.lower() != keyword.lower():
                    continue
                item_id = _real_item_id(value)
                if not item_id:
                    continue
                try:
                    price = float(value.get("displayPrice") or value.get("price"))
                except (TypeError, ValueError):
                    continue
                published = value.get("publishTime")
                publish_time = None
                try:
                    publish_time = datetime.fromtimestamp(int(published) / 1000).astimezone().isoformat(timespec="seconds")
                except (TypeError, ValueError, OSError):
                    pass
                tags = []
                if value.get("tagname"):
                    tags.append(str(value["tagname"]))
                items[item_id] = {
                    "item_id": item_id,
                    "keyword": item_keyword,
                    "price": price,
                    "publish_time": publish_time,
                    "tags": tags,
                    "image_urls": _image_urls(value),
                    "index": int(value.get("index") or 0),
                    "source_row_id": int(row["_id"]),
                }
        return list(items.values()), max_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection


class AXSnapshot:
    """调用 scripts/ax_snapshot.swift 读取或操作闲鱼窗口的可访问性树。

    首次使用时用 swiftc 编译到 .runtime/，之后直接执行二进制，省去每次解释启动的时间；
    编译失败时退回 ``swift`` 解释执行。
    """

    def __init__(self, bundle_id: str, script: Path | None = None):
        self.bundle_id = bundle_id
        self.script = script or ROOT / "scripts" / "ax_snapshot.swift"
        self.binary = ROOT / ".runtime" / "ax_snapshot"
        self._compile_failed = False

    async def click(self, text: str) -> dict[str, Any]:
        output = await self._run("--click", text)
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            result = {"clicked": True}
        logger.info("A11y 点击 target=%s method=%s", text, result.get("method", "unknown"))
        return result

    async def click_path(self, steps: list[str]) -> dict[str, Any]:
        """依次点击多个元素；``?`` 开头的步骤找不到时跳过。"""
        output = await self._run("--click-path", "|".join(steps))
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            result = {"clicked": steps}
        logger.info("A11y 点击路径 steps=%s actions=%s", " > ".join(steps), result.get("actions", []))
        return result

    async def home_search(self, city: str, keyword: str) -> dict[str, Any]:
        """从首页第二个地区 Tab 进入搜索，让结果页继承该地区。"""
        output = await self._run("--home-search", city, keyword)
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"searched": True, "region": city, "keyword": keyword}

    async def escape(self) -> None:
        await self._run("--escape")

    async def scroll(self, screens: float = 1) -> None:
        await self._run("--scroll", str(screens))

    async def nodes(self) -> list[dict[str, Any]]:
        output = await self._run()
        payload = json.loads(output)
        return payload if isinstance(payload, list) else []

    async def wait_for(self, labels: set[str], timeout: float = 15) -> list[dict[str, Any]]:
        """等待页面出现全部文字，避免固定睡眠不足时误判元素不存在。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        latest: list[dict[str, Any]] = []
        while True:
            latest = await self.nodes()
            texts = {
                str(node.get(key) or "").strip()
                for node in latest
                for key in ("title", "value", "description")
                if str(node.get(key) or "").strip()
            }
            if labels.issubset(texts):
                return latest
            if loop.time() >= deadline:
                missing = "、".join(sorted(labels - texts))
                raise RuntimeError(f"等待页面元素超时：{missing}")
            await asyncio.sleep(0.5)

    async def wait_for_sort_bar(self, labels: set[str], timeout: float = 15) -> list[dict[str, Any]]:
        """等待排序栏这一行出现目标文字，避免命中筛选面板等其他区域的同名文本。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        latest: list[dict[str, Any]] = []
        while True:
            latest = await self.nodes()
            row = _sort_bar_labels(latest)
            if labels.issubset(row):
                return latest
            if loop.time() >= deadline:
                missing = "、".join(sorted(labels - row))
                current = "、".join(row) or "无可识别排序项"
                raise RuntimeError(f"等待排序栏元素超时：{missing}；当前排序栏：{current}")
            await asyncio.sleep(0.5)

    async def _command(self) -> list[str]:
        if not self._compile_failed:
            fresh = self.binary.exists() and self.binary.stat().st_mtime >= self.script.stat().st_mtime
            if not fresh:
                self.binary.parent.mkdir(parents=True, exist_ok=True)
                process = await asyncio.create_subprocess_exec(
                    "swiftc", "-O", str(self.script), "-o", str(self.binary),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    fresh = await asyncio.wait_for(process.wait(), timeout=240) == 0
                except asyncio.TimeoutError:
                    process.kill()
                    fresh = False
                self._compile_failed = not fresh
            if fresh:
                return [str(self.binary)]
        return ["swift", str(self.script)]

    async def _run(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *await self._command(),
            self.bundle_id,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        if process.returncode:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "读取可访问性树失败")
        return stdout.decode("utf-8")


def visible_cards_from_nodes(
    nodes: list[dict[str, Any]], keyword: str, region_hint: str = ""
) -> list[VisibleCard]:
    texts: list[dict[str, Any]] = []
    ignored = {"新发", "价格", "区域", "筛选", "综合", "信用", "搜索"}
    for node in nodes:
        text = next(
            (
                str(node.get(key)).strip()
                for key in ("title", "value", "description")
                if node.get(key) and str(node.get(key)).strip()
            ),
            "",
        )
        if not text or text in ignored:
            continue
        position = node.get("position") or {}
        texts.append(
            {
                "text": text,
                "x": float(position.get("x", 0)),
                "y": float(position.get("y", 0)),
            }
        )

    price_pattern = re.compile(r"(?:¥|￥)?\s*(\d{2,6}(?:\.\d{1,2})?)\s*元?")
    distance_pattern = re.compile(r"^[\d.]+\s*(?:km|m|公里|米)$", re.I)
    prices = []
    for value in texts:
        matches = list(price_pattern.finditer(value["text"]))
        if len(matches) != 1:
            continue
        # 整段就是价格（「3850元」「¥3200」「3649」），或带货币符号；排除「25.7km」这类距离
        if not price_pattern.fullmatch(value["text"]) and not re.search(r"[¥￥元]", matches[0].group(0)):
            continue
        price = float(matches[0].group(1))
        if price < 20:
            continue
        prices.append({**value, "price": price, "match": matches[0]})

    cards: list[VisibleCard] = []
    used_titles: set[str] = set()
    for price_node in sorted(prices, key=lambda item: (item["y"], item["x"])):
        inline = price_node["text"]
        match = price_node["match"]
        inline_title = (inline[: match.start()] + " " + inline[match.end() :]).strip(" -|，,")
        candidates = [
            value
            for value in texts
            if value["text"] != price_node["text"]
            and value["text"] not in used_titles
            and len(value["text"]) >= 6
            and abs(value["y"] - price_node["y"]) <= 100
            and abs(value["x"] - price_node["x"]) <= 220
            and not price_pattern.fullmatch(value["text"])
            and not distance_pattern.match(value["text"])
        ]
        if len(inline_title) >= 6:
            title = inline_title
        elif candidates:
            preferred = [value for value in candidates if keyword.lower() in value["text"].lower()]
            pool = preferred or candidates
            title = max(pool, key=lambda item: len(item["text"]))["text"]
        else:
            continue
        if title in used_titles:
            continue
        used_titles.add(title)
        nearby = [
            value["text"]
            for value in texts
            if value["text"] not in (title, price_node["text"])
            and len(value["text"]) <= 12
            and abs(value["x"] - price_node["x"]) <= 220
            and -60 <= value["y"] - price_node["y"] <= 120
        ]
        cards.append(
            VisibleCard(
                title=title,
                price=price_node["price"],
                region=guess_region(nearby, region_hint),
            )
        )
    return cards


SORT_BAR = ("综合", "价格", "降价", "新发", "筛选")


def text_of(node: dict[str, Any]) -> str:
    return next(
        (str(node[key]).strip() for key in ("title", "value", "description") if str(node.get(key) or "").strip()),
        "",
    )


def _sort_bar_labels(nodes: list[dict[str, Any]]) -> set[str]:
    """Return labels from the row containing the known search sort controls."""
    return {text_of(node) for node in _sort_bar_row(nodes)}


def _sort_bar_rows(nodes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Return rows containing all known sort controls, avoiding duplicate labels elsewhere."""
    rows: list[tuple[float, set[str]]] = []
    for node in nodes:
        label = text_of(node)
        if not label:
            continue
        y = float((node.get("position") or {}).get("y", 0))
        row = next((labels for row_y, labels in rows if abs(y - row_y) <= 6), None)
        if row is None:
            row = set()
            rows.append((y, row))
        row.add(label)
    expected = {"综合", "新发", "降价", "筛选"}
    matching_y = [y for y, labels in rows if expected.issubset(labels)]
    if not matching_y:
        return []
    matching_y.sort(key=lambda y: len(next(labels for row_y, labels in rows if row_y == y)), reverse=True)
    return [
        sorted(
            (node for node in nodes if abs(float((node.get("position") or {}).get("y", 0)) - bar_y) <= 6 and text_of(node)),
            key=lambda node: float((node.get("position") or {}).get("x", 0)),
        )
        for bar_y in matching_y
    ]


def _sort_bar_row(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _sort_bar_rows(nodes)
    return rows[0] if rows else []


def capacity_filter_chip(nodes: list[dict[str, Any]]) -> str | None:
    """读取排序栏下一行的容量筛选标签，避开商品卡片里的容量文字。"""
    row = _sort_bar_row(nodes)
    filter_node = next((node for node in row if text_of(node) == "筛选"), None)
    if filter_node is None:
        return None
    anchor = filter_node.get("position") or {}
    sort_x, sort_y = float(anchor.get("x", 0)), float(anchor.get("y", 0))
    matches = []
    for node in nodes:
        label = text_of(node).strip().upper()
        if label != "存储容量" and not re.fullmatch(r"\d+(?:G|GB|T|TB)", label):
            continue
        position = node.get("position") or {}
        x, y = float(position.get("x", 0)), float(position.get("y", 0))
        if sort_x - 400 <= x < sort_x and sort_y + 15 <= y <= sort_y + 65:
            matches.append((abs(y - sort_y - 32), label))
    return min(matches)[1] if matches else None


def _capacity_matches(label: str | None, storage: str) -> bool:
    actual = (label or "").upper().replace("GB", "G").replace("TB", "T")
    expected = storage.upper() if storage.upper().endswith("T") else f"{storage.upper()}G"
    return actual == expected


def _capacity_picker_open(nodes: list[dict[str, Any]]) -> bool:
    controls = {text_of(node) for node in nodes if node.get("role") == "AXButton"}
    return {"重置", "确定"}.issubset(controls)


def _region_between_sort_controls(row: list[dict[str, Any]]) -> str | None:
    texts = [text_of(node) for node in row]
    if "新发" not in texts or "筛选" not in texts:
        return None
    start, end = texts.index("新发"), texts.index("筛选")
    for value in texts[start + 1 : end]:
        city = normalize_city(value)
        if value == "区域" or value in PROVINCES or city in CITY_PROVINCE:
            return value
    return None


def region_slot(nodes: list[dict[str, Any]], city: str = "") -> str | None:
    """排序栏里「新发」和「筛选」之间的按钮文字：未选地区时是「区域」，选过后显示城市名。"""
    rows = _sort_bar_rows(nodes)
    slots = [slot for row in rows if (slot := _region_between_sort_controls(row))]
    target = normalize_city(city)
    if target:
        match = next((slot for slot in slots if normalize_city(slot) == target), None)
        if match:
            return match
    return slots[0] if slots else None


def search_bar_city(nodes: list[dict[str, Any]]) -> str | None:
    """Read the city chip left of the search field, above the result sort row."""
    row = _sort_bar_row(nodes)
    if not row:
        return None
    sort_y = float((row[0].get("position") or {}).get("y", 0))
    sort_start_x = min(float((node.get("position") or {}).get("x", 0)) for node in row)
    candidates = []
    for node in nodes:
        value = text_of(node)
        city = normalize_city(value)
        position = node.get("position") or {}
        y, x = float(position.get("y", -1)), float(position.get("x", 0))
        if city in CITY_PROVINCE and 0 <= y < sort_y - 20 and x < sort_start_x:
            candidates.append((y, x, city))
    return min(candidates)[2] if candidates else None


def region_context_matches(nodes: list[dict[str, Any]], city: str) -> tuple[bool, str | None, str | None]:
    """Confirm the configured city against the search-field chip and/or result sort-row tab."""
    target = normalize_city(city)
    slot = region_slot(nodes, target)
    search_city = search_bar_city(nodes)
    explicit = [normalize_city(value) for value in (slot, search_city) if value and value != "区域"]
    if not explicit:
        return False, search_city, slot
    # If both UI indicators are present, require them to agree with the configured city.
    return all(value == target for value in explicit), search_city, slot


def search_bar_has_city(nodes: list[dict[str, Any]], city: str) -> bool:
    """Confirm the city chip at the left of the search field, above the results sort row."""
    target = normalize_city(city)
    row = _sort_bar_row(nodes)
    if not target or not row:
        return False
    sort_y = float((row[0].get("position") or {}).get("y", 0))
    sort_start_x = min(float((node.get("position") or {}).get("x", 0)) for node in row)
    return any(
        normalize_city(text_of(node)) == target
        and 0 <= float((node.get("position") or {}).get("y", -1)) < sort_y - 20
        and float((node.get("position") or {}).get("x", 0)) < sort_start_x
        for node in nodes
    )


def plan_region_steps(nodes: list[dict[str, Any]], city: str, steps: list[str]) -> list[str] | None:
    """返回实际要点的区域路径；``None`` 表示排序栏已经显示目标城市，无需再选。

    闲鱼会记住上次选的地区，下次搜索时排序栏直接显示「厦门」而不是「区域」。
    """
    slot = region_slot(nodes, city)
    city = city.strip().removesuffix("市")
    if slot and city and slot.removesuffix("市") == city:
        return None
    if slot and steps and steps[0] == "区域" and slot != "区域":
        return [slot, *steps[1:]]
    return steps


@dataclass
class CollectResult:
    items: list[dict[str, Any]]
    region_applied: bool = False
    region_error: str | None = None
    filter_applied: bool = False
    filter_error: str | None = None
    # 新发采集轮看到的商品数和错误
    passes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        pass_errors = {entry.get("error") for entry in self.passes if entry.get("error")}
        values = [value for value in (self.region_error, self.filter_error) if value not in pass_errors]
        for entry in self.passes:
            if not entry.get("error"):
                continue
            warning = f"{entry['label']}：{entry['error']}"
            if not any(warning in value for value in values if value):
                values.append(warning)
        return list(dict.fromkeys(value for value in values if value))


class GoofishCollector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exposures = ExposureReader(settings.track_db)
        self.accessibility = AXSnapshot(settings.bundle_id)

    async def collect(
        self,
        keyword: str,
        region_steps: list[str] | None = None,
        region_hint: str = "",
        storage_filters: list[tuple[str, list[str]]] | None = None,
        scroll_pages: int = 0,
        on_items: ItemCallback | None = None,
    ) -> CollectResult:
        """每款机型只打开一次搜索页，再逐个切换容量并读取「新发」第一页。

        每翻一屏就把新关联到的商品通过 ``on_items`` 交给调用方，边搜边判价、推送。
        """
        result = CollectResult(items=[])

        def add_pass(entry: dict[str, Any]) -> None:
            result.passes.append(entry)
            emit_safely(on_items, pass_info=entry, storage=entry.get("storage"))

        emit_safely(on_items, stage="打开搜索页")
        logger.info(
            "采集开始 keyword=%s region=%s capacities=%s sort=新发 scroll_pages=%s",
            keyword, region_hint or "未指定", [storage for storage, _ in storage_filters or []], scroll_pages,
        )
        search_nodes: list[dict[str, Any]] = []
        used_home_region = False
        setup_actions: list[dict[str, Any]] = []
        if region_hint:
            try:
                home_result = await self.accessibility.home_search(region_hint, keyword)
                search_nodes = await self.accessibility.wait_for_sort_bar({"新发"}, timeout=15)
                used_home_region = True
                setup_actions.append({
                    "action": "按首页地区搜索", "target": region_hint, "success": True,
                    "method": home_result.get("method") if isinstance(home_result, dict) else None,
                    "text_method": home_result.get("text_method") if isinstance(home_result, dict) else None,
                })
                logger.info("A11y 首页搜索完成 keyword=%s region=%s result=%s", keyword, region_hint, home_result)
            except Exception as exc:
                # 首页第二 Tab 不匹配或 UI 搜索不可用时，回退到原有深链。
                used_home_region = False
                setup_actions.append({"action": "首页地区搜索", "target": region_hint, "success": False, "error": str(exc)})
                logger.warning("A11y 首页搜索失败 keyword=%s region=%s error=%s", keyword, region_hint, exc)

        if not used_home_region:
            deep_link = (
                "fleamarket://idle_search_result?keyword="
                f"{quote(keyword, safe='')}&searchReqFromPage=home&searchTabType=SEARCH_TAB_MAIN"
            )
            process = await asyncio.create_subprocess_exec("open", deep_link)
            if await process.wait() != 0:
                raise RuntimeError("无法打开闲鱼搜索深链")
            search_nodes = await self.accessibility.wait_for_sort_bar({"新发"}, timeout=15)
            setup_actions.append({"action": "打开搜索结果", "target": keyword, "method": "闲鱼深链", "success": True})
            logger.info("闲鱼搜索页就绪 keyword=%s method=deep-link", keyword)

        if region_steps:
            emit_safely(on_items, stage=f"确认地区 {region_hint}")
            city_confirmed, search_city, slot = region_context_matches(search_nodes, region_hint)
            logger.info(
                "地区二次确认 configured=%s search_bar=%s result_tab=%s confirmed=%s",
                region_hint or "未指定", search_city or "未显示", slot or "未显示", city_confirmed,
            )
            if city_confirmed:
                # Confirm the selected city in the App UI before skipping province/city filters.
                result.region_applied = True
                setup_actions.append({
                    "action": "地区筛选", "target": region_hint,
                    "method": "闲鱼搜索栏/排序栏城市已确认，跳过结果页筛选", "success": True,
                })
            else:
                planned = plan_region_steps(search_nodes, region_hint, region_steps)
                if planned is None:
                    result.region_applied = True  # App 记住了目标城市，排序栏已选中
                    setup_actions.append({"action": "地区筛选", "target": region_hint, "method": "排序栏已选中，跳过点击", "success": True})
                else:
                    result.region_applied, result.region_error = await self._apply("地区筛选", planned, setup_actions)
                    if result.region_applied:
                        for attempt in range(6):
                            current = await self.accessibility.nodes()
                            confirmed, search_city, slot = region_context_matches(current, region_hint)
                            if confirmed or attempt == 5:
                                break
                            await asyncio.sleep(0.5)
                        logger.info(
                            "地区筛选后确认 configured=%s search_bar=%s result_tab=%s confirmed=%s",
                            region_hint, search_city or "未显示", slot or "未显示", confirmed,
                        )
                        if not confirmed:
                            result.region_applied = False
                            result.region_error = (
                                f"地区筛选点击完成但结果页未显示「{region_hint}」"
                                f"（搜索栏：{search_city or '未显示'}；排序栏：{slot or '未显示'}）"
                            )
        capacity_filters = storage_filters or [("", [])]
        filter_errors: list[str] = []
        filter_success: list[bool] = []
        merged: dict[str, dict[str, Any]] = {}
        for capacity_index, (storage, filter_steps) in enumerate(capacity_filters):
            label = "新发"
            actions = list(setup_actions)
            storage_label = (storage if storage.endswith("T") else f"{storage}G") if storage else "全部容量"
            emit_safely(on_items, stage=f"切换容量 {storage_label}", storage=storage or None)
            if filter_steps:
                # Use the quick-filter category directly. The broad 「筛选」 button is
                # unnecessary and can toggle a different panel. The picker is multi-select:
                # deselect the previous capacity before choosing the next one.
                current_chip = capacity_filter_chip(await self.accessibility.nodes())
                effective_filter_steps = list(filter_steps)
                if effective_filter_steps[0] == "筛选" and len(effective_filter_steps) > 1:
                    effective_filter_steps.pop(0)
                if current_chip is None:
                    applied, filter_error = False, "找不到结果页容量筛选标签"
                elif _capacity_matches(current_chip, storage):
                    applied, filter_error = True, None
                    actions.append({
                        "action": f"容量筛选 {storage}", "target": storage,
                        "method": "结果页容量标签已选中", "success": True,
                    })
                else:
                    if current_chip != "存储容量" and effective_filter_steps[0] == "存储容量":
                        effective_filter_steps[0] = current_chip
                        effective_filter_steps.insert(1, current_chip)
                    applied, filter_error = await self._apply(
                        f"容量筛选 {storage}", effective_filter_steps, actions
                    )
                if applied:
                    chip = None
                    picker_open = False
                    for _ in range(8):
                        current = await self.accessibility.nodes()
                        chip = capacity_filter_chip(current)
                        picker_open = _capacity_picker_open(current)
                        if _capacity_matches(chip, storage) and not picker_open:
                            break
                        await asyncio.sleep(0.5)
                    applied = _capacity_matches(chip, storage) and not picker_open
                    actions.append({
                        "action": "容量结果确认", "target": storage,
                        "observed": chip or "未显示", "picker_closed": not picker_open,
                        "success": applied,
                    })
                    logger.info("容量筛选后确认 expected=%s observed=%s picker_closed=%s confirmed=%s",
                                storage, chip or "未显示", not picker_open, applied)
                    if not applied:
                        filter_error = (
                            f"点击确定后结果页容量标签为「{chip or '未显示'}」，不是「{storage}」"
                            if not _capacity_matches(chip, storage)
                            else "点击确定后容量筛选弹层仍未关闭"
                        )
                filter_success.append(applied)
                if not applied:
                    filter_errors.append(f"{storage}：{filter_error}")
                    add_pass({
                        "label": label, "storage": storage, "filter_applied": False,
                        "observed": 0, "cards": 0, "exposures": 0, "actions": actions,
                        "items": [], "error": filter_error,
                    })
                    continue
            # 只读取点击该按钮之后产生的曝光，避免混入上一轮或筛选前的列表。
            start_row = await asyncio.to_thread(self.exposures.max_row_id)
            try:
                logger.info("A11y 等待排序栏 target=%s", label)
                await self.accessibility.wait_for_sort_bar({label}, timeout=8)
                click_result = await self.accessibility.click(label) or {}
                actions.append({
                    "action": "点击排序", "target": label, "success": True,
                    "method": click_result.get("method", "AX 操作"),
                })
            except Exception as exc:
                logger.warning("A11y 排序失败 target=%s error=%s", label, exc)
                actions.append({"action": "点击排序", "target": label, "success": False, "error": str(exc)})
                message = f"找不到按钮：{exc}"
                add_pass({
                    "label": label, "storage": storage, "filter_applied": bool(filter_steps),
                    "observed": 0, "actions": actions, "items": [], "error": message,
                })
                continue
            await asyncio.sleep(4)
            emitted: set[str] = set()

            def tag(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                for value in values:
                    value.setdefault("source_pass", label)
                    value["source_storage"] = storage or None
                return values

            def emit_new(values: list[dict[str, Any]], stage: str) -> None:
                fresh = [value for value in values if value["item_id"] not in emitted]
                emitted.update(value["item_id"] for value in fresh)
                emit_safely(
                    on_items, [dict(value) for value in fresh], stage=stage, storage=storage or None,
                    region_applied=result.region_applied, filter_applied=bool(filter_steps),
                )

            async def on_screen(cards_so_far: list[VisibleCard], page: int) -> None:
                if on_items is None:
                    return
                exposures_now, _ = await asyncio.to_thread(self.exposures.read_after, start_row, keyword)
                emit_new(tag(self._correlate(exposures_now, cards_so_far)),
                         f"读取 {storage_label} 第 {page + 1}/{scroll_pages + 1} 屏")

            cards = await self._read_first_page(keyword, region_hint, scroll_pages, on_screen)
            exposures, _ = await asyncio.to_thread(self.exposures.read_after, start_row, keyword)
            items = tag(self._correlate(exposures, cards))
            emit_new(items, f"{storage_label} 读取完成")
            linked = {item["title"] for item in items}
            unlinked = [card for card in cards if card.title not in linked]
            if unlinked:
                # 页面上看到了、但没对上曝光记录的卡片：没有商品链接，只记进运行记录供核对
                emit_safely(
                    on_items,
                    cards=[{"title": card.title, "price": card.price, "display_region": card.region,
                            "link_error": "列表卡片没有对上曝光记录，拿不到商品链接"} for card in unlinked],
                    storage=storage or None, region_applied=result.region_applied, filter_applied=bool(filter_steps),
                )
            for item in items:
                existing = merged.get(item["item_id"])
                if existing is None:
                    merged[item["item_id"]] = item
                elif existing.get("source_storage") != storage:
                    # 同一商品出现在多个容量结果里时，不用筛选容量替标题补容量，避免错误判价。
                    existing["source_storage"] = None
            add_pass(
                {
                    "label": label,
                    "storage": storage or None,
                    "filter_applied": bool(filter_steps),
                    "observed": len(items),
                    "cards": len(cards),
                    "exposures": len(exposures),
                    "actions": actions,
                    "items_total": len(items),
                    "items": [
                        {key: item.get(key) for key in ("item_id", "title", "price", "display_region", "item_url")}
                        for item in items[:30]
                    ],
                    "items_truncated": len(items) > 30,
                    "error": None,
                }
            )
            product_log = " | ".join(
                f"{item['title'][:80]} ¥{item['price']:g} {item.get('display_region') or '地区未知'} "
                f"id={item['item_id']} {item['item_url']}"
                for item in items[:30]
            ) or "无关联商品"
            logger.info(
                "采集结果 storage=%s sort=%s cards=%d exposures=%d matched=%d products=%s",
                storage or "未筛容量", label, len(cards), len(exposures), len(items), product_log,
            )
            if capacity_index < len(capacity_filters) - 1 and scroll_pages:
                try:
                    await self.accessibility.scroll(-float(scroll_pages + 2))
                    await asyncio.sleep(1)
                    logger.info("A11y 已返回列表顶部，准备切换下一容量 current=%s", storage)
                except Exception as exc:
                    add_pass({
                        "label": label,
                        "storage": capacity_filters[capacity_index + 1][0],
                        "filter_applied": False,
                        "observed": 0,
                        "actions": [],
                        "items": [],
                        "error": f"无法返回列表顶部，已停止后续容量筛选：{exc}",
                    })
                    filter_errors.append(str(exc))
                    break

        result.filter_applied = bool(filter_success) and all(filter_success)
        result.filter_error = "；".join(filter_errors) or None
        if result.passes and all(p["error"] for p in result.passes):
            # 前面的区域 / 容量筛选失败往往才是根因，一起报出来
            raise RuntimeError("；".join(result.warnings))
        result.items = list(merged.values())
        logger.info("采集结束 keyword=%s total_items=%d warnings=%s", keyword, len(result.items), result.warnings or "无")
        return result

    async def _read_first_page(
        self, keyword: str, region_hint: str, scroll_pages: int, on_screen: Callable[..., Any] | None = None,
    ) -> list[VisibleCard]:
        """读当前屏，再向下翻屏若干次，把第一页的卡片都收集起来（按标题 + 价格去重）。

        每读完一屏调用 ``on_screen(目前的卡片, 第几屏)``，用于实时关联并交出新商品。
        """
        cards: dict[tuple[str, float], VisibleCard] = {}
        for page in range(scroll_pages + 1):
            if page:
                try:
                    await self.accessibility.scroll(1)
                except Exception:
                    break
                await asyncio.sleep(2)
            for card in visible_cards_from_nodes(await self.accessibility.nodes(), keyword, region_hint):
                cards.setdefault((card.title, card.price), card)
            if on_screen is not None:
                try:
                    await on_screen(list(cards.values()), page)
                except Exception:
                    logger.exception("实时关联失败 page=%s", page)
        return list(cards.values())

    async def _apply(
        self, name: str, steps: list[str], actions: list[dict[str, Any]] | None = None
    ) -> tuple[bool, str | None]:
        try:
            output = await self.accessibility.click_path(steps)
            logger.info("A11y 路径成功 action=%s steps=%s result=%s", name, " > ".join(steps), output)
            actions and actions.append(
                {
                    "action": name,
                    "steps": steps,
                    "clicked": output.get("clicked", steps) if isinstance(output, dict) else steps,
                    "skipped": output.get("skipped", []) if isinstance(output, dict) else [],
                    "methods": output.get("actions", []) if isinstance(output, dict) else [],
                    "success": True,
                }
            )
            await asyncio.sleep(2)
            return True, None
        except Exception as exc:  # 筛选失败时仍继续采集，由标题和卡片地区复核兜底
            logger.warning("A11y 路径失败 action=%s steps=%s error=%s", name, " > ".join(steps), exc)
            actions and actions.append({"action": name, "steps": steps, "success": False, "error": str(exc)})
            # 路径走到一半时面板可能还开着，会挡住「新发」，按 Esc 关掉。
            try:
                await self.accessibility.escape()
                await asyncio.sleep(1)
                logger.info("A11y 失败恢复 action=%s method=escape", name)
            except Exception:
                logger.exception("A11y 失败恢复 action=%s method=escape failed", name)
            return False, f"{name}失败：{exc}"

    @staticmethod
    def _correlate(exposures: list[dict[str, Any]], cards: list[VisibleCard]) -> list[dict[str, Any]]:
        results = []
        remaining = list(cards)
        for exposure in sorted(exposures, key=lambda item: item["index"]):
            same_price = [card for card in remaining if abs(card.price - exposure["price"]) < 0.01]
            card = None
            # 曝光事件的 index 对同价商品更有辨识力。先尝试对应顺序的位置，
            # 再从剩余同价卡片中选距 index 最近的，避免重复价格总是取第一张。
            index = int(exposure.get("index") or 0)
            if index < len(cards):
                candidate = cards[index]
                if candidate in remaining and abs(candidate.price - exposure["price"]) < 1:
                    card = candidate
            if card is None and same_price:
                card = min(same_price, key=lambda item: abs(cards.index(item) - index))
            if not card:
                continue
            if card in remaining:
                remaining.remove(card)
            results.append(
                {
                    **exposure,
                    "title": card.title,
                    "display_region": card.region,
                    "tags": sorted(set(exposure["tags"] + list(card.tags))),
                    "item_url": f"https://www.goofish.com/item?id={exposure['item_id']}",
                    "image_urls": exposure.get("image_urls", []),
                }
            )
        return results

from urllib.parse import quote
