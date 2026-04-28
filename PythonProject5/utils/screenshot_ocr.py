# utils/screenshot_ocr.py
"""
Анализ скриншотов результатов игры через Tesseract OCR (pytesseract).

Логика:
    1. Скачиваем изображение по URL вложения Discord.
    2. OCR → получаем текст.
    3. Ищем ПОБЕДА / ПОРАЖЕНИЕ (верхняя команда).
    4. СТРОГАЯ ВАЛИДАЦИЯ:
       а) Считаем, сколько ников из комнаты найдено на скрине.
       б) Если найдено < 50% ников — скрин не от этой игры (ValidationError).
       в) Если найденное число явно не соответствует формату — тоже ошибка.
    5. Возвращаем ScreenshotResult или ValidationError если скрин не тот.
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

# ── Ленивая загрузка зависимостей ──────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.warning(
        "pytesseract / Pillow не установлены. "
        "OCR-анализ скриншотов недоступен. "
        "Установи: pip install pytesseract pillow && apt-get install tesseract-ocr tesseract-ocr-rus"
    )

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    log.warning("aiohttp не установлен. Установи: pip install aiohttp")


# ── Результаты ──────────────────────────────────────────────────────────────────

@dataclass
class ScreenshotResult:
    """
    winner_team: 1 или 2 — какая команда (из комнаты) победила.
    confidence:  'high' | 'low' — насколько уверены в результате.
    raw_verdict: 'win_top' | 'win_bottom' — что написано на скрине относительно верхней группы.
    matched_players: список ников игроков, найденных в тексте скрина.
    """
    winner_team: int           # 1 или 2
    confidence: str            # 'high' | 'low'
    raw_verdict: str           # 'win_top' | 'win_bottom'
    matched_players: list[str] = field(default_factory=list)


@dataclass
class ValidationError:
    """
    Скрин не прошёл валидацию — скорее всего это не тот матч.
    reason: человекочитаемое объяснение для чата.
    found_players: ники, которые всё же нашлись.
    missing_players: ники из комнаты, которых НЕТ на скрине.
    """
    reason: str
    found_players: list[str] = field(default_factory=list)
    missing_players: list[str] = field(default_factory=list)
    expected_count: int = 0
    found_count: int = 0


# ── Вспомогательные функции ─────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Нижний регистр + убираем диакритику/невидимые символы."""
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def _find_verdict(text: str) -> Optional[str]:
    """
    Ищем ПОБЕДА / ПОРАЖЕНИЕ в тексте OCR.
    Возвращает:
        'win_top'    — верхняя команда ПОБЕДИЛА
        'win_bottom' — верхняя команда ПРОИГРАЛА (нижняя ПОБЕДИЛА)
        None         — не найдено
    """
    upper = text.upper()

    win_patterns = [r"ПОБЕДА", r"П0БЕДА", r"ПОБЕДА!", r"VICTORY", r"WIN"]
    lose_patterns = [r"ПОРАЖЕНИЕ", r"П0РАЖЕНИЕ", r"DEFEAT", r"LOSS", r"LOSE"]

    found_win = any(re.search(p, upper) for p in win_patterns)
    found_lose = any(re.search(p, upper) for p in lose_patterns)

    if found_win and not found_lose:
        return "win_top"
    if found_lose and not found_win:
        return "win_bottom"
    if found_win and found_lose:
        win_pos = min(
            (m.start() for p in win_patterns for m in re.finditer(p, upper)),
            default=9999,
        )
        lose_pos = min(
            (m.start() for p in lose_patterns for m in re.finditer(p, upper)),
            default=9999,
        )
        return "win_top" if win_pos < lose_pos else "win_bottom"

    return None


def _levenshtein(a: str, b: str) -> int:
    """Простая реализация расстояния Левенштейна."""
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
    """
    Ищем ники игроков из комнаты в тексте OCR.
    Учитываем кланы: [TAG] ник в игре == ник в Discord.
    Возвращаем список найденных ников.
    """
    matched = []
    ocr_lines = [_normalize(line) for line in ocr_text.splitlines() if line.strip()]
    ocr_full = _normalize(ocr_text)

    for p in players:
        nick = p["username"]
        nick_norm = _normalize(nick)

        if not nick_norm:
            continue

        # Прямое совпадение нормализованного ника в полном тексте
        if nick_norm in ocr_full:
            matched.append(nick)
            continue

        # Fuzzy: проверяем каждую строку на близость (1 ошибка на 5 символов)
        if len(nick_norm) >= 3:
            for line in ocr_lines:
                if not line:
                    continue
                # Ищем ник как подстроку строки (с учётом опечатки)
                if _levenshtein(nick_norm, line) <= max(1, len(nick_norm) // 5):
                    matched.append(nick)
                    break
                # Ищем ник внутри длинной строки (если строка длиннее ника)
                if len(line) > len(nick_norm) and nick_norm in line:
                    matched.append(nick)
                    break

    return matched


def _determine_winner_team(
    verdict: str,
    players: list[dict],
    matched: list[str],
) -> tuple[int, str]:
    """
    Определяем какая команда победила.
    Возвращает (winner_team, confidence).
    """
    matched_set = set(matched)
    t1_found = [p for p in players if p["team"] == 1 and p["username"] in matched_set]
    t2_found = [p for p in players if p["team"] == 2 and p["username"] in matched_set]

    total_t1 = len([p for p in players if p["team"] == 1])
    total_t2 = len([p for p in players if p["team"] == 2])

    t1_ratio = len(t1_found) / max(total_t1, 1)
    t2_ratio = len(t2_found) / max(total_t2, 1)
    has_good_match = (
        (t1_ratio >= 0.5 or t2_ratio >= 0.5)
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
    else:  # win_bottom
        if t1_found and t2_found:
            winner_team = 2 if len(t1_found) >= len(t2_found) else 1
        elif t1_found:
            winner_team = 2
        elif t2_found:
            winner_team = 1
        else:
            winner_team = 2

    confidence = "high" if has_good_match else "low"
    return winner_team, confidence


def _validate_players(
    players: list[dict],
    matched: list[str],
) -> Optional[ValidationError]:
    """
    Строгая валидация: проверяем, что найденные ники соответствуют комнате.

    Правила:
    1. Все игроки должны иметь team=1 или team=2 (не 0).
    2. Должны быть найдены ники хотя бы из обеих команд.
    3. Минимальный порог: не менее 50% игроков от каждой команды найдено.
       Для маленьких комнат (1v1, 2v2): оба должны присутствовать.
    4. Если не найден хотя бы 1 игрок команды — ValidationError.

    Возвращает ValidationError или None если всё ок.
    """
    team1 = [p for p in players if p["team"] == 1]
    team2 = [p for p in players if p["team"] == 2]

    total_expected = len(team1) + len(team2)
    if total_expected == 0:
        return ValidationError(
            reason="Комнаты нет или команды не сформированы.",
            expected_count=0,
            found_count=0,
        )

    matched_set = set(matched)
    t1_found = [p for p in team1 if p["username"] in matched_set]
    t2_found = [p for p in team2 if p["username"] in matched_set]
    found_count = len(t1_found) + len(t2_found)

    size = len(team1)  # размер одной команды (они равны)

    missing = [
        p["username"] for p in players
        if p["username"] not in matched_set and p["team"] in (1, 2)
    ]

    # Правило 1: ни одного игрока из одной из команд — точно не тот скрин
    if not t1_found:
        return ValidationError(
            reason=(
                f"На скрине не найдено ни одного игрока из **Команды 1**. "
                f"Скорее всего это не тот матч."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )
    if not t2_found:
        return ValidationError(
            reason=(
                f"На скрине не найдено ни одного игрока из **Команды 2**. "
                f"Скорее всего это не тот матч."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Правило 2: для 1v1 и 2v2 — нужны все игроки
    if size <= 2:
        if len(t1_found) < len(team1) or len(t2_found) < len(team2):
            return ValidationError(
                reason=(
                    f"Формат {size}v{size}: на скрине должны быть все {total_expected} игрока. "
                    f"Найдено только {found_count}. "
                    f"Не найдены: {', '.join(f'**{n}**' for n in missing)}."
                ),
                found_players=list(matched_set),
                missing_players=missing,
                expected_count=total_expected,
                found_count=found_count,
            )

    # Правило 3: для 3v3 и 4v4 — минимум 50% каждой команды
    t1_ratio = len(t1_found) / max(len(team1), 1)
    t2_ratio = len(t2_found) / max(len(team2), 1)

    if t1_ratio < 0.5:
        return ValidationError(
            reason=(
                f"Из Команды 1 ({len(team1)} игроков) на скрине найдено только "
                f"{len(t1_found)}. Не найдены: "
                f"{', '.join(f'**{p[\"username\"]}**' for p in team1 if p[\"username\"] not in matched_set)}."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )
    if t2_ratio < 0.5:
        return ValidationError(
            reason=(
                f"Из Команды 2 ({len(team2)} игроков) на скрине найдено только "
                f"{len(t2_found)}. Не найдены: "
                f"{', '.join(f'**{p[\"username\"]}**' for p in team2 if p[\"username\"] not in matched_set)}."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Правило 4: общий порог — не менее 60% всех игроков
    total_ratio = found_count / max(total_expected, 1)
    if total_ratio < 0.6:
        return ValidationError(
            reason=(
                f"На скрине найдено только {found_count} из {total_expected} игроков ({int(total_ratio*100)}%). "
                f"Скрин должен содержать результаты этого матча. "
                f"Не найдены: {', '.join(f'**{n}**' for n in missing)}."
            ),
            found_players=list(matched_set),
            missing_players=missing,
            expected_count=total_expected,
            found_count=found_count,
        )

    return None  # Всё ок


# ── Основная публичная функция ──────────────────────────────────────────────────

async def analyze_screenshot(
    image_url: str,
    players: list[dict],
) -> ScreenshotResult | ValidationError | None:
    """
    Скачивает изображение по URL и анализирует результат игры.

    players — список dict с ключами 'discord_id', 'username', 'team'.

    Возвращает:
      - ScreenshotResult  — если всё распознано и прошло валидацию
      - ValidationError   — если скрин не соответствует комнате (не тот матч,
                            не те игроки, не тот формат)
      - None              — если OCR недоступен или не смог прочитать изображение
    """
    if not _OCR_AVAILABLE or not _AIOHTTP_AVAILABLE:
        return None

    try:
        image_data = await _download_image(image_url)
    except Exception as e:
        log.warning(f"OCR: не удалось скачать изображение: {e}")
        return None

    try:
        ocr_text = await asyncio.get_event_loop().run_in_executor(
            None, _run_ocr, image_data
        )
    except Exception as e:
        log.warning(f"OCR: ошибка при распознавании: {e}")
        return None

    log.warning(f"OCR FULL TEXT:\n{ocr_text}")

    if not ocr_text or not ocr_text.strip():
        log.debug("OCR: пустой результат")
        return None

    # Сопоставляем ники — делаем это ДО проверки ПОБЕДА/ПОРАЖЕНИЕ
    matched = _match_players(ocr_text, players)

    log.info(f"OCR matched players: {matched}")

    # ── Строгая валидация: правильный ли это скрин? ────────────────────────
    validation_err = _validate_players(players, matched)
    if validation_err is not None:
        log.info(
            f"OCR validation failed: {validation_err.reason} "
            f"(found {validation_err.found_count}/{validation_err.expected_count})"
        )
        return validation_err

    # ── Ищем ПОБЕДА / ПОРАЖЕНИЕ ────────────────────────────────────────────
    verdict = _find_verdict(ocr_text)
    if verdict is None:
        log.debug("OCR: не найдено ПОБЕДА/ПОРАЖЕНИЕ в тексте")
        # Игроки нашлись, но результат не ясен → возвращаем None (→ голосование)
        return None

    # ── Определяем победителя ─────────────────────────────────────────────
    winner_team, confidence = _determine_winner_team(verdict, players, matched)

    result = ScreenshotResult(
        winner_team=winner_team,
        confidence=confidence,
        raw_verdict=verdict,
        matched_players=matched,
    )
    log.info(
        f"OCR result: winner_team={winner_team}, confidence={confidence}, "
        f"verdict={verdict}, matched={matched}"
    )
    return result


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
    inverted = ImageOps.invert(gray)
    results.append(inverted)

    enhanced = ImageEnhance.Contrast(gray).enhance(3.0)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(2.0)
    results.append(enhanced)

    thresh = gray.point(lambda p: 255 if p > 100 else 0)
    results.append(thresh)

    thresh_inv = gray.point(lambda p: 0 if p > 100 else 255)
    results.append(thresh_inv)

    top_h = max(60, h // 4)
    top_crop = img.crop((0, 0, w, top_h))
    top_big = top_crop.resize((w * 3, top_h * 3), Image.LANCZOS)
    top_gray = top_big.convert("L")
    top_inv = ImageOps.invert(top_gray)
    results.append(top_inv)
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
