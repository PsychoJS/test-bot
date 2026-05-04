"""
هندلرهای فرمان برای مسدودسازی دسته‌جمعی کاربران
"""

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.bulk_ban_service import bulk_ban_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def start_bulk_ban_process(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    شروع فرایند مسدودسازی دسته‌جمعی کاربران
    """
    await callback.message.edit_text(
        '🛑 <b>مسدودسازی دسته‌جمعی کاربران</b>\n\n'
        'لیست Telegram ID ها را برای مسدودسازی وارد کنید.\n\n'
        '<b>فرمت‌های ورودی:</b>\n'
        '• یک ID در هر خط\n'
        '• با کاما\n'
        '• با فاصله\n\n'
        'مثال:\n'
        '<code>123456789\n'
        '987654321\n'
        '111222333</code>\n\n'
        'یا:\n'
        '<code>123456789, 987654321, 111222333</code>\n\n'
        'برای لغو از دستور /cancel استفاده کنید',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_users')]]
        ),
    )

    await state.set_state(AdminStates.waiting_for_bulk_ban_list)
    await callback.answer()


@admin_required
@error_handler
async def process_bulk_ban_list(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """
    پردازش لیست Telegram ID ها و اجرای مسدودسازی دسته‌جمعی
    """
    if not message.text:
        await message.answer(
            '❌ لطفاً یک پیام متنی با لیست Telegram ID ها ارسال کنید',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )
        return

    input_text = message.text.strip()

    if not input_text:
        await message.answer(
            '❌ لیست Telegram ID معتبر وارد کنید',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )
        return

    # Parsing IDs from text
    try:
        telegram_ids = await bulk_ban_service.parse_telegram_ids_from_text(input_text)
    except Exception as e:
        logger.error('Error parsing Telegram IDs', error=e)
        await message.answer(
            '❌ خطا در پردازش لیست ID. فرمت ورودی را بررسی کنید.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )
        return

    if not telegram_ids:
        await message.answer(
            '❌ Telegram ID معتبری در لیست یافت نشد',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )
        return

    if len(telegram_ids) > 1000:  # Limit on number of IDs at once
        await message.answer(
            f'❌ تعداد ID ها خیلی زیاد است ({len(telegram_ids)}). حداکثر: 1000',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )
        return

    # Performing bulk ban
    try:
        successfully_banned, not_found, error_ids = await bulk_ban_service.ban_users_by_telegram_ids(
            db=db,
            admin_user_id=db_user.id,
            telegram_ids=telegram_ids,
            reason='مسدودسازی دسته‌جمعی توسط ادمین',
            bot=message.bot,
            notify_admin=True,
            admin_name=db_user.full_name,
        )

        # Preparing result message
        result_text = '✅ <b>مسدودسازی دسته‌جمعی انجام شد</b>\n\n'
        result_text += '📊 <b>نتایج:</b>\n'
        result_text += f'✅ با موفقیت مسدود شد: {successfully_banned}\n'
        result_text += f'❌ یافت نشد: {not_found}\n'
        result_text += f'💥 خطاها: {len(error_ids)}\n\n'
        result_text += f'📈 کل پردازش شده: {len(telegram_ids)}'

        if successfully_banned > 0:
            result_text += f'\n🎯 درصد موفقیت: {round((successfully_banned / len(telegram_ids)) * 100, 1)}%'

        # Adding error info if any
        if error_ids:
            result_text += '\n\n⚠️ <b>Telegram ID با خطا:</b>\n'
            result_text += f'<code>{", ".join(map(str, error_ids[:10]))}</code>'  # Showing first 10
            if len(error_ids) > 10:
                result_text += f' و {len(error_ids) - 10} مورد دیگر...'

        await message.answer(
            result_text,
            parse_mode='HTML',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='👥 کاربران', callback_data='admin_users')]]
            ),
        )

    except Exception as e:
        logger.error('Error during bulk ban operation', error=e)
        await message.answer(
            '❌ در اجرای مسدودسازی دسته‌جمعی خطایی رخ داد',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🔙 بازگشت', callback_data='admin_users')]]
            ),
        )

    await state.clear()


def register_bulk_ban_handlers(dp):
    """
    ثبت هندلرهای فرمان برای مسدودسازی دسته‌جمعی
    """
    # Handler for starting bulk ban
    dp.callback_query.register(start_bulk_ban_process, lambda c: c.data == 'admin_bulk_ban_start')

    # Handler for text message with IDs to ban
    dp.message.register(process_bulk_ban_list, AdminStates.waiting_for_bulk_ban_list)
