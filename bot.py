import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from calendar_kb import kb_date_pick, parse_date_input, format_deadline

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")

USERS = {
    "445734805": {"name": "Артём", "role": "admin"},
    "1853672896": {"name": "Бухгалтер розницы", "role": "shop", "shop": "buh"},
    "1888343715": {"name": "Маг. 1", "role": "shop", "shop": "mag1"},
    "5813766989": {"name": "Маг. 2", "role": "shop", "shop": "mag2"},
    "6114755106": {"name": "Маг. 3", "role": "shop", "shop": "mag3"},
    "6056563458": {"name": "Маг. 5", "role": "shop", "shop": "mag5"},
}

ADMIN_ID = "445734805"

tasks: dict = {}
task_counter = 0


def next_task_id():
    global task_counter
    task_counter += 1
    return task_counter


def get_user(user_id):
    return USERS.get(str(user_id))


def is_admin(user_id):
    u = get_user(user_id)
    return u and u["role"] == "admin"


# ─── FSM ─────────────────────────────────────────────────────────────────────
class NewTask(StatesGroup):
    choosing_assignees = State()
    entering_text = State()
    choosing_deadline = State()
    entering_manual_deadline = State()

class ShopTask(StatesGroup):
    entering_text = State()
    choosing_deadline = State()
    entering_manual_deadline = State()

class ShopQuestion(StatesGroup):
    entering_text = State()

class ShopReport(StatesGroup):
    waiting_report = State()

class ReturnTask(StatesGroup):
    entering_remarks = State()

class EditTask(StatesGroup):
    entering_new_text = State()
    entering_new_deadline = State()
    choosing_new_deadline = State()
    entering_manual_new_deadline = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────
def kb_main_admin():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Новая задача")],
            [KeyboardButton(text="🕐 В работе"), KeyboardButton(text="🔍 На проверке")],
            [KeyboardButton(text="✅ Выполненные"), KeyboardButton(text="📊 Отчёт")],
        ],
        resize_keyboard=True, persistent=True
    )


def kb_main_shop():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои задачи")],
            [KeyboardButton(text="📌 Поставить задачу"), KeyboardButton(text="❓ Вопрос руководителю")],
        ],
        resize_keyboard=True, persistent=True
    )


def kb_assignee_select(selected: list):
    buttons = []
    for uid, u in USERS.items():
        if uid == ADMIN_ID:
            continue
        check = "✅ " if uid in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{u['name']}", callback_data=f"asgn_{uid}"
        )])
    all_uids = [uid for uid in USERS if uid != ADMIN_ID]
    all_sel = len(selected) == len(all_uids) and len(all_uids) > 0
    buttons.append([InlineKeyboardButton(
        text="✅ Все выбраны" if all_sel else "📢 Выбрать всех",
        callback_data="asgn_all"
    )])
    buttons.append([InlineKeyboardButton(text="➡️ Далее", callback_data="asgn_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_task_action(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_{task_id}"),
    ]])


def kb_control(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Контроль пройден", callback_data=f"approve_{task_id}"),
        InlineKeyboardButton(text="🔄 Вернуть в работу", callback_data=f"return_{task_id}"),
    ]])


def kb_edit_task(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit_text_{task_id}")],
        [InlineKeyboardButton(text="📅 Изменить срок", callback_data=f"edit_date_{task_id}")],
        [InlineKeyboardButton(text="🔴 Закрыть принудительно", callback_data=f"force_close_{task_id}")],
    ])


def kb_filter_person(status_key: str):
    buttons = [[InlineKeyboardButton(text="👥 Все", callback_data=f"flt_{status_key}_all")]]
    for uid, u in USERS.items():
        if uid == ADMIN_ID:
            continue
        buttons.append([InlineKeyboardButton(
            text=u["name"], callback_data=f"flt_{status_key}_{uid}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def deadline_str(task: dict) -> str:
    """Отображение дедлайна задачи"""
    d = task.get("deadline_date")
    if d:
        if isinstance(d, str):
            d = date.fromisoformat(d)
        return format_deadline(d)
    return task.get("deadline", "без срока")


def is_overdue(task: dict) -> bool:
    d = task.get("deadline_date")
    if not d:
        return False
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d < date.today() and task["status"] in ("open", "checking")


def format_my_tasks(uid: str) -> str:
    my_tasks = [t for t in tasks.values()
                if uid in t.get("assignees", []) and t["status"] != "done"]
    if not my_tasks:
        return "✅ Активных задач нет."
    text = "<b>Твои активные задачи:</b>\n\n"
    for t in my_tasks:
        if is_overdue(t):
            icon = "🔴"
        elif t["status"] == "checking":
            icon = "🔍"
        else:
            icon = "🕐"
        text += (
            f"{icon} <b>#{t['id']}</b> {t['text'][:55]}{'...' if len(t['text']) > 55 else ''}\n"
            f"   📅 {deadline_str(t)}\n\n"
        )
    return text


# ─── Bot & Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message):
    uid = str(message.from_user.id)
    user = get_user(uid)
    if not user:
        await message.answer(
            f"👋 Привет!\n\nТвой Telegram ID: <code>{uid}</code>\n"
            "Покажи его администратору чтобы получить доступ.",
            parse_mode="HTML"
        )
        return
    if is_admin(uid):
        await message.answer(f"👋 Привет, {user['name']}! Выбери действие 👇",
                             reply_markup=kb_main_admin())
    else:
        tasks_text = format_my_tasks(uid)
        await message.answer(f"👋 {user['name']}, добро пожаловать!\n\n{tasks_text}",
                             parse_mode="HTML", reply_markup=kb_main_shop())





# ─── Выбор даты — общая логика ────────────────────────────────────────────────
async def show_calendar(message: Message, prompt: str = "📅 Выбери срок выполнения:"):
    await message.answer(prompt, reply_markup=kb_date_pick())


@dp.callback_query(F.data == "cal_ignore")
async def cb_cal_ignore(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("cal_nav_"))
async def cb_cal_nav(callback: CallbackQuery):
    _, _, yr, mn = callback.data.split("_")
    await callback.message.edit_reply_markup(
        reply_markup=kb_date_pick(int(yr), int(mn))
    )
    await callback.answer()


# ─── ADMIN: Новая задача ──────────────────────────────────────────────────────
@dp.message(F.text == "📋 Новая задача")
@dp.message(Command("newtask"))
async def cmd_newtask(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    if not is_admin(uid):
        return
    await state.update_data(assignees=[])
    await message.answer("👇 Выбери исполнителей (можно несколько):",
                         reply_markup=kb_assignee_select([]))
    await state.set_state(NewTask.choosing_assignees)


@dp.callback_query(F.data.startswith("asgn_"), NewTask.choosing_assignees)
async def cb_toggle_assignee(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get("assignees", []))
    val = callback.data.replace("asgn_", "")
    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы одного!", show_alert=True)
            return
        names = [USERS[u]["name"] for u in selected if u in USERS]
        await callback.message.edit_text(
            f"✅ Исполнители: {', '.join(names)}\n\n📝 Введите текст задачи:")
        await state.set_state(NewTask.entering_text)
        await callback.answer()
        return
    if val == "all":
        all_uids = [u for u in USERS if u != ADMIN_ID]
        selected = all_uids if len(selected) < len(all_uids) else []
    else:
        if val in selected:
            selected.remove(val)
        else:
            selected.append(val)
    await state.update_data(assignees=selected)
    await callback.message.edit_reply_markup(reply_markup=kb_assignee_select(selected))
    await callback.answer()


@dp.message(NewTask.entering_text)
async def admin_task_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await show_calendar(message)
    await state.set_state(NewTask.choosing_deadline)


@dp.callback_query(F.data.startswith("cal_pick_"), NewTask.choosing_deadline)
async def admin_task_date_picked(callback: CallbackQuery, state: FSMContext):
    d = date.fromisoformat(callback.data.replace("cal_pick_", ""))
    await callback.message.edit_reply_markup(reply_markup=None)
    await _create_task_admin(callback.message, state, d)
    await callback.answer()


@dp.callback_query(F.data == "cal_none", NewTask.choosing_deadline)
async def admin_task_no_deadline(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await _create_task_admin(callback.message, state, None)
    await callback.answer()


@dp.callback_query(F.data == "cal_manual", NewTask.choosing_deadline)
async def admin_task_manual_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✏️ Введите дату в формате ДД.ММ или ДД.ММ.ГГГГ:")
    await state.set_state(NewTask.entering_manual_deadline)
    await callback.answer()


@dp.message(NewTask.entering_manual_deadline)
async def admin_task_manual_date_entered(message: Message, state: FSMContext):
    d = parse_date_input(message.text)
    if not d:
        await message.answer("❌ Не смог распознать дату. Попробуй формат: 25.03 или 25.03.2025")
        return
    await _create_task_admin(message, state, d)


async def _create_task_admin(message: Message, state: FSMContext, deadline_date):
    data = await state.get_data()
    uid = str(message.chat.id)
    # Ищем реального отправителя
    for possible_uid in [str(message.chat.id)]:
        user = get_user(possible_uid)
        if user:
            break
    user = get_user(ADMIN_ID)
    task_id = next_task_id()
    assignees = data.get("assignees", [])

    deadline_text = format_deadline(deadline_date) if deadline_date else "без срока"

    task = {
        "id": task_id,
        "from_uid": ADMIN_ID,
        "from_name": user["name"],
        "assignees": assignees,
        "confirmed_by": [],
        "text": data["text"],
        "deadline": deadline_text,
        "deadline_date": deadline_date.isoformat() if deadline_date else None,
        "status": "open",
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "iterations": 0,
        "remarks": [],
        "type": "task",
    }
    tasks[task_id] = task

    sent_names = []
    for a_uid in assignees:
        a_user = get_user(a_uid)
        if not a_user:
            continue
        try:
            await bot.send_message(
                a_uid,
                f"📋 <b>Новая задача от {user['name']}</b>\n\n"
                f"📝 {data['text']}\n📅 Срок: {deadline_text}\n🆔 Задача #{task_id}",
                parse_mode="HTML",
                reply_markup=kb_task_action(task_id)
            )
            if a_uid != ADMIN_ID:
                sent_names.append(a_user["name"])
        except Exception as e:
            logging.warning(f"Send to {a_uid} failed: {e}")

    await message.answer(
        f"✅ Задача #{task_id} отправлена: {', '.join(sent_names)}",
        reply_markup=kb_main_admin()
    )
    await state.clear()


# ─── SHOP: Поставить задачу ───────────────────────────────────────────────────
@dp.message(F.text == "📌 Поставить задачу")
async def btn_shop_task(message: Message, state: FSMContext):
    await message.answer("📝 Опишите задачу для руководителя:",
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(ShopTask.entering_text)


@dp.message(ShopTask.entering_text)
async def shop_task_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await show_calendar(message, "📅 Выбери желаемый срок:")
    await state.set_state(ShopTask.choosing_deadline)


@dp.callback_query(F.data.startswith("cal_pick_"), ShopTask.choosing_deadline)
async def shop_task_date_picked(callback: CallbackQuery, state: FSMContext):
    d = date.fromisoformat(callback.data.replace("cal_pick_", ""))
    await callback.message.edit_reply_markup(reply_markup=None)
    await _create_task_shop(callback.message, state, d)
    await callback.answer()


@dp.callback_query(F.data == "cal_none", ShopTask.choosing_deadline)
async def shop_task_no_deadline(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await _create_task_shop(callback.message, state, None)
    await callback.answer()


@dp.callback_query(F.data == "cal_manual", ShopTask.choosing_deadline)
async def shop_task_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✏️ Введите дату: ДД.ММ или ДД.ММ.ГГГГ")
    await state.set_state(ShopTask.entering_manual_deadline)
    await callback.answer()


@dp.message(ShopTask.entering_manual_deadline)
async def shop_task_manual_entered(message: Message, state: FSMContext):
    d = parse_date_input(message.text)
    if not d:
        await message.answer("❌ Не смог распознать дату. Попробуй: 25.03 или 25.03.2025")
        return
    await _create_task_shop(message, state, d)


async def _create_task_shop(message: Message, state: FSMContext, deadline_date):
    data = await state.get_data()
    uid = str(message.chat.id)
    user = get_user(uid)
    if not user:
        # fallback при callback
        for u_id, u in USERS.items():
            if u["role"] == "shop":
                user = u
                uid = u_id
                break
    task_id = next_task_id()
    deadline_text = format_deadline(deadline_date) if deadline_date else "без срока"

    task = {
        "id": task_id,
        "from_uid": uid,
        "from_name": user["name"] if user else "Магазин",
        "assignees": [ADMIN_ID],
        "confirmed_by": [],
        "text": data["text"],
        "deadline": deadline_text,
        "deadline_date": deadline_date.isoformat() if deadline_date else None,
        "status": "open",
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "iterations": 0,
        "remarks": [],
        "type": "task",
    }
    tasks[task_id] = task

    await bot.send_message(
        ADMIN_ID,
        f"📌 <b>Задача от {user['name'] if user else 'Магазин'}</b>\n\n"
        f"📝 {data['text']}\n📅 Срок: {deadline_text}\n🆔 Задача #{task_id}",
        parse_mode="HTML",
        reply_markup=kb_task_action(task_id)
    )
    await message.answer(f"✅ Задача #{task_id} отправлена руководителю.",
                         reply_markup=kb_main_shop())
    await state.clear()


# ─── SHOP: Вопрос (без дедлайна) ─────────────────────────────────────────────
@dp.message(F.text == "❓ Вопрос руководителю")
async def btn_question(message: Message, state: FSMContext):
    await message.answer("❓ Напишите вопрос:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ShopQuestion.entering_text)


@dp.message(ShopQuestion.entering_text)
async def shop_question_text(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    user = get_user(uid)
    task_id = next_task_id()
    task = {
        "id": task_id,
        "from_uid": uid,
        "from_name": user["name"] if user else uid,
        "assignees": [ADMIN_ID],
        "confirmed_by": [],
        "text": message.text,
        "deadline": "без срока",
        "deadline_date": None,
        "status": "open",
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "iterations": 0,
        "remarks": [],
        "type": "question",
    }
    tasks[task_id] = task
    await bot.send_message(
        ADMIN_ID,
        f"❓ <b>Вопрос от {user['name'] if user else uid}</b>\n\n"
        f"{message.text}\n🆔 #{task_id}",
        parse_mode="HTML",
        reply_markup=kb_task_action(task_id)
    )
    await message.answer("✅ Вопрос отправлен.", reply_markup=kb_main_shop())
    await state.clear()


# ─── Выполнено → отчёт ────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("done_"))
async def cb_done(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.replace("done_", ""))
    await state.update_data(task_id=task_id)
    await callback.message.answer("📷 Пришлите фото результата и/или напишите комментарий:")
    await state.set_state(ShopReport.waiting_report)
    await callback.answer()


@dp.message(ShopReport.waiting_report)
async def shop_report_received(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = tasks.get(task_id)
    if not task:
        await message.answer("Задача не найдена.")
        await state.clear()
        return

    uid = str(message.from_user.id)
    user = get_user(uid)
    if uid not in task["confirmed_by"]:
        task["confirmed_by"].append(uid)
    task["iterations"] += 1
    task["status"] = "checking"

    remaining = [a for a in task["assignees"] if a not in task["confirmed_by"] and a != ADMIN_ID]
    remaining_names = [USERS[r]["name"] for r in remaining if r in USERS]

    caption = (
        f"🔔 <b>{user['name'] if user else uid} отчитался по задаче #{task_id}</b>\n\n"
        f"📝 {task['text']}\n🔁 Итерация: {task['iterations']}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    if message.caption:
        caption += f"\n\n💬 {message.caption}"
    elif message.text:
        caption += f"\n\n💬 {message.text}"
    if remaining_names:
        caption += f"\n\n⏳ Ещё не отчитались: {', '.join(remaining_names)}"

    if message.photo:
        await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id,
                             caption=caption, parse_mode="HTML",
                             reply_markup=kb_control(task_id))
    else:
        await bot.send_message(ADMIN_ID, caption, parse_mode="HTML",
                               reply_markup=kb_control(task_id))

    await message.answer("✅ Отчёт отправлен. Ожидайте проверки.",
                         reply_markup=kb_main_shop())
    await state.clear()


# ─── Контроль пройден ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(callback: CallbackQuery):
    task_id = int(callback.data.replace("approve_", ""))
    task = tasks.get(task_id)
    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    task["status"] = "done"
    task["closed_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    for a_uid in task["assignees"]:
        if a_uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(a_uid,
                f"✅ <b>Задача #{task_id} принята!</b>\n\n📝 {task['text']}",
                parse_mode="HTML", reply_markup=kb_main_shop())
        except Exception as e:
            logging.warning(f"Notify error: {e}")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Задача #{task_id} закрыта.",
                                  reply_markup=kb_main_admin())
    await callback.answer()


# ─── Вернуть в работу ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("return_"))
async def cb_return(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.replace("return_", ""))
    await state.update_data(task_id=task_id)
    await callback.message.answer(f"✏️ Опишите замечания по задаче #{task_id}:")
    await state.set_state(ReturnTask.entering_remarks)
    await callback.answer()


@dp.message(ReturnTask.entering_remarks)
async def return_remarks_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = tasks.get(task_id)
    if not task:
        await message.answer("Задача не найдена.")
        await state.clear()
        return
    task["status"] = "open"
    task["confirmed_by"] = []
    remarks = message.text
    task["remarks"].append({"text": remarks, "at": datetime.now().strftime("%d.%m.%Y %H:%M")})
    for a_uid in task["assignees"]:
        if a_uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(a_uid,
                f"🔄 <b>Задача #{task_id} возвращена на доработку</b>\n\n"
                f"📝 {task['text']}\n\n❗ Замечания:\n{remarks}",
                parse_mode="HTML", reply_markup=kb_task_action(task_id))
        except Exception as e:
            logging.warning(f"Notify error: {e}")
    await message.answer(f"🔄 Задача #{task_id} возвращена.", reply_markup=kb_main_admin())
    await state.clear()


# ─── Редактирование задач ─────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("edit_text_"))
async def cb_edit_text(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.replace("edit_text_", ""))
    task = tasks.get(task_id)
    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    await state.update_data(task_id=task_id)
    await callback.message.answer(
        f"✏️ Текущий текст:\n<i>{task['text']}</i>\n\nВведите новый текст:",
        parse_mode="HTML")
    await state.set_state(EditTask.entering_new_text)
    await callback.answer()


@dp.message(EditTask.entering_new_text)
async def edit_text_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = tasks.get(task_id)
    if not task:
        await state.clear()
        return
    task["text"] = message.text
    for a_uid in task["assignees"]:
        if a_uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(a_uid,
                f"✏️ <b>Задача #{task_id} изменена</b>\n\n"
                f"📝 {message.text}\n📅 Срок: {deadline_str(task)}",
                parse_mode="HTML", reply_markup=kb_task_action(task_id))
        except Exception as e:
            logging.warning(f"Notify error: {e}")
    await message.answer(f"✅ Текст задачи #{task_id} обновлён.", reply_markup=kb_main_admin())
    await state.clear()


@dp.callback_query(F.data.startswith("edit_date_"))
async def cb_edit_date(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.replace("edit_date_", ""))
    task = tasks.get(task_id)
    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    await state.update_data(task_id=task_id)
    await callback.message.answer(
        f"📅 Текущий срок: <i>{deadline_str(task)}</i>\n\nВыбери новый срок:",
        parse_mode="HTML", reply_markup=kb_date_pick())
    await state.set_state(EditTask.choosing_new_deadline)
    await callback.answer()


@dp.callback_query(F.data.startswith("cal_pick_"), EditTask.choosing_new_deadline)
async def edit_date_picked(callback: CallbackQuery, state: FSMContext):
    d = date.fromisoformat(callback.data.replace("cal_pick_", ""))
    await callback.message.edit_reply_markup(reply_markup=None)
    await _apply_new_deadline(callback.message, state, d)
    await callback.answer()


@dp.callback_query(F.data == "cal_none", EditTask.choosing_new_deadline)
async def edit_date_none(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await _apply_new_deadline(callback.message, state, None)
    await callback.answer()


@dp.callback_query(F.data == "cal_manual", EditTask.choosing_new_deadline)
async def edit_date_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✏️ Введите дату: ДД.ММ или ДД.ММ.ГГГГ")
    await state.set_state(EditTask.entering_manual_new_deadline)
    await callback.answer()


@dp.message(EditTask.entering_manual_new_deadline)
async def edit_date_manual_entered(message: Message, state: FSMContext):
    d = parse_date_input(message.text)
    if not d:
        await message.answer("❌ Не смог распознать дату. Попробуй: 25.03 или 25.03.2025")
        return
    await _apply_new_deadline(message, state, d)


async def _apply_new_deadline(message: Message, state: FSMContext, new_date):
    data = await state.get_data()
    task_id = data["task_id"]
    task = tasks.get(task_id)
    if not task:
        await state.clear()
        return
    deadline_text = format_deadline(new_date) if new_date else "без срока"
    task["deadline"] = deadline_text
    task["deadline_date"] = new_date.isoformat() if new_date else None
    for a_uid in task["assignees"]:
        if a_uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(a_uid,
                f"📅 <b>Срок задачи #{task_id} изменён</b>\n\n"
                f"📝 {task['text']}\n📅 Новый срок: {deadline_text}",
                parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Notify error: {e}")
    await message.answer(f"✅ Срок задачи #{task_id} обновлён: {deadline_text}",
                         reply_markup=kb_main_admin())
    await state.clear()


@dp.callback_query(F.data.startswith("force_close_"))
async def cb_force_close(callback: CallbackQuery):
    task_id = int(callback.data.replace("force_close_", ""))
    task = tasks.get(task_id)
    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    task["status"] = "done"
    task["closed_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    task["force_closed"] = True
    for a_uid in task["assignees"]:
        if a_uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(a_uid,
                f"🔴 <b>Задача #{task_id} закрыта руководителем</b>\n\n📝 {task['text']}",
                parse_mode="HTML", reply_markup=kb_main_shop())
        except Exception as e:
            logging.warning(f"Notify error: {e}")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🔴 Задача #{task_id} принудительно закрыта.",
                                  reply_markup=kb_main_admin())
    await callback.answer()


# ─── Фильтры ──────────────────────────────────────────────────────────────────
@dp.message(F.text.in_(["🕐 В работе", "🔍 На проверке", "✅ Выполненные"]))
async def btn_filter_status(message: Message):
    uid = str(message.from_user.id)
    if not is_admin(uid):
        return
    status_map = {"🕐 В работе": "open", "🔍 На проверке": "checking", "✅ Выполненные": "done"}
    status_key = status_map[message.text]
    await message.answer("Показать для кого?", reply_markup=kb_filter_person(status_key))


@dp.callback_query(F.data.startswith("flt_"))
async def cb_filter_tasks(callback: CallbackQuery):
    parts = callback.data.split("_", 2)
    status_key = parts[1]
    person = parts[2]
    status_labels = {"open": "🕐 В работе", "checking": "🔍 На проверке", "done": "✅ Выполненные"}
    filtered = [t for t in tasks.values()
                if t["status"] == status_key and
                (person == "all" or person in t["assignees"])]
    if not filtered:
        await callback.message.edit_text("Задач не найдено.")
        await callback.answer()
        return
    label = status_labels.get(status_key, status_key)
    person_label = "все" if person == "all" else (get_user(person) or {}).get("name", person)
    text = f"<b>{label} — {person_label}</b>\n\n"
    for t in filtered:
        names = [USERS[a]["name"] for a in t["assignees"] if a in USERS and a != ADMIN_ID]
        overdue = " 🔴" if is_overdue(t) else ""
        force = " [закрыта]" if t.get("force_closed") else ""
        tag = "❓" if t.get("type") == "question" else "📌" if t.get("from_uid") != ADMIN_ID else "📋"
        text += (
            f"{tag} <b>#{t['id']}</b>{overdue}{force} "
            f"{t['text'][:50]}{'...' if len(t['text']) > 50 else ''}\n"
            f"   👤 {', '.join(names) or t['from_name']} | 📅 {deadline_str(t)}\n\n"
        )
    if status_key in ("open", "checking") and len(filtered) == 1:
        await callback.message.edit_text(text, parse_mode="HTML",
                                         reply_markup=kb_edit_task(filtered[0]["id"]))
    else:
        await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@dp.message(F.text == "📋 Мои задачи")
async def btn_my_tasks(message: Message):
    uid = str(message.from_user.id)
    user = get_user(uid)
    if not user:
        return
    tasks_text = format_my_tasks(uid)
    done_count = sum(1 for t in tasks.values()
                     if uid in t.get("assignees", []) and t["status"] == "done")
    if done_count:
        tasks_text += f"\n✅ Выполнено всего: {done_count}"
    await message.answer(tasks_text, parse_mode="HTML", reply_markup=kb_main_shop())


@dp.message(F.text == "📊 Отчёт")
@dp.message(Command("report"))
async def cmd_report(message: Message):
    uid = str(message.from_user.id)
    if not is_admin(uid):
        return
    total = len(tasks)
    open_t = sum(1 for t in tasks.values() if t["status"] == "open")
    checking_t = sum(1 for t in tasks.values() if t["status"] == "checking")
    done_t = sum(1 for t in tasks.values() if t["status"] == "done")
    overdue_t = sum(1 for t in tasks.values() if is_overdue(t))
    text = (
        f"📊 <b>Сводный отчёт</b>\n{datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"📌 Всего: {total}\n🕐 В работе: {open_t}\n"
        f"🔍 На проверке: {checking_t}\n✅ Выполнено: {done_t}\n"
        f"🔴 Просрочено: {overdue_t}\n\n<b>По участникам:</b>\n"
    )
    for p_uid, p_user in USERS.items():
        if p_uid == ADMIN_ID:
            continue
        p_tasks = [t for t in tasks.values() if p_uid in t.get("assignees", [])]
        if p_tasks:
            o = sum(1 for t in p_tasks if t["status"] in ("open", "checking"))
            d = sum(1 for t in p_tasks if t["status"] == "done")
            ov = sum(1 for t in p_tasks if is_overdue(t))
            overdue_str = f" 🔴{ov}" if ov else ""
            text += f"  {p_user['name']}: 🕐{o} ✅{d}{overdue_str}\n"
    await message.answer(text, parse_mode="HTML", reply_markup=kb_main_admin())


# ─── Планировщик уведомлений ──────────────────────────────────────────────────
async def notify_today_deadlines():
    """Уведомление в день дедлайна — исполнителям"""
    today = date.today()
    for task in tasks.values():
        if task["status"] not in ("open", "checking"):
            continue
        d = task.get("deadline_date")
        if not d:
            continue
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if d == today:
            for a_uid in task["assignees"]:
                if a_uid == ADMIN_ID:
                    continue
                try:
                    await bot.send_message(
                        a_uid,
                        f"⏰ <b>Сегодня срок задачи #{task['id']}!</b>\n\n"
                        f"📝 {task['text']}",
                        parse_mode="HTML",
                        reply_markup=kb_task_action(task["id"])
                    )
                except Exception as e:
                    logging.warning(f"Deadline notify error: {e}")


async def notify_overdue_summary():
    """Утренняя сводка просроченных — руководителю"""
    overdue = [t for t in tasks.values() if is_overdue(t)]
    if not overdue:
        return
    text = f"🔴 <b>Просроченные задачи ({len(overdue)}):</b>\n\n"
    for t in overdue:
        names = [USERS[a]["name"] for a in t["assignees"] if a in USERS and a != ADMIN_ID]
        text += (
            f"• <b>#{t['id']}</b> {t['text'][:50]}\n"
            f"  👤 {', '.join(names)} | 📅 {deadline_str(t)}\n\n"
        )
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Overdue summary error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    # Сводка просроченных — каждый день в 9:00
    scheduler.add_job(notify_overdue_summary, "cron", hour=9, minute=0)
    # Уведомления в день дедлайна — каждый день в 9:05
    scheduler.add_job(notify_today_deadlines, "cron", hour=9, minute=5)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
