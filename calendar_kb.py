"""
Модуль выбора даты через Telegram inline-кнопки.
Возвращает date объект либо None (без срока).
"""
from datetime import date, timedelta
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]


def kb_date_pick(year: int = None, month: int = None) -> InlineKeyboardMarkup:
    """
    Главный экран выбора даты.
    Быстрые кнопки + сетка на 14 дней вперёд + кнопка «без срока».
    """
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    buttons = []

    # Быстрые кнопки
    tomorrow = today + timedelta(days=1)
    buttons.append([
        InlineKeyboardButton(
            text=f"Сегодня ({today.strftime('%d.%m')})",
            callback_data=f"cal_pick_{today.isoformat()}"
        ),
        InlineKeyboardButton(
            text=f"Завтра ({tomorrow.strftime('%d.%m')})",
            callback_data=f"cal_pick_{tomorrow.isoformat()}"
        ),
    ])

    # Заголовок недели
    buttons.append([InlineKeyboardButton(
        text=f"── {MONTHS_RU[month]} {year} ──",
        callback_data="cal_ignore"
    )])

    # Названия дней
    buttons.append([
        InlineKeyboardButton(text=d, callback_data="cal_ignore")
        for d in WEEKDAYS_RU
    ])

    # Сетка: 2 недели начиная с сегодня
    # Находим начало строки (понедельник недели today)
    start = today - timedelta(days=today.weekday())
    row = []
    for i in range(14):
        day = start + timedelta(days=i)
        if day < today:
            row.append(InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
        else:
            label = day.strftime("%d")
            if day == today:
                label = f"[{label}]"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"cal_pick_{day.isoformat()}"
            ))
        if len(row) == 7:
            buttons.append(row)
            row = []
    if row:
        # Дополняем до 7
        while len(row) < 7:
            row.append(InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
        buttons.append(row)

    # Навигация по месяцам
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    buttons.append([
        InlineKeyboardButton(text="◀ Пред. месяц", callback_data=f"cal_nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton(text="След. месяц ▶", callback_data=f"cal_nav_{next_year}_{next_month}"),
    ])

    buttons.append([
        InlineKeyboardButton(text="✏️ Ввести дату вручную", callback_data="cal_manual"),
        InlineKeyboardButton(text="∞ Без срока", callback_data="cal_none"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def parse_date_input(text: str):
    """
    Пробует распарсить дату из текста пользователя.
    Поддерживает: 25.03, 25.03.2025, 2025-03-25
    Возвращает date или None.
    """
    import re
    text = text.strip()

    # Формат ДД.ММ или ДД.ММ.ГГГГ
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", text)
    if m:
        day, mon, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        if yr is None:
            yr = date.today().year
        else:
            yr = int(yr)
            if yr < 100:
                yr += 2000
        try:
            return date(yr, mon, day)
        except ValueError:
            return None

    # ISO формат
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    return None


def format_deadline(d: date) -> str:
    """Красивый текст дедлайна для отображения"""
    today = date.today()
    diff = (d - today).days
    base = d.strftime("%d.%m.%Y")
    if diff == 0:
        return f"{base} (сегодня)"
    elif diff == 1:
        return f"{base} (завтра)"
    elif diff < 0:
        return f"{base} (просрочено на {-diff} дн.)"
    else:
        return f"{base} (через {diff} дн.)"
