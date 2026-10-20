"""地区数据：生成 App 区域筛选路径，以及判断商品卡片上的地区是否属于目标城市。"""

from __future__ import annotations

PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)

# 常用城市所属省份，用于生成默认的 App 区域筛选点击路径。
CITY_PROVINCE = {
    "厦门": "福建", "福州": "福建", "泉州": "福建", "漳州": "福建", "莆田": "福建",
    "龙岩": "福建", "三明": "福建", "南平": "福建", "宁德": "福建",
    "广州": "广东", "深圳": "广东", "东莞": "广东", "佛山": "广东", "珠海": "广东",
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "南京": "江苏", "苏州": "江苏",
    "无锡": "江苏", "成都": "四川", "武汉": "湖北", "长沙": "湖南", "郑州": "河南",
    "西安": "陕西", "济南": "山东", "青岛": "山东", "合肥": "安徽", "南昌": "江西",
    "南宁": "广西", "昆明": "云南", "贵阳": "贵州", "海口": "海南", "沈阳": "辽宁",
    "大连": "辽宁", "长春": "吉林", "哈尔滨": "黑龙江", "石家庄": "河北", "太原": "山西",
    "兰州": "甘肃", "呼和浩特": "内蒙古", "乌鲁木齐": "新疆",
    "北京": "北京", "上海": "上海", "天津": "天津", "重庆": "重庆",
}

# 卡片上有时只显示区县；按需补充其他城市。
CITY_DISTRICTS = {
    "厦门": ("思明", "湖里", "集美", "海沧", "同安", "翔安", "鼓浪屿"),
}


def normalize_city(city: str) -> str:
    return city.strip().removesuffix("市")


def default_region_path(city: str) -> str:
    """区域面板里依次选省、市，再点右下角「查看」才会生效。"""
    city = normalize_city(city)
    if not city:
        return ""
    province = CITY_PROVINCE.get(city)
    if province and province != city:
        return f"区域 > {province} > {city} > 查看"
    return f"区域 > {city} > 查看"


def region_status(region: str, city: str) -> str:
    """返回 ``match``（属于目标城市）、``other``（明确是别处）或 ``unknown``。"""
    region, city = region.strip(), normalize_city(city)
    if not region or not city:
        return "unknown"
    if city in region or any(district in region for district in CITY_DISTRICTS.get(city, ())):
        return "match"
    for other_city in CITY_PROVINCE:
        if other_city != city and other_city in region:
            return "other"
    province = CITY_PROVINCE.get(city)
    for name in PROVINCES:
        if region.startswith(name):
            # 只写了本省（例如「福建」）无法确认城市；写了其他省份则一定不是。
            return "unknown" if name == province else "other"
    return "unknown"
