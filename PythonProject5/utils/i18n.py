# utils/i18n.py

_STRINGS: dict[str, dict[str, str]] = {
    # ── Register ─────────────────────────────────────────────────────────────
    "register_already": {
        "ru": "Ты уже зарегистрирован как **{username}** (ELO: {elo}).",
        "en": "You are already registered as **{username}** (ELO: {elo}).",
    },
    "register_nick_taken": {
        "ru": "⚠️ Ник **{nick}** уже занят. Выбери другой.",
        "en": "⚠️ Nickname **{nick}** is already taken. Choose another.",
    },
    "register_ok_title": {
        "ru": "✅ Регистрация прошла успешно!",
        "en": "✅ Registration successful!",
    },
    "register_ok_desc": {
        "ru": "Добро пожаловать, **{nick}**! Удачи в играх 🎮",
        "en": "Welcome, **{nick}**! Good luck in your games 🎮",
    },
    "register_error": {
        "ru": "❌ Произошла ошибка при регистрации. Попробуй ещё раз.",
        "en": "❌ An error occurred during registration. Please try again.",
    },

    # ── Rename ───────────────────────────────────────────────────────────────
    "rename_usage": {
        "ru": "Использование: `!rename <новый_ник>`",
        "en": "Usage: `!rename <new_nickname>`",
    },
    "rename_length": {
        "ru": "⚠️ Ник должен быть от 2 до 32 символов.",
        "en": "⚠️ Nickname must be between 2 and 32 characters.",
    },
    "rename_not_registered": {
        "ru": "❌ Ты не зарегистрирован. Используй `!register <ник>`.",
        "en": "❌ You are not registered. Use `!register <nickname>`.",
    },
    "rename_taken": {
        "ru": "⚠️ Ник **{nick}** уже занят. Выбери другой.",
        "en": "⚠️ Nickname **{nick}** is already taken. Choose another.",
    },
    "rename_ok_server": {
        "ru": "✅ Ник изменён на **{nick}** (и на сервере тоже).",
        "en": "✅ Nickname changed to **{nick}** (server nickname updated too).",
    },
    "rename_ok_manual": {
        "ru": "✅ Ник изменён на **{nick}** (смени отображаемое имя вручную — нет прав).",
        "en": "✅ Nickname changed to **{nick}** (update your server nickname manually — missing permissions).",
    },
}

_FALLBACK = "ru"


def t(key: str, lang: str, **kwargs) -> str:
    """
    Return a translated string for *key* in *lang*.

    Falls back to Russian if the key or language is missing.
    Any extra keyword arguments are interpolated into the string with str.format().
    """
    bucket = _STRINGS.get(key)
    if bucket is None:
        return key  # unknown key — return as-is so bugs are obvious

    text = bucket.get(lang) or bucket.get(_FALLBACK) or key
    return text.format(**kwargs) if kwargs else text