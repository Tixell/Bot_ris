import logging
import random
import time
import os
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, CallbackContext

# Файлы для хранения рейтинга чая и времени сброса
RATING_FILE = "tea_rating.json"
LAST_RESET_FILE = "last_reset.json"

# Глобальные словари и переменные
participants = {}  # {chat_id: {user_id: {"first_name": ..., "username": ...}}}
chai_consumption = {}  # рейтинг чая, ключ – строковое представление user.id
banned_users = {}

# Модуль браков
# marriages: ключ – frozenset({user1, user2}), значение – словарь с информацией о браке
marriages = {}
# user_marriage: отображение user_id -> ключ брака (frozenset)
user_marriage = {}
# marriage_proposals: ключ – target user_id, значение – данные предложения (proposer и время)
marriage_proposals = {}
# Цена продления брака (например, условная единица)
marriage_extension_price = 0

# Переменная для отслеживания времени последнего сброса рейтинга чая
last_reset_time = None

# Модуль дуэлей
# duels: ключ – chat_id, значение – структура текущей дуэли
duels = {}
# duel_stats: статистика дуэлей {user_id: {"wins": int, "draws": int, "losses": int}}
duel_stats = {}
# Глобальная настройка исхода дуэли (по умолчанию "0" – ничего не делать)
duel_outcome = None

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Функции сохранения/загрузки рейтинга чая ===
def load_rating():
    global chai_consumption
    if os.path.exists(RATING_FILE):
        with open(RATING_FILE, "r", encoding="utf-8") as f:
            try:
                chai_consumption = json.load(f)
            except json.JSONDecodeError:
                chai_consumption = {}
    else:
        chai_consumption = {}

def save_rating():
    with open(RATING_FILE, "w", encoding="utf-8") as f:
        json.dump(chai_consumption, f)

def load_last_reset():
    global last_reset_time
    if os.path.exists(LAST_RESET_FILE):
        with open(LAST_RESET_FILE, "r", encoding="utf-8") as f:
            try:
                timestamp = json.load(f)
                last_reset_time = datetime.fromisoformat(timestamp) if timestamp else None
            except Exception:
                last_reset_time = None
    else:
        last_reset_time = None

def save_last_reset():
    with open(LAST_RESET_FILE, "w", encoding="utf-8") as f:
        if last_reset_time:
            json.dump(last_reset_time.isoformat(), f)
        else:
            json.dump("", f)

# Загрузка данных при старте
load_rating()
load_last_reset()

# === Функция проверки банов ===
def check_bans():
    current_time = time.time()
    to_unban = []
    for user_id, unban_time in banned_users.items():
        if current_time >= unban_time:
            to_unban.append(user_id)
    for user_id in to_unban:
        del banned_users[user_id]
        logger.info(f"Пользователь с ID {user_id} был разбанен.")

# === Функция для поиска пользователя по ссылке/референсу ===
def find_user_by_reference(chat_id, ref: str):
    ref = ref.lower()
    if ref.startswith("t.me/"):
        ref = ref[5:]
    if ref.startswith("@"):
        ref = ref[1:]
    for uid, info in participants.get(chat_id, {}).items():
        if info["username"] == ref or info["first_name"].lower() == ref:
            return uid, info
    return None, None

# === Функция для вывода рейтинга чая ===
async def rating_chai(update: Update, context: CallbackContext):
    global last_reset_time
    # Если прошло 1 неделя, сбрасываем рейтинг
    if last_reset_time is None or datetime.now() - last_reset_time >= timedelta(weeks=1):
        chai_consumption.clear()
        last_reset_time = datetime.now()
        save_last_reset()
        save_rating()
        logger.info("Рейтинг чая был сброшен.")
    sorted_users = sorted(chai_consumption.items(), key=lambda x: x[1], reverse=True)
    if not sorted_users:
        await update.message.reply_text("📊 На данный момент нет данных для рейтинга чая. ☕")
        return
    message = "🍵 Рейтинг по чаю за неделю: \n"
    for uid, total in sorted_users[:10]:
        uid_int = int(uid)
        user_info = participants.get(update.effective_chat.id, {}).get(uid_int, {"first_name": "Неизвестный"})
        # Скрытая ссылка: имя пользователя выделяется синей ссылкой без явного URL
        message += f"[{user_info['first_name']}](tg://user?id={uid_int}): {total} литров ☕\n"
    await update.message.reply_text(message, parse_mode="Markdown")

# === Функции модуля «Браки» ===

async def propose_marriage(update: Update, context: CallbackContext, ref: str):
    chat_id = update.effective_chat.id
    proposer = update.effective_user
    proposer_info = participants[chat_id].get(proposer.id)
    target_uid, target_info = find_user_by_reference(chat_id, ref)
    if not target_uid:
        await update.message.reply_text("❌ Не удалось найти пользователя по указанной ссылке/имени.")
        return
    if target_uid == proposer.id:
        await update.message.reply_text("❌ Нельзя предложить брак самому себе!")
        return
    if proposer.id in user_marriage:
        await update.message.reply_text("❌ Вы уже состоите в браке!")
        return
    if target_uid in user_marriage:
        await update.message.reply_text("❌ Этот пользователь уже состоит в браке!")
        return
    marriage_proposals[target_uid] = {"proposer": proposer.id, "timestamp": datetime.now()}
    await update.message.reply_text(
        f"💍 {proposer_info['first_name']} предлагает вступить в брак с {target_info['first_name']}.\n"
        f"{target_info['first_name']}, ответьте командой «Брак да» или «Брак нет»."
    )

async def accept_marriage(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    target = update.effective_user
    target_info = participants[chat_id].get(target.id)
    proposal = marriage_proposals.get(target.id)
    if not proposal:
        await update.message.reply_text("❌ У вас нет предложений брака.")
        return
    proposer_id = proposal["proposer"]
    proposer_info = participants[chat_id].get(proposer_id)
    now = datetime.now()
    key = frozenset({proposer_id, target.id})
    if key in marriages:
        marriage = marriages[key]
        if not marriage["active"]:
            divorced_time = marriage.get("divorced_time")
            if divorced_time and now - divorced_time <= timedelta(days=3):
                marriage["active"] = True
                marriage["divorced_time"] = None
                user_marriage[proposer_id] = key
                user_marriage[target.id] = key
                await update.message.reply_text("💍 Брак восстановлен! Поздравляем!")
                del marriage_proposals[target.id]
                return
    if proposer_id in user_marriage or target.id in user_marriage:
        await update.message.reply_text("❌ Один из участников уже состоит в браке.")
        del marriage_proposals[target.id]
        return
    marriages[key] = {"partners": (proposer_id, target.id), "start_time": now, "active": True, "divorced_time": None, "extended_until": now}
    user_marriage[proposer_id] = key
    user_marriage[target.id] = key
    await update.message.reply_text("💍 Поздравляем, вы теперь в браке!")
    del marriage_proposals[target.id]

async def decline_marriage(update: Update, context: CallbackContext):
    target = update.effective_user
    if target.id in marriage_proposals:
        del marriage_proposals[target.id]
        await update.message.reply_text("❌ Предложение отклонено.")
    else:
        await update.message.reply_text("❌ У вас нет предложений брака.")

async def dissolve_marriage(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if user_id not in user_marriage:
        await update.message.reply_text("❌ Вы не состоите в браке.")
        return
    key = user_marriage[user_id]
    marriage = marriages.get(key)
    if marriage and marriage["active"]:
        marriage["active"] = False
        marriage["divorced_time"] = datetime.now()
        for uid in key:
            if uid in user_marriage:
                del user_marriage[uid]
        await update.message.reply_text("💔 Брак расторгнут.")
    else:
        await update.message.reply_text("❌ Ошибка при расторжении брака.")

async def my_marriage(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if user_id not in user_marriage:
        await update.message.reply_text("ℹ️ У вас нет брака.")
        return
    key = user_marriage[user_id]
    marriage = marriages.get(key)
    if marriage and marriage["active"]:
        partner_id = next(uid for uid in key if uid != user_id)
        partner_info = participants[chat_id].get(partner_id, {"first_name": "Неизвестно"})
        duration = datetime.now() - marriage["start_time"]
        await update.message.reply_text(
            f"💍 Ваш брак с {partner_info['first_name']} длится уже {duration.days} дней."
        )
    else:
        await update.message.reply_text("ℹ️ У вас нет активного брака.")

async def user_marriage_info(update: Update, context: CallbackContext, ref: str):
    chat_id = update.effective_chat.id
    target_uid, target_info = find_user_by_reference(chat_id, ref)
    if not target_uid:
        await update.message.reply_text("❌ Пользователь не найден.")
        return
    if target_uid not in user_marriage:
        await update.message.reply_text(f"ℹ️ Пользователь {target_info['first_name']} не состоит в браке.")
        return
    key = user_marriage[target_uid]
    marriage = marriages.get(key)
    if marriage and marriage["active"]:
        partner_id = next(uid for uid in key if uid != target_uid)
        partner_info = participants[chat_id].get(partner_id, {"first_name": "Неизвестно"})
        duration = datetime.now() - marriage["start_time"]
        await update.message.reply_text(
            f"💍 Брак пользователя {target_info['first_name']} с {partner_info['first_name']} длится {duration.days} дней."
        )
    else:
        await update.message.reply_text(f"ℹ️ У пользователя {target_info['first_name']} нет активного брака.")

async def list_marriages(update: Update, context: CallbackContext, page: int = 1, per_page: int = 5):
    chat_id = update.effective_chat.id
    active_marriages = [m for m in marriages.values() if m["active"]]
    if not active_marriages:
        await update.message.reply_text("ℹ️ В чате нет активных браков.")
        return
    active_marriages.sort(key=lambda m: datetime.now() - m["start_time"], reverse=True)
    total = len(active_marriages)
    start = (page - 1) * per_page
    end = start + per_page
    msg = "💍 Список браков:\n"
    for m in active_marriages[start:end]:
        u1, u2 = m["partners"]
        name1 = participants[chat_id].get(u1, {"first_name": "Неизвестно"})["first_name"]
        name2 = participants[chat_id].get(u2, {"first_name": "Неизвестно"})["first_name"]
        duration = datetime.now() - m["start_time"]
        msg += f"{name1} & {name2} – {duration.days} дней\n"
    msg += f"\nСтраница {page} из {((total - 1) // per_page) + 1}"
    await update.message.reply_text(msg)

async def marry_pair(update: Update, context: CallbackContext, ref1: str, ref2: str):
    chat_id = update.effective_chat.id
    uid1, info1 = find_user_by_reference(chat_id, ref1)
    uid2, info2 = find_user_by_reference(chat_id, ref2)
    if not uid1 or not uid2:
        await update.message.reply_text("❌ Не удалось найти одного из пользователей.")
        return
    if uid1 in user_marriage or uid2 in user_marriage:
        await update.message.reply_text("❌ Один из пользователей уже состоит в браке.")
        return
    key = frozenset({uid1, uid2})
    now = datetime.now()
    marriages[key] = {"partners": (uid1, uid2), "start_time": now, "active": True, "divorced_time": None, "extended_until": now}
    user_marriage[uid1] = key
    user_marriage[uid2] = key
    await update.message.reply_text(f"💍 {info1['first_name']} и {info2['first_name']} теперь состоят в браке!")

async def divorce_pair(update: Update, context: CallbackContext, ref1: str, ref2: str):
    chat_id = update.effective_chat.id
    uid1, info1 = find_user_by_reference(chat_id, ref1)
    uid2, info2 = find_user_by_reference(chat_id, ref2)
    if not uid1 or not uid2:
        await update.message.reply_text("❌ Не удалось найти одного из пользователей.")
        return
    key = frozenset({uid1, uid2})
    marriage = marriages.get(key)
    if marriage and marriage["active"]:
        marriage["active"] = False
        marriage["divorced_time"] = datetime.now()
        for uid in key:
            if uid in user_marriage:
                del user_marriage[uid]
        await update.message.reply_text(f"💔 Брак между {info1['first_name']} и {info2['first_name']} расторгнут.")
    else:
        await update.message.reply_text("❌ Указанная пара не состоит в активном браке.")

async def reset_marriages(update: Update, context: CallbackContext):
    member = await update.message.chat.get_member(update.message.from_user.id)
    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("❌ Только администратор может сбросить браки.")
        return
    marriages.clear()
    user_marriage.clear()
    marriage_proposals.clear()
    await update.message.reply_text("💥 Все браки сброшены.")

# Новые команды для продления брака
async def set_marriage_extension_price(update: Update, context: CallbackContext):
    global marriage_extension_price
    try:
        parts = update.message.text.split()
        price = int(parts[2])
        marriage_extension_price = price
        await update.message.reply_text(f"💰 Цена продления брака установлена на {price}.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Использование: Брак цена продления {число}")

async def extend_marriage_custom(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        parts = update.message.text.split()
        days = int(parts[2])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Использование: Брак продлить {кол-во дней}")
        return
    if user_id not in user_marriage:
        await update.message.reply_text("❌ Вы не состоите в браке.")
        return
    key = user_marriage[user_id]
    marriage = marriages.get(key)
    if not marriage or not marriage["active"]:
        await update.message.reply_text("❌ У вас нет активного брака.")
        return
    # Продлеваем брак на указанное количество дней
    extension = timedelta(days=days)
    if marriage.get("extended_until") and marriage["extended_until"] > datetime.now():
        marriage["extended_until"] += extension
    else:
        marriage["extended_until"] = datetime.now() + extension
    await update.message.reply_text(
        f"⏳ Брак продлён до {marriage['extended_until'].strftime('%d.%m.%Y %H:%M:%S')}. Цена продления: {marriage_extension_price}."
    )

# === Модуль «Дуэли» ===

def init_duel_stats(user_id):
    if user_id not in duel_stats:
        duel_stats[user_id] = {"wins": 0, "draws": 0, "losses": 0}

async def handle_duel(update: Update, context: CallbackContext):
    global duel_outcome
    message_text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user

    lower_text = message_text.lower()

    # Если команда вида "дуэль {ссылка}"
    if lower_text.startswith("дуэль ") and not lower_text.startswith("дуэль да") and not lower_text.startswith("дуэль нет") and not lower_text.startswith("дуэль отмена"):
        if chat_id in duels:
            await update.message.reply_text("❌ В чате уже идёт дуэль.")
            return
        parts = message_text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Использование: дуэль {ссылка}")
            return
        ref = parts[1].strip()
        target_uid, target_info = find_user_by_reference(chat_id, ref)
        if not target_uid:
            await update.message.reply_text("❌ Не удалось найти пользователя по указанной ссылке/имени.")
            return
        if target_uid == user.id:
            await update.message.reply_text("❌ Нельзя вызвать себя на дуэль!")
            return
        duels[chat_id] = {
            "challenger": user.id,
            "target": target_uid,
            "status": "pending",
            "timestamp": datetime.now(),
            "aim_bonus": {user.id: 0, target_uid: 0}
        }
        challenger_info = participants[chat_id].get(user.id)
        await update.message.reply_text(
            f"⚔️ {challenger_info['first_name']} вызывает {target_info['first_name']} на дуэль!\n"
            f"{target_info['first_name']}, ответьте командами «Дуэль да» или «Дуэль нет»."
        )
        return

    # Команда "Кто дуэль" — выбираем случайного участника для дуэли
    if lower_text == "кто дуэль":
        if chat_id in duels:
            await update.message.reply_text("❌ В чате уже идёт дуэль.")
            return
        chat_participants = [uid for uid in participants.get(chat_id, {}) if uid != context.bot.id and uid != user.id]
        if not chat_participants:
            await update.message.reply_text("❌ Недостаточно участников для дуэли.")
            return
        target_uid = random.choice(chat_participants)
        target_info = participants[chat_id].get(target_uid)
        duels[chat_id] = {
            "challenger": user.id,
            "target": target_uid,
            "status": "pending",
            "timestamp": datetime.now(),
            "aim_bonus": {user.id: 0, target_uid: 0}
        }
        challenger_info = participants[chat_id].get(user.id)
        await update.message.reply_text(
            f"⚔️ {challenger_info['first_name']} приглашает {target_info['first_name']} на дуэль!\n"
            f"{target_info['first_name']}, ответьте «Дуэль да» или «Дуэль нет»."
        )
        return

    # Принятие вызова
    if lower_text == "дуэль да":
        if chat_id not in duels:
            await update.message.reply_text("❌ Нет активных вызовов дуэли.")
            return
        duel = duels[chat_id]
        if duel["status"] != "pending" or duel["target"] != user.id:
            await update.message.reply_text("❌ У вас нет вызова на дуэль.")
            return
        duel["status"] = "active"
        duel["turn"] = random.choice([duel["challenger"], duel["target"]])
        for uid in duel["aim_bonus"]:
            duel["aim_bonus"][uid] = 0
        challenger_info = participants[chat_id].get(duel["challenger"])
        target_info = participants[chat_id].get(duel["target"])
        starter = participants[chat_id].get(duel["turn"])
        await update.message.reply_text(
            f"⚔️ Дуэль между {challenger_info['first_name']} и {target_info['first_name']} началась!\n"
            f"Первый ход у {starter['first_name']}. Команды: «Выстрел», «Прицелиться», «Сбросить прицел»."
        )
        return

    # Отклонение вызова
    if lower_text == "дуэль нет":
        if chat_id not in duels:
            await update.message.reply_text("❌ Нет активных вызовов дуэли.")
            return
        duel = duels[chat_id]
        if duel["status"] != "pending" or duel["target"] != user.id:
            await update.message.reply_text("❌ У вас нет вызова на дуэль.")
            return
        del duels[chat_id]
        await update.message.reply_text("❌ Вызов на дуэль отклонён.")
        return

    # Отмена вызова (если вы являетесь инициатором)
    if lower_text == "дуэль отмена":
        if chat_id not in duels:
            await update.message.reply_text("❌ Нет активных вызовов дуэли.")
            return
        duel = duels[chat_id]
        if duel["challenger"] != user.id:
            await update.message.reply_text("❌ Только инициатор может отменить вызов.")
            return
        del duels[chat_id]
        await update.message.reply_text("❌ Вызов на дуэль отменён.")
        return

    # Команды в активной дуэли
    if chat_id in duels:
        duel = duels[chat_id]
        if duel["status"] == "active":
            if user.id not in [duel["challenger"], duel["target"]]:
                return

            # Прицелиться
            if lower_text == "прицелиться":
                if duel.get("turn") != user.id:
                    await update.message.reply_text("❌ Сейчас не ваш ход.")
                    return
                duel["aim_bonus"][user.id] += 10
                await update.message.reply_text(
                    f"🎯 {participants[chat_id][user.id]['first_name']} прицелился. Бонус: {duel['aim_bonus'][user.id]}%"
                )
                return

            # Сбросить прицел
            if lower_text == "сбросить прицел":
                if duel.get("turn") != user.id:
                    await update.message.reply_text("❌ Сейчас не ваш ход.")
                    return
                duel["aim_bonus"][user.id] = 0
                await update.message.reply_text(
                    f"🎯 {participants[chat_id][user.id]['first_name']} сбросил прицел."
                )
                return

            # Выстрел
            if lower_text == "выстрел":
                if duel.get("turn") != user.id:
                    await update.message.reply_text("❌ Сейчас не ваш ход.")
                    return
                base_chance = 50
                bonus = duel["aim_bonus"][user.id]
                hit_chance = base_chance + bonus
                roll = random.randint(1, 100)
                shooter_name = participants[chat_id][user.id]["first_name"]
                if roll <= hit_chance:
                    loser = duel["target"] if user.id == duel["challenger"] else duel["challenger"]
                    winner = user.id
                    init_duel_stats(winner)
                    init_duel_stats(loser)
                    duel_stats[winner]["wins"] += 1
                    duel_stats[loser]["losses"] += 1
                    result_msg = f"💥 {shooter_name} выстрелил и попал! Дуэль окончена."
                    outcome_text = ""
                    if duel_outcome == "кик":
                        outcome_text = "Будет произведён кик."
                    elif duel_outcome == "бан минута":
                        banned_users[loser] = time.time() + 60
                        outcome_text = "Произведён бан на минуту."
                    elif duel_outcome == "бан 10 минут":
                        banned_users[loser] = time.time() + 600
                        outcome_text = "Произведён бан на 10 минут."
                    elif duel_outcome == "бан час":
                        banned_users[loser] = time.time() + 3600
                        outcome_text = "Произведён бан на час."
                    elif duel_outcome == "бан сутки":
                        banned_users[loser] = time.time() + 86400
                        outcome_text = "Произведён бан на сутки."
                    elif duel_outcome == "бан навсегда":
                        banned_users[loser] = time.time() + 10 * 365 * 86400
                        outcome_text = "Произведён бан навсегда."
                    if outcome_text:
                        result_msg += f"\n{outcome_text}"
                    await update.message.reply_text(result_msg)
                    del duels[chat_id]
                else:
                    await update.message.reply_text(f"😅 {shooter_name} выстрелил, но промахнулся!")
                    duel["aim_bonus"][user.id] = 0
                    duel["turn"] = duel["target"] if user.id == duel["challenger"] else duel["challenger"]
                    next_shooter = participants[chat_id][duel["turn"]]["first_name"]
                    await update.message.reply_text(f"Сейчас ход у {next_shooter}.")
                return

    # Настройка исхода дуэли
    if lower_text.startswith("дуэли исход"):
        parts = message_text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("❌ Использование: Дуэли исход {параметр}")
            return
        duel_outcome = parts[2].strip().lower()
        await update.message.reply_text(f"⚙️ Исход дуэли установлен: {duel_outcome}")
        return

    # Вывод статистики дуэлей
    if lower_text == "дуэли стата":
        if not duel_stats:
            await update.message.reply_text("ℹ️ Статистика дуэлей пуста.")
            return
        msg = "🏆 Статистика дуэлей:\n"
        for uid, stats in duel_stats.items():
            name = participants[chat_id].get(uid, {"first_name": "Неизвестно"})["first_name"]
            msg += f"{name}: Выигрышей {stats['wins']} | Проигрышей {stats['losses']} | Ничьих {stats['draws']}\n"
        await update.message.reply_text(msg)
        return

    # Сброс статистики дуэлей
    if lower_text in ("!дуэли сброс", "!сброс дуэлей"):
        duel_stats.clear()
        await update.message.reply_text("🔄 Статистика дуэлей сброшена.")
        return

# === Основной обработчик текстовых сообщений ===

async def handle_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return  # Обрабатываем только текстовые сообщения
    message_text = update.message.text.strip()
    chat = update.effective_chat
    user = update.effective_user

    check_bans()

    if user.id in banned_users:
        await update.message.reply_text("❗ Вы забанены и не можете отправлять сообщения. ❗")
        return

    # Обновляем список участников: сохраняем first_name и username
    if chat.id not in participants:
        participants[chat.id] = {}
    participants[chat.id][user.id] = {
        "first_name": user.first_name,
        "username": user.username.lower() if user.username else str(user.id)
    }

    # Если сообщение начинается с команды рейтинга чая
    if message_text.startswith("Рис рейтинг чая"):
        await rating_chai(update, context)
        return

    # Обработка команд модуля «Браки» и дуэлей
    lower_text = message_text.lower()
    if lower_text.startswith("брак") or lower_text.startswith("!развод") or \
       lower_text.startswith("мой брак") or lower_text.startswith("твой брак") or \
       lower_text.startswith("браки") or lower_text.startswith("поженить пару") or \
       lower_text.startswith("развести пару") or lower_text.startswith("сброс браков") or \
       lower_text.startswith("продление брака") or lower_text.startswith("топ браков") or \
       lower_text.startswith("авторазвод браков") or lower_text.startswith("брак цена продления") or \
       lower_text.startswith("брак продлить"):
        await handle_marriage(update, context)
        return

    # Обработка команд модуля «Дуэли»
    if lower_text.startswith("дуэль") or lower_text.startswith("кто дуэль") or \
       lower_text.startswith("выстрел") or lower_text in ("прицелиться", "сбросить прицел") or \
       lower_text.startswith("дуэли исход") or lower_text.startswith("дуэли стата") or \
       lower_text in ("!дуэли сброс", "!сброс дуэлей"):
        await handle_duel(update, context)
        return

    # Прочие команды (Рис кто, Рис инфа что, Чай пить, Крутим бутылко)
    if message_text.startswith("Рис кто") or message_text.startswith("Кто рисует"):
        if message_text.startswith("Рис кто"):
            additional_text = message_text[len("Рис кто"):].strip()
        else:
            additional_text = message_text[len("Кто рисует"):].strip()
        chosen_phrase = random.choice(["🔮Я вижу", "🔮Я знаю", "🥠Мне кажется", "🧙Я уверен"])
        bot_id = context.bot.id
        bot_name = "Бот"
        participants[chat.id][bot_id] = {"first_name": bot_name, "username": bot_name.lower()}
        chat_participants = list(participants[chat.id].items())
        chosen_user_id, info = random.choice(chat_participants)
        chosen_name = info["first_name"]
        # Используем скрытую ссылку с tg://user?id=
        reply_text = f"{chosen_phrase}, что [{chosen_name}](tg://user?id={chosen_user_id}) {additional_text} 😄"
        await update.message.reply_text(reply_text, parse_mode="Markdown")
        return

    if "рис инфа что" in message_text.lower() or "инфа что" in message_text.lower():
        infa_phrases = ["🔍Я обнаружил", "🤔По моим данным", "🧐Я подсчитал", "🧮Кажется, я определил"]
        chosen_phrase = random.choice(infa_phrases)
        try:
            if "рис инфа что" in message_text.lower():
                subject = message_text.split("Рис инфа что", 1)[1].strip()
            else:
                subject = message_text.split("Инфа что", 1)[1].strip()
        except IndexError:
            subject = ""
        random_percentage = random.randint(1, 100)
        reply_text = f"💡 {chosen_phrase}, что {subject} составляет {random_percentage}% 😌"
        await update.message.reply_text(reply_text)
        return

    if message_text.startswith("Чай пить") or message_text.startswith("Пить чай"):
        if message_text.startswith("Чай пить"):
            tea_name = message_text[len("Чай пить"):].strip()
        else:
            tea_name = message_text[len("Пить чай"):].strip()
        if tea_name:
            random_liters = round(random.uniform(1, 40), 2)
            uid = str(user.id)
            chai_consumption[uid] = chai_consumption.get(uid, 0) + random_liters
            save_rating()
            reply_text = f"🍵 {user.first_name}, выпил {random_liters} литров чая {tea_name} 😋"
            await update.message.reply_text(reply_text)
        return

    if message_text.startswith("Крутим бутылко") or message_text.startswith("Кто крутит бутылко"):
        if message_text.startswith("Крутим бутылко"):
            additional_text = message_text[len("Крутим бутылко"):].strip()
        else:
            additional_text = message_text[len("Кто крутит бутылко"):].strip()
        if len(participants[chat.id]) < 2:
            await update.message.reply_text("❌ Нельзя крутануть бутылко, нужно хотя бы два человека. ❌")
            return
        sampled = random.sample(list(participants[chat.id].items()), 2)
        (user1_id, user1_info), (user2_id, user2_info) = sampled[0], sampled[1]
        user1_name = user1_info["first_name"]
        user2_name = user2_info["first_name"]
        bottle_phrases = [
            "🍾 Бутылка решила, что",
            "🎉 Судьба через бутылку: выберите",
            "💫 Бутылка указывает на",
            "🥂 Бутылка выбрала"
        ]
        chosen_bottle_phrase = random.choice(bottle_phrases)
        if additional_text:
            phrase = f"{chosen_bottle_phrase} {user1_name} {additional_text} {user2_name} 🔄"
        else:
            phrase = f"{chosen_bottle_phrase} {user1_name} и {user2_name} 🔄"
        reply_text = f"{phrase}\n[{user1_name}](tg://user?id={user1_id}) | [{user2_name}](tg://user?id={user2_id})"
        await update.message.reply_text(reply_text, parse_mode="Markdown")
        return

# Команда для бана пользователя
async def ban_user(update: Update, context: CallbackContext):
    if not update.message.chat.get_member(update.message.from_user.id).status in ["administrator", "creator"]:
        await update.message.reply_text("❗ Только администратор может забанить пользователя. ❗")
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /ban <ID пользователя> <время в секундах> ❌")
        return
    try:
        user_id = int(context.args[0])
        ban_time = int(context.args[1])
        unban_time = time.time() + ban_time
        banned_users[user_id] = unban_time
        await update.message.reply_text(f"🚫 Пользователь {user_id} забанен на {ban_time} секунд. 🚫")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID пользователя или времени. ❌")

# Обработчик inline-кнопок
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'rating_chai':
        update.message = query.message
        await rating_chai(update, context)
    elif data == 'ban_user':
        await query.edit_message_text("Чтобы забанить пользователя, используйте команду /ban <ID пользователя> <время в секундах>")
    elif data == 'info_what':
        await query.edit_message_text("Пример использования: напишите сообщение 'Рис инфа что <тема>', чтобы узнать процент по теме.")

# Команда /start
async def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📊 Рейтинг чая", callback_data='rating_chai')],
        [InlineKeyboardButton("🚫 Забанить пользователя", callback_data='ban_user')],
        [InlineKeyboardButton("💡 Инфа что", callback_data='info_what')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 Привет! Вот список команд:", reply_markup=reply_markup)
    await update.message.reply_text("⚫ Бот запущен. Ожидание сообщений...")

# Обработчик ошибок
def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Произошла ошибка: {context.error}")

# Основная функция
def main():
    TOKEN = os.environ.get("BOTTOKEN")
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан в переменных окружения.")
        return
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rating_chai", rating_chai))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    application.run_polling()
    logger.info("⚫ Бот запущен. Ожидание сообщений...")

if __name__ == '__main__':
    main()
