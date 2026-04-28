# utils/screenshot_ocr.py
"""
Анализ скриншотов результатов игры через Tesseract OCR (pytesseract).

Логика:
    1. Скачиваем изображение по URL вложения Discord.
    2. OCR -> получаем текст.
    3. СТРОГАЯ ВАЛИДАЦИЯ: ищем ники всех игроков комнаты в тексте.
       Если недостаточно найдено - возвращаем ValidationError (не тот скрин).
    4. Ищем ПОБЕДА / ПОРАЖЕНИЕ -> определяем победителя.
    5. Возвращаем ScreenshotResult, ValidationError, или None.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("bot.ocr")

# Ленивая загрузка зависимостей
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.warning("pytesseract / Pillow не установлены.")

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    log.warning("aiohttp не установлен.")


# ── Результаты ──────────────────────────────────────────────────────────────────

@dataclass
class ScreenshotResult:
    """OCR успешно распознал результат матча."""
    winner_team: int
    confidence: str             # 'high' | 'low'
    raw_verdict: str            # 'win_top' | 'win_bottom'
    matched_players: list[str] = field(default_factory=list)


@dataclass
class ValidationError:
    """Скрин не прошёл валидацию - не те игроки / не тот формат."""
    reason: str
    found_players: list[str] = field(default_factory=list)
    missing_players: list[str] = field(default_factory=list)
    expected_count: int = 0
    found_count: int = 0


# ── Вспомогательные функции ─────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def _find_verdict(text: str) -> Optional[str]:
    upper = text.upper()
    win_patterns  = [r"ПОБЕДА", r"П0БЕДА", r"ПОБЕДА!", r"VICTORY", r"WIN"]
    lose_patterns = [r"ПОРАЖЕНИЕ", r"П0РАЖЕНИЕ", r"DEFEAT", r"LOSS", r"LOSE"]

    found_win  = any(re.search(p, upper) for p in win_patterns)
    found_lose = any(re.search(p, upper) for p in lose_patterns)

    if found_win and not found_lose:
        return "win_top"
    if found_lose and not found_win:
        return "win_bottom"
    if found_win and found_lose:
        win_pos  = min((m.start() for p in win_patterns  for m in re.finditer(p, upper)), default=9999)
        lose_pos = min((m.start() for p in lose_patterns for m in re.finditer(p, upper)), default=9999)
        return "win_top" if win_pos < lose_pos else "win_bottom"
    return None


def _levenshtein(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 4:
        return 999
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev[j - 1] + cost)
    return dp[n]


def _match_players(ocr_text: str, players: list[dict]) -> list[str]:
    matched   = []
    ocr_lines = [_normalize(line) for line in ocr_text.splitlines() if line.strip()]
    ocr_full  = _normalize(ocr_text)

    for p in players:
        nick      = p["username"]
        nick_norm = _normalize(nick)
        if not nick_norm:
            continue

        if nick_norm in ocr_full:
            matched.append(nick)
            continue

        if len(nick_norm) >= 3:
            for line in ocr_lines:
                if not line:
                    continue
                if _levenshtein(nick_norm, line) <= max(1, len(nick_norm) // 5):
                    matched.append(nick)
                    break
                if len(line) > len(nick_norm) and nick_norm in line:
                    matched.append(nick)
                    break

    return matched


def _determine_winner_team(verdict: str, players: list[dict], matched: list[str]) -> tuple[int, str]:
    matched_set = set(matched)
    t1_found = [p for p in players if p["team"] == 1 and p["username"] in matched_set]
    t2_found = [p for p in players if p["team"] == 2 and p["username"] in matched_set]
    total_t1 = max(len([p for p in players if p["team"] == 1]), 1)
    total_t2 = max(len([p for p in players if p["team"] == 2]), 1)

    has_good_match = (
        (len(t1_found) / total_t1 >= 0.5 or len(t2_found) / total_t2 >= 0.5)
        and (len(t1_found) + len(t2_found) >= 2)
    )

    if verdict == "win_top":
        if t1_found and t2_found:
            winner_team = 1 if len(t1_found) >= len(t2_found) else 2
        elif t1_found:
            winner_team = 1
        elif t2_found:
            winner_team = 2
        else:
            winner_team = 1
    else:
        if t1_found and t2_found:
            winner_team = 2 if len(t1_found) >= len(t2_found) else 1
        elif t1_found:
            winner_team = 2
        elif t2_found:
            winner_team = 1
        else:
            winner_team = 2

    return winner_team, ("high" if has_good_match else "low")


def _validate_players(players: list[dict], matched: list[str]) -> Optional[ValidationError]:
    """
    Строгая проверка: все ли нужные игроки присутствуют на скрине.
    Возвращает ValidationError если скрин не подходит, иначе None.
    """
    team1 = [p for p in players if p["team"] == 1]
    team2 = [p for p in players if p["team"] == 2]
    total_expected = len(team1) + len(team2)

    if total_expected == 0:
        return ValidationError(
            reason="Команды в комнате не сформированы.",
            expected_count=0,
            found_count=0,
        )

    matched_set = set(matched)
    t1_found    = [p for p in team1 if p["username"] in matched_set]
    t2_found    = [p for p in team2 if p["username"] in matched_set]
    found_count = len(t1_found) + len(t2_found)
    size        = len(team1)

    missing = [p["username"] for p in players
               if p["username"] not in matched_set and p["team"] in (1, 2)]

    # Правило 1: ни одного игрока из одной из команд
    if not t1_found:
        return ValidationError(
            reason="На скрине не найдено ни одного игрока из Команды 1. Скорее всего это не тот матч.",
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )
    if not t2_found:
        return ValidationError(
            reason="На скрине не найдено ни одного игрока из Команды 2. Скорее всего это не тот матч.",
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Правило 2: для 1v1 и 2v2 - нужны ВСЕ игроки
    if size <= 2:
        if len(t1_found) < len(team1) or len(t2_found) < len(team2):
            missing_str = ", ".join(missing)
            return ValidationError(
                reason=(
                    "Формат " + str(size) + "v" + str(size)
                    + ": на скрине должны быть все " + str(total_expected) + " игрока. "
                    + "Найдено только " + str(found_count) + ". "
                    + "Не найдены: " + missing_str + "."
                ),
                found_players=list(matched_set),
                missing_players=missing,
                expected_count=total_expected,
                found_count=found_count,
            )

    # Правило 3: для 3v3 и 4v4 - минимум 50% каждой команды
    t1_ratio = len(t1_found) / max(len(team1), 1)
    t2_ratio = len(t2_found) / max(len(team2), 1)

    if t1_ratio < 0.5:
        t1_missing = ", ".join(p["username"] for p in team1 if p["username"] not in matched_set)
        return ValidationError(
            reason=(
                "Из Команды 1 (" + str(len(team1)) + " игроков) на скрине найдено только "
                + str(len(t1_found)) + ". Не найдены: " + t1_missing + "."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )
    if t2_ratio < 0.5:
        t2_missing = ", ".join(p["username"] for p in team2 if p["username"] not in matched_set)
        return ValidationError(
            reason=(
                "Из Команды 2 (" + str(len(team2)) + " игроков) на скрине найдено только "
                + str(len(t2_found)) + ". Не найдены: " + t2_missing + "."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Правило 4: общий порог 60%
    total_ratio = found_count / max(total_expected, 1)
    if total_ratio < 0.6:
        missing_str = ", ".join(missing)
        return ValidationError(
            reason=(
                "На скрине найдено только " + str(found_count) + " из " + str(total_expected)
                + " игроков (" + str(int(total_ratio * 100)) + "%). "
                + "Не найдены: " + missing_str + "."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    return None  # всё ок


# ── Основная публичная функция ──────────────────────────────────────────────────

async def analyze_screenshot(
    image_url: str,
    players: list[dict],
) -> "ScreenshotResult | ValidationError | None":
    """
    Возвращает:
      ScreenshotResult  - скрин верный, результат определён
      ValidationError   - скрин не от этой игры (не те игроки)
      None              - OCR недоступен или не смог прочитать изображение
    """
    if not _OCR_AVAILABLE or not _AIOHTTP_AVAILABLE:
        return None

    try:
        image_data = await _download_image(image_url)
    except Exception as e:
        log.warning("OCR: не удалось скачать изображение: %s", e)
        return None

    try:
        ocr_text = await asyncio.get_event_loop().run_in_executor(None, _run_ocr, image_data)
    except Exception as e:
        log.warning("OCR: ошибка при распознавании: %s", e)
        return None

    log.warning("OCR FULL TEXT:\n%s", ocr_text)

    if not ocr_text or not ocr_text.strip():
        return None

    # 1. Ищем ники игроков
    matched = _match_players(ocr_text, players)
    log.info("OCR matched players: %s", matched)

    # 2. Строгая валидация ДО проверки результата
    err = _validate_players(players, matched)
    if err is not None:
        log.info("OCR validation failed: %s (found %d/%d)", err.reason, err.found_count, err.expected_count)
        return err

    # 3. Ищем ПОБЕДА / ПОРАЖЕНИЕ
    verdict = _find_verdict(ocr_text)
    if verdict is None:
        log.debug("OCR: не найдено ПОБЕДА/ПОРАЖЕНИЕ")
        return None  # игроки нашлись, но результат неясен -> голосование

    # 4. Определяем победителя
    winner_team, confidence = _determine_winner_team(verdict, players, matched)
    log.info("OCR result: winner_team=%d confidence=%s verdict=%s matched=%s",
             winner_team, confidence, verdict, matched)

    return ScreenshotResult(
        winner_team=winner_team,
        confidence=confidence,
        raw_verdict=verdict,
        matched_players=matched,
    )


async def _download_image(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _preprocess_image(img) -> list:
    from PIL import ImageOps, ImageEnhance
    results = []
    w, h = img.size
    img_big = img.resize((w * 2, h * 2), Image.LANCZOS)
    gray = img_big.convert("L")

    results.append(ImageOps.invert(gray))
    results.append(ImageEnhance.Sharpness(ImageEnhance.Contrast(gray).enhance(3.0)).enhance(2.0))
    results.append(gray.point(lambda p: 255 if p > 100 else 0))
    results.append(gray.point(lambda p: 0 if p > 100 else 255))

    top_h   = max(60, h // 4)
    top_big = img.crop((0, 0, w, top_h)).resize((w * 3, top_h * 3), Image.LANCZOS)
    top_gray = top_big.convert("L")
    results.append(ImageOps.invert(top_gray))
    results.append(top_gray)

    return results


def _run_ocr(image_data: bytes) -> str:
    img = Image.open(io.BytesIO(image_data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    configs = [
        r"--oem 3 --psm 6 -l rus+eng",
        r"--oem 3 --psm 3 -l rus+eng",
        r"--oem 3 --psm 11 -l rus+eng",
    ]
    win_keywords = ("ПОБЕДА", "ПОРАЖЕНИЕ", "VICTORY", "DEFEAT")
    all_texts = []

    for variant in _preprocess_image(img):
        for cfg in configs:
            try:
                text = pytesseract.image_to_string(variant, config=cfg)
                if not text.strip():
                    continue
                all_texts.append(text)
                if any(kw in text.upper() for kw in win_keywords):
                    return "\n".join(all_texts)
            except Exception:
                continue

    return "\n".join(all_texts) if all_texts else ""
