import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rules import clear_all_rules, create_or_update_rules, get_current_rules_content
from app.database.models import User
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.validators import get_html_help_text, validate_html_tags


def _safe_preview(html_text: str, limit: int = 500) -> str:
    """Creates a text preview, safely truncating HTML tags."""
    plain = re.sub(r'<[^>]+>', '', html_text)
    if len(plain) <= limit:
        return plain
    return plain[:limit] + '...'


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_rules_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
📋 <b>مدیریت قوانین سرویس</b>

قوانین فعلی هنگام ثبت‌نام و در منوی اصلی به کاربران نشان داده می‌شود.

یک عمل را انتخاب کنید:
"""

    keyboard = [
        [types.InlineKeyboardButton(text='📝 ویرایش قوانین', callback_data='admin_edit_rules')],
        [types.InlineKeyboardButton(text='👀 مشاهده قوانین', callback_data='admin_view_rules')],
        [types.InlineKeyboardButton(text='🗑️ پاک کردن قوانین', callback_data='admin_clear_rules')],
        [types.InlineKeyboardButton(text='ℹ️ راهنمای HTML', callback_data='admin_rules_help')],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_submenu_settings')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def view_current_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        is_valid, error_msg = validate_html_tags(current_rules)
        warning = ''
        if not is_valid:
            warning = f'\n\n⚠️ <b>توجه:</b> خطای HTML در قوانین یافت شد: {error_msg}'

        await callback.message.edit_text(
            f'📋 <b>قوانین فعلی سرویس</b>\n\n{current_rules}{warning}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='✏️ ویرایش', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='🗑️ پاک کردن', callback_data='admin_clear_rules')],
                    [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()
    except Exception as e:
        logger.error('Error displaying rules', error=e)
        await callback.message.edit_text(
            '❌ خطا در بارگذاری قوانین. احتمالاً متن دارای تگ‌های HTML نامعتبر است.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🗑️ پاک کردن قوانین', callback_data='admin_clear_rules')],
                    [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def start_edit_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        preview = _safe_preview(current_rules, 500)

        text = (
            '✏️ <b>ویرایش قوانین</b>\n\n'
            f'<b>قوانین فعلی:</b>\n<code>{preview}</code>\n\n'
            'متن جدید قوانین سرویس را ارسال کنید.\n\n'
            '<i>قالب‌بندی HTML پشتیبانی می‌شود. تمام تگ‌ها قبل از ذخیره بررسی می‌شوند.</i>\n\n'
            '💡 <b>نکته:</b> برای مشاهده تگ‌های پشتیبانی‌شده /html_help را فشار دهید'
        )

        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='ℹ️ راهنمای HTML', callback_data='admin_rules_help')],
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rules')],
                ]
            ),
        )

        await state.set_state(AdminStates.editing_rules_page)
        await callback.answer()

    except Exception as e:
        logger.error('Error starting rules edit', error=e)
        await callback.answer('❌ خطا در بارگذاری قوانین برای ویرایش', show_alert=True)


@admin_required
@error_handler
async def process_rules_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    new_rules = message.text

    if len(new_rules) > 4000:
        await message.answer('❌ متن قوانین بسیار طولانی است (حداکثر ۴۰۰۰ کاراکتر)')
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await message.answer(
            f'❌ <b>خطا در قالب‌بندی HTML:</b>\n{error_msg}\n\n'
            f'لطفاً خطاها را برطرف کرده و متن را مجدداً ارسال کنید.\n\n'
            f'💡 از /html_help برای مشاهده نحو صحیح استفاده کنید',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='ℹ️ راهنمای HTML', callback_data='admin_rules_help')],
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rules')],
                ]
            ),
        )
        return

    try:
        preview_text = f'📋 <b>پیش‌نمایش قوانین جدید:</b>\n\n{new_rules}\n\n'
        preview_text += '⚠️ <b>توجه!</b> قوانین جدید برای همه کاربران نمایش داده خواهد شد.\n\n'
        preview_text += 'تغییرات ذخیره شود؟'

        if len(preview_text) > 4000:
            preview_text = (
                '📋 <b>پیش‌نمایش قوانین جدید:</b>\n\n'
                f'{_safe_preview(new_rules, 500)}\n\n'
                f'⚠️ <b>توجه!</b> قوانین جدید برای همه کاربران نمایش داده خواهد شد.\n\n'
                f'متن قوانین: {len(new_rules)} کاراکتر\n'
                f'تغییرات ذخیره شود؟'
            )

        await message.answer(
            preview_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text='✅ ذخیره', callback_data='admin_save_rules'),
                        types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)

    except Exception as e:
        logger.error('Error displaying rules preview', error=e)
        await message.answer(
            '⚠️ <b>تأیید ذخیره قوانین</b>\n\n'
            f'قوانین جدید آماده ذخیره هستند ({len(new_rules)} کاراکتر).\n'
            f'تگ‌های HTML بررسی شده و صحیح هستند.\n\n'
            f'تغییرات ذخیره شود؟',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text='✅ ذخیره', callback_data='admin_save_rules'),
                        types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)


@admin_required
@error_handler
async def save_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    new_rules = data.get('new_rules')

    if not new_rules:
        await callback.answer('❌ خطا: متن قوانین یافت نشد', show_alert=True)
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await callback.message.edit_text(
            f'❌ <b>خطا در ذخیره:</b>\n{error_msg}\n\nقوانین به دلیل خطا در قالب‌بندی HTML ذخیره نشدند.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 تلاش مجدد', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='📋 به قوانین', callback_data='admin_rules')],
                ]
            ),
        )
        await state.clear()
        await callback.answer()
        return

    try:
        await create_or_update_rules(db=db, content=new_rules, language=db_user.language)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        from app.localization.texts import refresh_rules_cache

        await refresh_rules_cache(db_user.language)

        await callback.message.edit_text(
            '✅ <b>قوانین سرویس با موفقیت به‌روز شدند!</b>\n\n'
            '✓ قوانین جدید در پایگاه داده ذخیره شدند\n'
            '✓ تگ‌های HTML بررسی شده و صحیح هستند\n'
            '✓ کش قوانین پاک و به‌روز شد\n'
            '✓ قوانین برای کاربران نمایش داده خواهد شد\n\n'
            f'📊 اندازه متن: {len(new_rules)} کاراکتر',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='👀 مشاهده', callback_data='admin_view_rules')],
                    [types.InlineKeyboardButton(text='📋 به قوانین', callback_data='admin_rules')],
                ]
            ),
        )

        await state.clear()
        logger.info('Service rules updated by admin', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Error saving rules', error=e)
        await callback.message.edit_text(
            '❌ <b>خطا در ذخیره قوانین</b>\n\nهنگام نوشتن در پایگاه داده خطایی رخ داد. دوباره تلاش کنید.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 تلاش مجدد', callback_data='admin_save_rules')],
                    [types.InlineKeyboardButton(text='📋 به قوانین', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def clear_rules_confirmation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🗑️ <b>پاک کردن قوانین سرویس</b>\n\n'
        '⚠️ <b>توجه!</b> شما در حال حذف کامل تمام قوانین سرویس هستید.\n\n'
        'پس از پاک کردن، کاربران قوانین پیش‌فرض استاندارد را خواهند دید.\n\n'
        'این عمل قابل بازگشت نیست. ادامه می‌دهید؟',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text='✅ بله، پاک کن', callback_data='admin_confirm_clear_rules'),
                    types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rules'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_clear_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        await clear_all_rules(db, db_user.language)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        await callback.message.edit_text(
            '✅ <b>قوانین با موفقیت پاک شدند!</b>\n\n'
            '✓ تمام قوانین کاربری حذف شدند\n'
            '✓ اکنون از قوانین استاندارد استفاده می‌شود\n'
            '✓ کش قوانین پاک شد\n\n'
            'کاربران قوانین پیش‌فرض را خواهند دید.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📝 ایجاد قوانین جدید', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='👀 مشاهده فعلی', callback_data='admin_view_rules')],
                    [types.InlineKeyboardButton(text='📋 به قوانین', callback_data='admin_rules')],
                ]
            ),
        )

        logger.info('Rules cleared by admin', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Error clearing rules', error=e)
        await callback.answer('❌ خطا در پاک کردن قوانین', show_alert=True)


@admin_required
@error_handler
async def show_html_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    help_text = get_html_help_text()

    await callback.message.edit_text(
        f'ℹ️ <b>راهنمای قالب‌بندی HTML</b>\n\n{help_text}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📝 ویرایش قوانین', callback_data='admin_edit_rules')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_rules')],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_rules_management, F.data == 'admin_rules')
    dp.callback_query.register(view_current_rules, F.data == 'admin_view_rules')
    dp.callback_query.register(start_edit_rules, F.data == 'admin_edit_rules')
    dp.callback_query.register(save_rules, F.data == 'admin_save_rules')

    dp.callback_query.register(clear_rules_confirmation, F.data == 'admin_clear_rules')
    dp.callback_query.register(confirm_clear_rules, F.data == 'admin_confirm_clear_rules')

    dp.callback_query.register(show_html_help, F.data == 'admin_rules_help')

    dp.message.register(process_rules_edit, AdminStates.editing_rules_page)
