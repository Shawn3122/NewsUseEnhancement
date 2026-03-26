# -*- coding: utf-8 -*-
"""
新聞內文 post-processing 清洗模組。

在擷取引擎成功取得原始文字後，移除殘留的非正文內容：
社群按鈕、推薦區塊、廣告文字、Cookie 提示等。

所有清洗關鍵字定義在 config.py，方便日後新增。
"""
from __future__ import annotations

import config


def clean_content(text: str) -> str:
    """依序套用所有清洗規則，回傳清洗後的文字。"""
    if not text:
        return text
    text = _rule_a_tail_truncation(text)
    text = _rule_b_paragraph_removal(text)
    text = _rule_c_social_removal(text)
    text = _rule_d_trailing_list_cleanup(text)
    # 清理多餘空行（連續 3 個以上換行合併為 2 個）
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _rule_a_tail_truncation(text: str) -> str:
    """Rule A: 遇到截斷關鍵字時，移除該行及之後所有內容。"""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for kw in config.TAIL_TRUNCATION_KEYWORDS:
            if kw in line:
                return "\n".join(lines[:i]).rstrip()
    return text


def _rule_b_paragraph_removal(text: str) -> str:
    """Rule B: 移除包含指定關鍵字的整行。"""
    lines = text.split("\n")
    filtered = [
        line for line in lines
        if not any(kw in line for kw in config.PARAGRAPH_REMOVAL_KEYWORDS)
    ]
    return "\n".join(filtered)


def _rule_c_social_removal(text: str) -> str:
    """Rule C: 移除社群元素。完整片語全文匹配；短詞僅文末 200 字內匹配。"""
    if not text:
        return text

    tail_start = max(0, len(text) - 200)
    lines = text.split("\n")
    filtered = []
    char_pos = 0

    for line in lines:
        line_start = char_pos
        char_pos += len(line) + 1  # +1 for \n

        # 完整片語：全文任何位置都安全移除
        if any(p in line for p in config.SOCIAL_PATTERNS_EXACT):
            continue

        # 短詞/模糊詞：僅在文末 200 字範圍內移除
        if line_start >= tail_start and config.SOCIAL_PATTERNS_TAIL_ONLY:
            if any(p in line for p in config.SOCIAL_PATTERNS_TAIL_ONLY):
                continue

        filtered.append(line)

    return "\n".join(filtered)


def _rule_d_trailing_list_cleanup(text: str) -> str:
    """Rule D: 移除文末連續 ≥ N 行以 '- ' 開頭的推薦列表。"""
    lines = text.split("\n")

    # 先去除尾部空行
    while lines and not lines[-1].strip():
        lines.pop()

    # 從尾部計算連續 "- " 開頭的行數
    count = 0
    for line in reversed(lines):
        if line.startswith("- "):
            count += 1
        else:
            break

    if count >= config.TRAILING_LIST_MIN_LINES:
        return "\n".join(lines[:-count]).rstrip()
    return text
