"""全局监控规则与搜索目标。

规则保存在 app_state 表的 ``rules`` 键中，后台可以直接修改。
搜索目标不单独建表：每次都从最新报价表推导，报价更新后自动生效。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time
from typing import Any

from .db import Database
from .regions import default_region_path, normalize_city
from .quotes import (
    APPLE_NO_WARRANTY_CATEGORY,
    APPLE_PHONE_CATEGORIES,
    GRADE_COLUMNS,
    latest_entries,
)


# 只放明确表示「不是在卖整机」的词。「配件」「手机壳」「维修」「主板」等在正常描述里很常见
# （配件齐全、送手机壳、无维修、主板无修），不能作为排除词。
DEFAULT_EXCLUDE_KEYWORDS = (
    "回收,求购,收购,高价收,租赁,出租,租机,机模,模型机,"
    "ID锁,有锁,监管机,黑解,卡贴,不开机,坏机,仅配件"
)
# v0.2/v0.3 的默认值；读到原样未改的旧默认值时自动换成新默认值
_OLD_DEFAULT_EXCLUDE_KEYWORDS = (
    "回收,求购,收购,高价收,租赁,出租,维修,换屏,配件,手机壳,机模,模型机,"
    "ID锁,有锁,监管机,黑解,卡贴,不开机,坏机,主板,屏幕总成"
)

DEFAULT_STORAGE_PATH = "筛选 > 存储容量 > {容量} > 确定"
STORAGE_PLACEHOLDER = "{容量}"
_OLD_DEFAULT_STORAGE_PATHS = (
    "筛选 > {容量} > 确定",
    "筛选 > {容量} > ?确定",
    "筛选 > {容量} > ?查看",
)

_VARIANT_ORDER = ["", "mini", "plus", "air", "pro", "pro max", "e"]


@dataclass
class Rules:
    enabled: bool = True
    # 地区：城市名用于核对商品卡片地区；点击路径用于在 App 内切换「区域」筛选。
    region_city: str = ""
    region_path: str = ""
    # 报价基准
    quote_category: str = APPLE_NO_WARRANTY_CATEGORY
    quote_grade: str = "小花"
    # 每日报价是回收价；允许挂牌价高于回收价一定金额，留出议价候选。
    max_over_quote: float = 200
    # 低于报价该比例的商品视为引流或非整机，不推送
    min_price_ratio: float = 0.3
    exclude_keywords: str = DEFAULT_EXCLUDE_KEYWORDS
    # 搜索方式：每款机型只搜索一次，再在结果页内依次切换容量筛选
    storage_path: str = DEFAULT_STORAGE_PATH
    # 每轮点击按钮后向下翻屏的次数，用来读完第一页（窗口里一屏只露出几件商品）
    scroll_pages: int = 3
    # 搜索节奏：同一机型两次搜索的最小间隔
    search_start: str = "09:00"
    search_end: str = "23:00"
    model_interval_minutes: int = 90
    # 停用项：「15 Pro」停用整个机型，「15 Pro|256」只停用该容量
    disabled_models: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "Rules":
        known = {item.name for item in fields(cls)}
        values = {key: val for key, val in (value or {}).items() if key in known}
        if values.get("exclude_keywords") == _OLD_DEFAULT_EXCLUDE_KEYWORDS:
            values["exclude_keywords"] = DEFAULT_EXCLUDE_KEYWORDS
        if values.get("storage_path") in _OLD_DEFAULT_STORAGE_PATHS:
            values["storage_path"] = DEFAULT_STORAGE_PATH
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def excluded(self) -> list[str]:
        return [word.strip() for word in re.split(r"[,，\s]+", self.exclude_keywords) if word.strip()]

    @property
    def region_steps(self) -> list[str]:
        """App 内的点击路径，例如 ``区域 > 福建 > 厦门 > 查看``；``?`` 开头表示可选步骤。"""
        if not self.region_city.strip():
            return []
        return split_path(self.region_path.strip() or default_region_path(self.region_city))

    @property
    def passes(self) -> list[str]:
        # 固定只看新发布商品，避免排序切换引入额外的 UI 状态和误点。
        return ["新发"]

    def storage_steps(self, storage: str | None) -> list[str]:
        """容量筛选的点击路径，``{容量}`` 替换为 ``256G`` / ``1T`` 这样的写法。"""
        if not storage:
            return []
        label = storage if storage.endswith("T") else f"{storage}G"
        raw = (self.storage_path.strip() or DEFAULT_STORAGE_PATH).replace(STORAGE_PLACEHOLDER, label)
        return split_path(raw)

    def is_enabled(self, model: str, storage: str | None = None) -> bool:
        disabled = set(self.disabled_models)
        series = series_of(model)
        return (
            f"@series:{series}" not in disabled
            and model not in disabled
            and (storage is None or f"{model}|{storage}" not in disabled)
        )

    def series_is_enabled(self, series: int) -> bool:
        return f"@series:{series}" not in set(self.disabled_models)

    @property
    def quote_category_name(self) -> str:
        return APPLE_PHONE_CATEGORIES.get(self.quote_category, self.quote_category)


def split_path(raw: str) -> list[str]:
    return [step.strip() for step in re.split(r"\s*(?:>|›|\|)\s*", raw) if step.strip()]


def load_rules(database: Database) -> Rules:
    return Rules.from_dict(database.state_get("rules"))


def save_rules(database: Database, rules: Rules) -> Rules:
    if rules.quote_grade not in GRADE_COLUMNS:
        raise ValueError("不支持的参考成色")
    if rules.quote_category not in APPLE_PHONE_CATEGORIES:
        raise ValueError("不支持的报价分类")
    parse_clock(rules.search_start)
    parse_clock(rules.search_end)
    rules.region_city = normalize_city(rules.region_city)
    database.state_set("rules", rules.to_dict())
    return rules


def parse_clock(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"时间格式应为 HH:MM：{value}") from exc


def in_search_window(rules: Rules, now: datetime) -> bool:
    start, end = parse_clock(rules.search_start), parse_clock(rules.search_end)
    current = now.time()
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end  # 跨午夜


def series_of(model: str) -> int:
    match = re.match(r"\s*(\d+)", str(model))
    return int(match.group(1)) if match else 0


def model_sort_key(model: str) -> tuple[int, int, str]:
    rest = re.sub(r"^\d+\s*", "", model).lower()
    rank = _VARIANT_ORDER.index(rest) if rest in _VARIANT_ORDER else 99
    return (-series_of(model), rank, model)


SEARCH_KEYWORD_OVERRIDES = {"17 Air": "iPhone Air"}


def search_keyword(model: str) -> str:
    return SEARCH_KEYWORD_OVERRIDES.get(model, f"iPhone {model}")


def eligible_quotes(database: Database, rules: Rules) -> list[dict[str, Any]]:
    """所有报价机型中当前参考成色可用于判价的条目。"""
    return latest_entries(database, rules.quote_category, rules.quote_grade)


def search_targets(database: Database, rules: Rules) -> list[dict[str, Any]]:
    """从最新报价表推导所有机型及其容量筛选项。"""
    storages: dict[str, set[str]] = {}
    for entry in latest_entries(database, rules.quote_category):
        storages.setdefault(entry["model"], set()).add(entry["storage"])
    targets = []
    for model in sorted(storages, key=model_sort_key):
        ordered = sorted(storages[model], key=_storage_value)
        units = [{"storage": value, "enabled": rules.is_enabled(model, value)} for value in ordered]
        # 机型是否启用由容量决定：至少一个容量在监控即为启用（父级勾选状态从子项推导）
        enabled = any(unit["enabled"] for unit in units)
        targets.append(
            {
                "model": model,
                "keyword": search_keyword(model),
                "series": series_of(model),
                "series_enabled": rules.series_is_enabled(series_of(model)),
                "storages": ordered,
                "enabled": enabled,
                "units": units,
            }
        )
    return targets


def expand_disabled(rules: Rules, targets: list[dict[str, Any]]) -> set[str]:
    """把系列级（``@series:15``）和机型级（``15 Pro``）停用项展开成具体容量（``15 Pro|256``）。

    勾选状态只记在容量这一层，系列和机型的勾选状态由容量推导；这样停用整个系列后，
    仍然可以单独勾选其中某个机型或容量。报价表里已没有的机型保留原停用项。
    """
    storages = {target["model"]: target["storages"] for target in targets}
    expanded: set[str] = set()
    for key in rules.disabled_models:
        if key.startswith("@series:"):
            try:
                series = int(key.split(":", 1)[1])
            except ValueError:
                expanded.add(key)
                continue
            models = [model for model in storages if series_of(model) == series]
            if not models:
                expanded.add(key)
            for model in models:
                expanded.update(f"{model}|{storage}" for storage in storages[model])
        elif "|" not in key and key in storages:
            expanded.update(f"{key}|{storage}" for storage in storages[key])
        else:
            expanded.add(key)
    return expanded


def set_targets_enabled(
    database: Database, rules: Rules, enabled: bool, *, series: int | None = None,
    model: str | None = None, storage: str | None = None,
) -> Rules:
    """勾选 / 取消勾选一个系列、机型或容量；只改动容量层的停用项。"""
    targets = search_targets(database, rules)
    disabled = expand_disabled(rules, targets)
    if series is not None:
        keys = {f"{t['model']}|{s}" for t in targets if t["series"] == series for s in t["storages"]}
        disabled.discard(f"@series:{series}")
    elif model is not None:
        target = next((t for t in targets if t["model"] == model), None)
        storages = [storage] if storage else (target["storages"] if target else [])
        keys = {f"{model}|{value}" for value in storages}
        if not storage:
            disabled.discard(model)
    else:
        raise ValueError("需要指定系列或机型")
    if enabled:
        disabled -= keys
    else:
        disabled |= keys
    rules.disabled_models = sorted(disabled)
    return rules


def search_units(database: Database, rules: Rules) -> list[dict[str, Any]]:
    """每款机型一个搜索单元；启用容量在同一搜索结果页内依次筛选。"""
    return [
        {
            "model": target["model"], "storage": None, "keyword": target["keyword"],
            "storages": [unit["storage"] for unit in target["units"] if unit["enabled"]],
        }
        for target in search_targets(database, rules)
        if target["enabled"] and any(unit["enabled"] for unit in target["units"])
    ]


def _storage_value(storage: str) -> int:
    return int(storage[:-1]) * 1024 if storage.endswith("T") else int(storage)
