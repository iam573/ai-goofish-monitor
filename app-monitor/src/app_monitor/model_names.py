"""Canonical marketing names shared by OCR parsing and stored-data cleanup."""

import re


def strip_hardware_code(value: str) -> str:
    # 报价 OCR 可能把机型与末尾的 A 型号代码读进同一文本框。
    clean = re.sub(r"(?:[\s|｜（(]*A\s*\d{4}[\s）)]*)+$", "", value, flags=re.I).strip(" \t\r\n|｜")
    return clean or value


def normalize_model(value: str) -> str:
    text = strip_hardware_code(value).upper().strip()
    text = re.sub(r"\bIPHONE\b|苹果", "", text)
    text = text.replace("Ｐ", "P").replace("Ｍ", "M")
    text = re.sub(r"(?<=\d)(?=PRO\b)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\bPRO\s*MAX\b", "Pro Max", text, flags=re.I)
    text = re.sub(r"\bPRO\b", "Pro", text, flags=re.I)
    text = re.sub(r"\bPLUS\b", "Plus", text, flags=re.I)
    text = re.sub(r"\bMINI\b", "Mini", text, flags=re.I)
    text = re.sub(r"\bAIR\b", "Air", text, flags=re.I)
    text = re.sub(r"\bMAX\b", "Max", text, flags=re.I)
    if re.fullmatch(r"\d{1,2}[Ee]", text):
        return text[:-1] + "e"
    return text
