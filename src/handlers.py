import asyncio
import logging
import json
import io
import qrcode
from datetime import datetime, timedelta
from aiogram import Dispatcher, Router, F, Bot
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery, BufferedInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import config
from database import (
    StaticProfile, get_user, create_user, update_subscription, 
    get_all_users, create_static_profile, get_static_profiles, 
    User, Session, get_user_stats as db_user_stats, delete_user,
    get_pricing, update_pricing_tier, calculate_final_price
)
from monitoring import get_oracle_stats, get_ihor_stats
from functions import (
    create_awg_profile, delete_client_by_id, delete_client_by_name,
    get_client_stats, create_static_client, get_global_stats,
    get_online_users, set_client_enabled,
)

logger = logging.getLogger(__name__)

router = Router()

MAX_MESSAGE_LENGTH = 4096

class AdminStates(StatesGroup):
    ADD_TIME = State()
    REMOVE_TIME = State()
    CREATE_STATIC_PROFILE = State()
    SEND_MESSAGE = State()
    ADD_TIME_USER = State()
    REMOVE_TIME_USER = State()
    ADD_TIME_AMOUNT = State()
    REMOVE_TIME_AMOUNT = State()
    SEND_MESSAGE_TARGET = State()
    DELETE_USER = State()
    EDIT_PRICING = State()

def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбивает текст на части указанной максимальной длины"""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        part = text[:max_length]
        last_newline = part.rfind('\n')
        if last_newline != -1:
            part = part[:last_newline]
        parts.append(part)
        text = text[len(part):].lstrip()
    return parts

async def show_menu(bot: Bot, chat_id: int, message_id: int = None):
    """Функция для отображения меню (может как редактировать существующее сообщение, так и отправлять новое)"""
    user = await get_user(chat_id)
    if not user:
        return
    
    status = "Активна" if user.subscription_end > datetime.utcnow() else "Истекла"
    expire_date = user.subscription_end.strftime("%d-%m-%Y %H:%M") if status == "Активна" else status
    
    text = (
        f"**Имя профиля**: `{user.full_name}`\n"
        f"**Id**: `{user.telegram_id}`\n"
        f"**Подписка**: `{status}`\n"
        f"**Дата окончания подписки**: `{expire_date}`"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Продлить" if status=="Активна" else "💵 Оплатить", callback_data="renew_sub")
    builder.button(text="✅ Подключить", callback_data="connect")
    builder.button(text="📊 Статистика", callback_data="stats")
    builder.button(text="ℹ️ Помощь", callback_data="help")
    
    if user.is_admin:
        builder.button(text="⚠️ Админ. меню", callback_data="admin_menu")
    
    builder.adjust(2, 2, 1)
    
    if message_id:
        # Редактируем существующее сообщение
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )
    else:
        # Отправляем новое сообщение
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )

@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    logger.info(f"ℹ️  Start command from {message.from_user.id}")
    user = await get_user(message.from_user.id)
    
    # Обновляем данные пользователя если они изменились
    update_data = {}
    if user:
        if user.full_name != message.from_user.full_name:
            update_data["full_name"] = message.from_user.full_name
        if user.username != message.from_user.username:
            update_data["username"] = message.from_user.username
    else:
        is_admin = message.from_user.id in config.ADMINS
        user = await create_user(
            telegram_id=message.from_user.id, 
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            is_admin=is_admin
        )
        await message.answer(f"Добро пожаловать в VPN бота `{(await bot.get_me()).full_name}`!\nВам предоставлен **бесплатный** тестовый период на **3 дня**!", parse_mode='Markdown')
        await asyncio.sleep(2)
    
    # Обновляем данные если есть изменения
    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄 Updated user data: {message.from_user.id}")
    
    await show_menu(bot, message.from_user.id)

@router.message(Command("menu"))
async def menu_cmd(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return
    
    # Проверяем изменения данных
    update_data = {}
    if user.full_name != message.from_user.full_name:
        update_data["full_name"] = message.from_user.full_name
    if user.username != message.from_user.username:
        update_data["username"] = message.from_user.username
    
    # Обновляем данные если есть изменения
    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄 Updated user data in menu: {message.from_user.id}")
    
    await show_menu(bot, message.from_user.id)

@router.message(Command("renew"))
async def renew_cmd(message: Message, bot: Bot):
    """Слеш команда для продления/оплаты подписки"""
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return
    
    # Создаем клавиатуру с вариантами подписки
    builder = InlineKeyboardBuilder()
    pricing = await get_pricing()

    # Добавляем кнопки для каждого варианта подписки
    for months in sorted(pricing.keys()):
        price_info = pricing[months]
        final_price = calculate_final_price(price_info["base_price"], price_info["discount_percent"])

        discount_text = ""
        if price_info["discount_percent"] > 0:
            discount_text = f" (-{price_info['discount_percent']}%)"

        button_text = f"{months} мес. - ⭐ {final_price}{discount_text}"
        builder.button(text=button_text, callback_data=f"pay_{months}")

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await message.answer(
        "⭐ **Выберите период подписки:**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.message(Command("connect"))
async def connect_cmd(message: Message, bot: Bot):
    """Слеш команда для подключения к VPN"""
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return

    if user.subscription_end < datetime.utcnow():
        await message.answer("⚠️ Подписка истекла! Продлите подписку.")
        return

    if not user.awg_profile_data:
        await message.answer("⚙️ Создаем ваш VPN профиль...")
        profile_data = await create_awg_profile(user.telegram_id)

        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                if db_user:
                    db_user.awg_profile_data = json.dumps(profile_data)
                    session.commit()
            user = await get_user(user.telegram_id)
        else:
            await message.answer("🛑 Ошибка при создании профиля. Попробуйте позже.")
            return

    profile_data = safe_json_loads(user.awg_profile_data, default={})
    if not profile_data or not profile_data.get("config"):
        await message.answer("⚠️ У вас пока нет созданного профиля.")
        return

    config_file = BufferedInputFile(
        profile_data["config"].encode("utf-8"),
        filename="vpn.conf",
    )

    text = (
        "📲 Как подключить VPN\n\n"
        "1. Установите приложение AmneziaWG (App Store / Google Play), если ещё не установлено\n"
        "2. Откройте файл vpn.conf, приложенный выше — приложение AmneziaWG предложит импортировать его\n"
        "3. Подтвердите импорт и включите тоннель внутри приложения\n\n"
        "✅ Готово — VPN включён, интернет работает без ограничений 🚀"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В меню", callback_data="back_to_menu")
    builder.adjust(1)

    await message.answer_document(
        document=config_file,
        caption=text,
        reply_markup=builder.as_markup(),
    )

@router.message(Command("stats"))
async def stats_cmd(message: Message, bot: Bot):
    """Слеш команда для показа статистики"""
    user = await get_user(message.from_user.id)
    if not user or not user.awg_profile_data:
        await message.answer("⚠️ Профиль не создан")
        return

    await message.answer("⚙️ Загружаем вашу статистику...")
    profile_data = safe_json_loads(user.awg_profile_data, default={})
    client_id = profile_data.get("client_id")
    stats = await get_client_stats(client_id) if client_id else {"state": "not_found"}

    logger.debug(stats)

    state = stats.get("state", "unknown")
    state_labels = {
        "online": "🟢 В сети",
        "offline": "⚪️ Не в сети",
        "disabled": "🔴 Отключен",
    }
    state_text = state_labels.get(state, "❓ Неизвестно")

    download_mbps = (stats.get("downloadBps") or 0) / 1_000_000
    upload_mbps = (stats.get("uploadBps") or 0) / 1_000_000

    handshake_age = stats.get("handshakeAgeSeconds")
    if handshake_age is None:
        handshake_text = "никогда"
    elif handshake_age < 60:
        handshake_text = f"{handshake_age} сек. назад"
    else:
        handshake_text = f"{handshake_age // 60} мин. назад"

    text = (
        "📊 **Статус подключения:**\n\n"
        f"{state_text}\n"
        f"⬇️ Скорость сейчас: `{download_mbps:.2f} Мбит/с`\n"
        f"⬆️ Скорость сейчас: `{upload_mbps:.2f} Мбит/с`\n"
        f"🤝 Последнее подключение: `{handshake_text}`\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В меню", callback_data="back_to_menu")

    await message.answer(text, parse_mode='Markdown', reply_markup=builder.as_markup())

@router.message(Command("help"))
async def help_cmd(message: Message, bot: Bot):
    """Слеш команда для показа справки"""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В меню", callback_data="back_to_menu")
    
    text = (
        f"О боте:\n"
        #"<b>Разработчики:</b>\n"
        #"@QueenDekim | @cpn_moris\n"
        #"<i>Отдельное спасибо</i> @ascento <i>за помощь в разработке</i>\n"
        # "По вопросам технической поддержки: @your_username_or_chat"
    )
    
    await message.answer(text, parse_mode='HTML', reply_markup=builder.as_markup())

@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    text = (
        f"О боте:\n"
        #"<b>Разработчики:</b>\n"
        #"@QueenDekim | @cpn_moris\n"
        #"<i>Отдельное спасибо</i> @ascento <i>за помощь в разработке</i>\n"
        # "По вопросам технической поддержки: @your_username_or_chat"
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=builder.as_markup())

@router.callback_query(F.data == "renew_sub")
async def renew_subscription(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    pricing = await get_pricing()

    # Добавляем кнопки для каждого варианта подписки
    for months in sorted(pricing.keys()):
        price_info = pricing[months]
        final_price = calculate_final_price(price_info["base_price"], price_info["discount_percent"])

        discount_text = ""
        if price_info["discount_percent"] > 0:
            discount_text = f" (-{price_info['discount_percent']}%)"

        button_text = f"{months} мес. - ⭐ {final_price}{discount_text}"
        builder.button(text=button_text, callback_data=f"pay_{months}")

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "⭐ **Выберите период подписки:**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data.startswith("pay_"))
async def process_payment(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    
    try:
        months = int(callback.data.split("_")[1])
        pricing = await get_pricing()
        if months not in pricing:
            await callback.message.answer("❌ Неверный период подписки")
            return

        price_info = pricing[months]
        final_price = calculate_final_price(price_info["base_price"], price_info["discount_percent"])
        suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
        # Создаем инвойс для оплаты
        prices = [LabeledPrice(label=f"VPN подписка на {months} мес.", amount=final_price)]
        if config.PAYMENT_TOKEN:
            await bot.send_invoice(
                chat_id=callback.from_user.id,
                title=f"VPN подписка на {months} месяцев",
                description=f"Доступ к VPN сервису на {months} {suffix}",
                payload=f"subscription_{months}",
                provider_token="",
                currency="XTR",
                prices=prices,
                start_parameter="create_subscription",
            )
        else:
            await callback.message.answer("❌ Оплата временно недоступна")
    except Exception as e:
        logger.error(f"🛑 Payment error: {e}")
        await callback.message.answer("❌ Ошибка при создании счета на оплату")

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    try:
        # Извлекаем информацию из payload
        payload = message.successful_payment.invoice_payload
        if payload.startswith("subscription_"):
            months = int(payload.split("_")[1])
            pricing = await get_pricing()
            price_info = pricing.get(months, {"base_price": 0, "discount_percent": 0})
            final_price = calculate_final_price(price_info["base_price"], price_info["discount_percent"])  # Пересчитываем стоимость в звёздах
            
            # Получаем информацию о пользователе
            user = await get_user(message.from_user.id)
            if not user:
                await message.answer("❌ Ошибка: пользователь не найден")
                return
            
            # Определяем тип действия (покупка или продление)
            now = datetime.utcnow()
            action_type = "продлена" if user.subscription_end > now else "куплена"
            
            # Обновляем подписку
            success = await update_subscription(message.from_user.id, months)
            suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
            if success:
                # Получаем обновленные данные пользователя
                updated_user = await get_user(message.from_user.id)

                # Если у пользователя уже был профиль (например, отключённый после истечения) — включаем его обратно
                if updated_user and updated_user.awg_profile_data:
                    try:
                        profile_data = safe_json_loads(updated_user.awg_profile_data, default={})
                        client_id = profile_data.get("client_id")
                        if client_id:
                            await set_client_enabled(client_id, True)
                    except Exception as e:
                        logger.error(f"🛑 Error re-enabling client after payment: {e}")

                await message.answer(
                    f"✅ Оплата прошла успешно! Ваша подписка {action_type} на {months} {suffix}.\n\n"
                    "Спасибо за покупку! 🎉"
                )
                
                # Отправляем уведомление администраторам
                admin_message = (
                    f"{action_type.capitalize()} подписка пользователем "
                    f"`{user.full_name}` | `{user.telegram_id}` "
                    f"на {months} {suffix} - ⭐ {final_price}"
                )
                
                for admin_id in config.ADMINS:
                    try:
                        await bot.send_message(admin_id, admin_message, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"🛑 Failed to send notification to admin {admin_id}: {e}")
            else:
                await message.answer("❌ Ошибка при обновлении подписки")
    except Exception as e:
        logger.error(f"🛑 Successful payment processing error: {e}")
        await message.answer("❌ Ошибка при обработке платежа")

@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("🛑 Доступ запрещен!")
        return
    
    total, with_sub, without_sub = await db_user_stats()
    online_count = await get_online_users()
    
    text = (
        "**Административное меню**\n\n"
        f"**Всего пользователей**: `{total}`\n"
        f"**С подпиской/Без подписки**: `{with_sub}`/`{without_sub}`\n"
        f"**Онлайн**: `{online_count}` | **Офлайн**: `{with_sub - online_count}`"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="+ время", callback_data="admin_add_time")
    builder.button(text="- время", callback_data="admin_remove_time")
    builder.button(text="📋 Список пользователей", callback_data="admin_user_list")
    builder.button(text="🗑️ Удалить пользователя", callback_data="admin_delete_user")
    builder.button(text="📊 Статистика исп. сети", callback_data="admin_network_stats")
    builder.button(text="🖥️ Мониторинг серверов", callback_data="admin_server_monitoring")
    builder.button(text="💰 Тарифы", callback_data="admin_pricing")
    builder.button(text="📢 Рассылка", callback_data="admin_send_message")
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(2, 1, 1, 1, 1, 1, 1, 1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')

# Обработчики для управления временем подписки
@router.callback_query(F.data == "admin_add_time")
async def admin_add_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.ADD_TIME_USER)

@router.message(AdminStates.ADD_TIME_USER)
async def admin_add_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.ADD_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.ADD_TIME_AMOUNT)
async def admin_add_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
        return
    
    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )
        
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                if user.subscription_end > datetime.utcnow():
                    user.subscription_end += timedelta(seconds=total_seconds)
                else:
                    user.subscription_end = datetime.utcnow() + timedelta(seconds=total_seconds)
                session.commit()
                
                
                await message.answer(f"✅ Добавлено время пользователю {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

@router.callback_query(F.data == "admin_remove_time")
async def admin_remove_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.REMOVE_TIME_USER)

@router.message(AdminStates.REMOVE_TIME_USER)
async def admin_remove_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.REMOVE_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.REMOVE_TIME_AMOUNT)
async def admin_remove_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
        return
    
    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )
        
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                new_end = user.subscription_end - timedelta(seconds=total_seconds)
                # Проверяем, чтобы не ушло в прошлое
                if new_end < datetime.utcnow():
                    new_end = datetime.utcnow()
                user.subscription_end = new_end
                session.commit()
                
                
                await message.answer(f"✅ Удалено время у пользователя {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

# Обработчики для вывода списка пользователей
@router.callback_query(F.data == "admin_user_list")
async def admin_user_list(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="user_list_active")
    builder.button(text="🛑 Без подписки", callback_data="user_list_inactive")
    builder.button(text="⏱️ Статические профили", callback_data="static_profiles_menu")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1, 1, 1)
    await callback.message.edit_text("**Выберите фильтр**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "user_list_active")
async def handle_user_list_active(callback: CallbackQuery):
    users = await get_all_users(with_subscription=True)
    await callback.answer()
    if not users:
        await callback.answer("Нет пользователей с активной подпиской")
        return
    
    text = "👤 <b>Пользователи с активной подпиской:</b>\n\n"
    for user in users:
        expire_date = user.subscription_end.strftime("%d.%m.%Y %H:%M")
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>) - до <code>{expire_date}</code>\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи с активной подпиской (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data == "user_list_inactive")
async def handle_user_list_inactive(callback: CallbackQuery):
    await callback.answer()
    users = await get_all_users(with_subscription=False)
    if not users:
        await callback.answer("Нет пользователей без подписки")
        return
    
    text = "👤 <b>Пользователи без подписки:</b>\n\n"
    for user in users:
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>)\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи без подписки (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

# Обработчики для рассылки сообщений
@router.callback_query(F.data == "admin_send_message")
async def admin_send_message_start(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="↩️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    target = callback.data.split("_")[1]
    await state.update_data(target=target)
    await callback.message.answer("Введите сообщение для рассылки:")
    await state.set_state(AdminStates.SEND_MESSAGE)

@router.message(AdminStates.SEND_MESSAGE)
async def admin_send_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target = data['target']
    text = message.text
    
    users = []
    if target == "active":
        users = await get_all_users(with_subscription=True)
    elif target == "inactive":
        users = await get_all_users(with_subscription=False)
    else:  # all
        users = await get_all_users()
    
    success = 0
    failed = 0
    
    for user in users:
        try:
            await bot.send_message(user.telegram_id, text)
            success += 1
        except Exception as e:
            logger.error(f"🛑 Ошибка отправки сообщения {user.telegram_id}: {e}")
            failed += 1
    
    await message.answer(
        f"📨 Результаты рассылки:\n\n"
        f"• Успешно: {success}\n"
        f"• Не удалось: {failed}\n"
        f"• Всего: {len(users)}"
    )
    await state.clear()

# Остальные обработчики остаются без изменений
@router.callback_query(F.data == "static_profiles_menu")
async def static_profiles_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Добавить статический профиль", callback_data="static_profile_add")
    builder.button(text="📋 Вывести статические профили", callback_data="static_profile_list")
    builder.button(text="⬅️ Назад", callback_data="admin_user_list")
    builder.adjust(1)
    await callback.message.edit_text("**Выберите действие**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "static_profile_add")
async def static_profile_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите имя для статического профиля:")
    await state.set_state(AdminStates.CREATE_STATIC_PROFILE)

@router.message(AdminStates.CREATE_STATIC_PROFILE)
async def process_static_profile_name(message: Message, state: FSMContext):
    profile_name = message.text
    profile_data = await create_static_client(profile_name)

    if profile_data:
        config_file = BufferedInputFile(
            profile_data["config"].encode("utf-8"),
            filename="vpn.conf",
        )

        await create_static_profile(profile_name, profile_data["config"])
        profiles = await get_static_profiles()
        for profile in profiles:
            if profile.name == profile_name:
                id = profile.id
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{id}")
        await message.answer_document(
            document=config_file,
            caption=f"Профиль создан: {profile_name}",
            reply_markup=builder.as_markup(),
        )
    else:
        await message.answer("Ошибка при создании профиля")

    await state.clear()

@router.callback_query(F.data == "static_profile_list")
async def static_profile_list(callback: CallbackQuery):
    profiles = await get_static_profiles()
    if not profiles:
        await callback.answer("Нет статических профилей")
        return
    
    for profile in profiles:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{profile.id}")
        
        # Генерация QR-кода локально
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(profile.awg_config)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Сохранение в буфер
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        photo = BufferedInputFile(img_byte_arr.getvalue(), filename="qr.png")
        
        await callback.message.answer_photo(
            photo=photo,
            caption=f"**{profile.name}**\n`{profile.awg_config}`", 
            reply_markup=builder.as_markup(), 
            parse_mode='Markdown'
        )

@router.callback_query(F.data.startswith("delete_static_"))
async def handle_delete_static_profile(callback: CallbackQuery):
    try:
        profile_id = int(callback.data.split("_")[-1])
        
        with Session() as session:
            profile = session.query(StaticProfile).filter_by(id=profile_id).first()
            if not profile:
                await callback.answer("⚠️ Профиль не найден")
                return
            
            success = await delete_client_by_name(profile.name)
            if not success:
                logger.error(f"🛑 Ошибка удаления клиента из инбаунда: {profile.name}")
            
            session.delete(profile)
            session.commit()
        
        await callback.answer("✅ Профиль удален!")
        await callback.message.delete()
    except Exception as e:
        logger.error(f"🛑 Ошибка при удалении статического профиля: {e}")
        await callback.answer("⚠️ Ошибка при удалении профиля")

@router.callback_query(F.data == "connect")
async def connect_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("🛑 Ошибка профиля")
        return

    if user.subscription_end < datetime.utcnow():
        await callback.answer("⚠️ Подписка истекла! Продлите подписку.")
        return

    if not user.awg_profile_data:
        await callback.message.edit_text("⚙️ Создаем ваш VPN профиль...")
        profile_data = await create_awg_profile(user.telegram_id)

        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                if db_user:
                    db_user.awg_profile_data = json.dumps(profile_data)
                    session.commit()
            user = await get_user(user.telegram_id)
        else:
            await callback.message.answer("🛑 Ошибка при создании профиля. Попробуйте позже.")
            return

    profile_data = safe_json_loads(user.awg_profile_data, default={})
    if not profile_data or not profile_data.get("config"):
        await callback.message.answer("⚠️ У вас пока нет созданного профиля.")
        return

    config_file = BufferedInputFile(
        profile_data["config"].encode("utf-8"),
        filename="vpn.conf",
    )

    text = (
        "📲 Как подключить VPN\n\n"
        "1. Установите приложение AmneziaWG (App Store / Google Play), если ещё не установлено\n"
        "2. Откройте файл vpn.conf, приложенный выше — приложение AmneziaWG предложит импортировать его\n"
        "3. Подтвердите импорт и включите тоннель внутри приложения\n\n"
        "✅ Готово — VPN включён, интернет работает без ограничений 🚀"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await callback.message.answer_document(
        document=config_file,
        caption=text,
        reply_markup=builder.as_markup(),
    )
    await callback.message.delete()

@router.callback_query(F.data == "stats")
async def user_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.awg_profile_data:
        await callback.answer("⚠️ Профиль не создан")
        return
    await callback.message.edit_text("⚙️ Загружаем вашу статистику...")
    profile_data = safe_json_loads(user.awg_profile_data, default={})
    client_id = profile_data.get("client_id")
    stats = await get_client_stats(client_id) if client_id else {"state": "not_found"}

    logger.debug(stats)

    state = stats.get("state", "unknown")
    state_labels = {
        "online": "🟢 В сети",
        "offline": "⚪️ Не в сети",
        "disabled": "🔴 Отключен",
    }
    state_text = state_labels.get(state, "❓ Неизвестно")

    download_mbps = (stats.get("downloadBps") or 0) / 1_000_000
    upload_mbps = (stats.get("uploadBps") or 0) / 1_000_000

    handshake_age = stats.get("handshakeAgeSeconds")
    if handshake_age is None:
        handshake_text = "никогда"
    elif handshake_age < 60:
        handshake_text = f"{handshake_age} сек. назад"
    else:
        handshake_text = f"{handshake_age // 60} мин. назад"

    await callback.message.delete()
    text = (
        "📊 **Статус подключения:**\n\n"
        f"{state_text}\n"
        f"⬇️ Скорость сейчас: `{download_mbps:.2f} Мбит/с`\n"
        f"⬆️ Скорость сейчас: `{upload_mbps:.2f} Мбит/с`\n"
        f"🤝 Последнее подключение: `{handshake_text}`\n"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    await callback.message.answer(text, parse_mode='Markdown', reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_network_stats")
async def network_stats(callback: CallbackQuery):
    stats = await get_global_stats()

    download_mbps = stats.get("download", 0) / 1_000_000
    upload_mbps = stats.get("upload", 0) / 1_000_000

    await callback.answer()
    text = (
        "📊 **Статистика сети (сейчас):**\n\n"
        f"🔼 Upload - `{upload_mbps:.2f} Мбит/с` | 🔽 Download - `{download_mbps:.2f} Мбит/с`"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    await callback.message.edit_text(text, parse_mode='Markdown', reply_markup=builder.as_markup())

@router.callback_query(F.data == "admin_delete_user")
async def admin_delete_user_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса удаления пользователя"""
    await callback.answer()
    await callback.message.answer("🗑️ **Удаление пользователя**\n\nВведите Telegram ID пользователя для удаления:", parse_mode='Markdown')
    await state.set_state(AdminStates.DELETE_USER)

@router.message(AdminStates.DELETE_USER)
async def admin_delete_user_process(message: Message, state: FSMContext):
    """Обработка ввода Telegram ID для удаления"""
    try:
        telegram_id = int(message.text)
        
        # Проверяем существование пользователя
        user = await get_user(telegram_id)
        
        if not user:
            await message.answer(f"❌ Пользователь с Telegram ID `{telegram_id}` не найден")
            await state.clear()
            return
        
        # Подтверждение удаления
        username = f"@{user.username}" if user.username else "отсутствует"
        text = (
            f"⚠️ **Подтвердите удаление:**\n\n"
            f"👤 **Имя:** `{user.full_name}`\n"
            f"📱 **Username:** `{username}`\n"
            f"🆔 **Telegram ID:** `{user.telegram_id}`\n"
            f"📅 **Регистрация:** `{user.registration_date.strftime('%d-%m-%Y %H:%M')}`\n"
            f"⏰ **Подписка до:** `{user.subscription_end.strftime('%d-%m-%Y %H:%M')}`\n"
            f"🔧 **Профиль:** `{'Есть' if user.awg_profile_data else 'Нет'}`\n\n"
            f"❗️ **Это действие необратимо!**"
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить удаление", callback_data=f"confirm_delete_{telegram_id}")
        builder.button(text="❌ Отмена", callback_data="admin_menu")
        builder.adjust(1)
        
        await message.answer(text, parse_mode='Markdown', reply_markup=builder.as_markup())
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Ошибка: Telegram ID должен быть числом")
    except Exception as e:
        logger.error(f"🛑 Error in admin_delete_user_process: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.clear()

@router.callback_query(F.data.startswith("confirm_delete_"))
async def admin_confirm_delete_user(callback: CallbackQuery):
    """Подтверждение и удаление пользователя"""
    await callback.answer()
    
    try:
        telegram_id = int(callback.data.split("_")[2])
        
        # Удаляем пользователя
        result = await delete_user(telegram_id)
        
        if result:
            text = (
                f"✅ **Пользователь удалён**\n\n"
                f"🆔 Telegram ID: `{telegram_id}`\n\n"
                f"Профиль в 3x-ui также был удалён (если существовал)."
            )
        else:
            text = (
                f"❌ **Ошибка удаления**\n\n"
                f"🆔 Telegram ID: `{telegram_id}`\n\n"
                f"Пользователь не найден в базе данных."
            )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
        
        await callback.message.edit_text(text, parse_mode='Markdown', reply_markup=builder.as_markup())
        
    except Exception as e:
        logger.error(f"🛑 Error in admin_confirm_delete_user: {e}")
        await callback.message.answer(f"❌ Ошибка при удалении: {str(e)}")

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if callback.message.photo or callback.message.document:
        await callback.message.delete()
        await show_menu(bot, callback.from_user.id)
    else:
        await show_menu(bot, callback.from_user.id, callback.message.message_id)

def setup_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("✅ Handlers setup completed")

def safe_json_loads(data, default=None):
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default


@router.callback_query(F.data == "admin_server_monitoring")
async def admin_server_monitoring(callback: CallbackQuery):
    """Мониторинг обоих серверов: CPU, RAM, Swap, диск"""
    await callback.answer("⏳ Собираем данные с обоих серверов...")

    nl_stats = await get_oracle_stats()
    ru_stats = await get_ihor_stats()

    text = (
        "🖥️ **Мониторинг серверов**\n\n"
        "🇳🇱 **NL**\n"
        f"CPU: `{nl_stats['cpu']}`\n"
        f"RAM: `{nl_stats['ram']}`\n"
        f"Swap: `{nl_stats['swap']}`\n"
        f"Диск: `{nl_stats['disk']}`\n\n"
        "🇷🇺 **RU**\n"
        f"CPU: `{ru_stats['cpu']}`\n"
        f"RAM: `{ru_stats['ram']}`\n"
        f"Swap: `{ru_stats['swap']}`\n"
        f"Диск: `{ru_stats['disk']}`\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="admin_server_monitoring")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1, 1)

    await callback.message.edit_text(text, parse_mode='Markdown', reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin_pricing")
async def admin_pricing(callback: CallbackQuery):
    """Показывает текущие тарифы с кнопками изменения"""
    pricing = await get_pricing()

    builder = InlineKeyboardBuilder()
    text = "💰 **Текущие тарифы**\n\n"

    for months in sorted(pricing.keys()):
        price_info = pricing[months]
        final_price = calculate_final_price(price_info["base_price"], price_info["discount_percent"])
        discount_text = f" (-{price_info['discount_percent']}%)" if price_info["discount_percent"] > 0 else ""
        text += f"{months} мес. — ⭐ {final_price}{discount_text} (база: {price_info['base_price']})\n"
        builder.button(text=f"✏️ {months} мес.", callback_data=f"edit_price_{months}")

    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(2, 2, 1)

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("edit_price_"))
async def edit_price_start(callback: CallbackQuery, state: FSMContext):
    """Начало изменения цены конкретного тарифа"""
    months = int(callback.data.split("_")[-1])
    await state.update_data(edit_months=months)
    await state.set_state(AdminStates.EDIT_PRICING)
    await callback.answer()
    await callback.message.answer(
        f"Введите новую базовую цену (в ⭐) для тарифа {months} мес.:"
    )


@router.message(AdminStates.EDIT_PRICING)
async def edit_price_process(message: Message, state: FSMContext):
    """Обработка введённой новой цены"""
    data = await state.get_data()
    months = data.get("edit_months")

    try:
        new_price = int(message.text)
        if new_price <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("⚠️ Введите положительное целое число.")
        return

    success = await update_pricing_tier(months, new_price)
    await state.clear()

    if success:
        await message.answer(f"✅ Цена для тарифа {months} мес. обновлена: ⭐ {new_price}")
    else:
        await message.answer("🛑 Не удалось обновить цену — тариф не найден.")
