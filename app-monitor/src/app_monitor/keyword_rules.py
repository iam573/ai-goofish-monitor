"""Shared editable vocabulary. Built-in words also recognize their common grammatical forms."""
from __future__ import annotations

import re
import unicodedata

# A displayed keyword owns its built-in variants; custom keywords are literal text.
CONDITION_TERMS = {
    'screen_watermark': {
        '水印': '水印',
        '烧屏': '烧屏',
        '压伤': '压伤',
    },
    'screen_broken': {
        '屏幕破损': '(?:内屏|外屏|屏幕|屏幕玻璃)(?:有|出现|已经)?(?:破|碎|裂)',
        '碎屏': '碎屏',
        '屏裂': '屏裂',
        '外爆': '外爆',
        '内爆': '内爆',
        '漏液': '漏液',
    },
    'function_fault': {
        '不开机': '不开机',
        '不能开机': '不能开机',
        '无法开机': '无法开机',
        '无法插卡或通话': '无法(?:插卡|通话|联网)',
        '插不了卡': '插不了卡',
        '面容失灵': '面容(?:损坏|失灵|不可用)',
        '无面容': '无面容',
        '触摸失灵': '触摸(?:失灵|故障)',
        '触控失灵': '触控(?:失灵|故障)',
        '功能异常': '功能(?:异常|故障)',
        '坏机': '坏机',
        '安卓爆改': '安卓爆改',
        '爆改机': '爆改机',
    },
    'locked': {
        'ID锁': '(?:id|激活|账户|账号)锁',
        'ID机': '(?:id|激活)机',
        '卡贴机': '卡贴(?:机)?',
        '监管机': '监管机',
        '有锁': '有锁',
        '黑解': '黑解',
        '扩容机': '扩容机',
    },
    'repaired': {
        '拆修': '拆修',
        '拆机': '拆机',
        '维修': '维修',
        '修过': '修过',
        '换过配件': '换过?(?:屏|电池|壳|后盖|主板)',
        '更换配件': '更换过?(?:屏幕|电池|后盖|主板)',
        '翻新': '翻新',
        '进水': '进水',
        '泡水': '泡水',
        '扩容': '扩容',
        '爆改': '爆改',
    },
    'screen_replaced': {
        '换屏': '换(?:过|了)?屏',
        '更换屏幕': '更换(?:过|了)?屏幕',
        '第三方屏': '第三方屏',
        '组装屏': '组装屏',
        '国产屏': '国产屏',
        '非原装屏': '非原装屏',
        '华星屏': '华星屏',
        '天马屏': '天马屏',
    },
    'battery_replaced': {
        '换电池': '(?:更换|换)(?:过|了)?(?:原装|原厂|官方|品胜|德赛|第三方|非原装)?(?:的)?电池',
        '第三方电池': '第三方电池',
        '电池维修': '电池维修',
    },
    'water_damage': {
        '进水': '进水',
        '泡水': '泡水',
        '水泡': '水泡',
    },
    'back_glass_broken': {
        '后盖破损': '(?:后盖|后玻璃|背板)(?:有|已经)?(?:破|碎|裂)',
    },
    'screen_original': {
        '换原装屏': '(?:换|更换)(?:过|了|的是|的)?(?:官方|原装|原厂|拆机)(?:原装|原厂)?屏',
        '原装屏换过': '(?:原装|原厂|拆机)屏(?:幕)?(?:已|有)?更?换过',
    },
    'screen_popup': {
        '屏幕弹窗': '(?:屏幕?|显示屏)(?:有|会|出现)?弹窗',
        '弹屏': '弹屏',
        '屏幕未知部件': '屏幕?(?:未知|不明|无法验证)(?:部件)?',
    },
    'battery_popup': {
        '电池弹窗': '电池(?:有|会|出现)?弹窗',
        '弹电': '弹电',
        '电池未知部件': '电池(?:未知|不明|无法验证)(?:部件)?',
    },
    'battery_cell_replaced': {
        '换电芯': '(?:更换|换)(?:过|了)?(?:全新|新)?(?:原装|原厂|官方|第三方)?(?:的)?电芯',
        '新电芯': '(?:全新|新)(?:原装|原厂)?电芯',
        '电芯已更换': '电芯(?:已更换|更换过|换过|换新|新换)',
    },
    'battery_third_party': {
        '第三方电池': '(?:第三方|非原装|组装|国产|德赛|品胜|飞毛腿|诺希|倍思)(?:的)?(?:电池|电芯)',
    },
    'battery_swollen': {
        '鼓包': '(?:电池)?鼓包',
        '电池膨胀': '电池(?:有|出现)?(?:鼓起|膨胀)',
    },
    'face_id_fault': {
        '面容失灵': '面容(?:损坏|失灵|不可用|坏|故障)',
        '无面容': '无面容',
        'Face ID失灵': 'face\\s*id(?:失灵|损坏)',
    },
    'rear_camera_replaced': {
        '换后摄': '(?:更换|换)(?:过|了)?(?:后置?|后摄)(?:摄像头|镜头|像头)?',
        '后摄换过': '后(?:置摄像头|摄像头|镜头|摄|像头)(?:更换|换过|换了)',
    },
    'camera_watermark': {
        '镜头水印': '(?:摄像头|镜头|像头)(?:有|出现|轻微|小)?水印',
    },
    'screen_aging': {
        '屏幕老化': '(?:屏幕|内屏|显示屏)(?:有|轻微|出现)?老化',
    },
    'front_camera_spots': {
        '前摄彩点': '(?:前置?|前摄|自拍)(?:摄像头|镜头|像头)?(?:有|出现)?(?:彩点|黑点|斑点)',
    },
    'rear_camera_spots': {
        '后摄彩点': '(?:后置?|后摄)(?:摄像头|镜头|像头)?(?:有|出现)?(?:彩点|黑点|斑点)',
    },
    'front_camera_flare': {
        '前摄蓝光': '(?:前置?|前摄|自拍)(?:摄像头|镜头|像头)?(?:有|出现)?蓝光',
    },
    'rear_camera_flare': {
        '后摄蓝光': '(?:后置?|后摄)(?:摄像头|镜头|像头)?(?:有|出现)?蓝光',
    },
    'true_tone_missing': {
        '无原彩': '无原彩',
        '原彩缺失': '原彩(?:缺失|不可用|没有|丢失)',
    },
    'full_set_missing': {
        '单机': '单机(?:出|出售)?',
        '无配件': '无配件',
        '无包装': '无(?:包装|全套)',
        '不带盒或配件': '不带(?:盒|配件)',
        '只有手机': '只有手机',
    },
}


def normalize_word(word: str) -> str:
    return unicodedata.normalize("NFKC", word).translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))).casefold().strip()


def default_condition_keywords() -> dict[str, list[str]]:
    return {key: list(terms) for key, terms in CONDITION_TERMS.items()}


def condition_patterns(key: str, keywords: dict[str, list[str]] | None = None) -> list[str]:
    terms = CONDITION_TERMS[key]
    words = list(terms) if keywords is None else keywords[key]
    builtins = {normalize_word(word): pattern for word, pattern in terms.items()}
    return [builtins.get(normalize_word(word), re.escape(normalize_word(word))) for word in words]
