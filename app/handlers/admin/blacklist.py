"""
هندلرهای پنل ادمین برای مدیریت لیست سیاه
"""

import html

import structlog
from aiogram import types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.services.blacklist_service import blacklist_service
from app.states import BlacklistStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_blacklist_settings(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    تنظیمات لیست سیاه را نمایش می‌دهد
    """
    logger.info('show_blacklist_settings handler called for user', from_user_id=callback.from_user.id)

    is_enabled = blacklist_service.is_blacklist_check_enabled()
    github_url = blacklist_service.get_blacklist_github_url()
    blacklist_count = len(await blacklist_service.get_all_blacklisted_users())

    status_text = '✅ فعال' if is_enabled else '❌ غیرفعال'
    url_text = github_url or 'تنظیم نشده'

    text = f"""
🔐 <b>تنظیمات لیست سیاه</b>

وضعیت: {status_text}
URL لیست سیاه: <code>{url_text}</code>
تعداد رکوردها: {blacklist_count}

عملیات:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(
                text='🔄 به‌روزرسانی لیست' if is_enabled else '🔄 به‌روزرسانی (غیرفعال)',
                callback_data='admin_blacklist_update',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='📋 مشاهده لیست' if is_enabled else '📋 مشاهده (غیرفعال)',
                callback_data='admin_blacklist_view',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='✏️ URL به GitHub' if not github_url else '✏️ تغییر URL', callback_data='admin_blacklist_set_url'
            )
        ],
        [
            types.InlineKeyboardButton(
                text='✅ فعال کردن' if not is_enabled else '❌ غیرفعال کردن', callback_data='admin_blacklist_toggle'
            )
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت به کاربران', callback_data='admin_users')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def toggle_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    وضعیت بررسی لیست سیاه را تغییر می‌دهد
    """
    # پیاده‌سازی فعلی از تنظیمات .env استفاده می‌کند
    # برای پیاده‌سازی کامل، باید یک سرویس تنظیمات ایجاد شود
    is_enabled = blacklist_service.is_blacklist_check_enabled()

    # در پیاده‌سازی واقعی، باید تنظیم را در پایگاه داده تغییر داد
    # یا در سیستم تنظیمات، اما فعلاً فقط وضعیت را نمایش می‌دهیم
    new_status = not is_enabled
    status_text = 'فعال' if new_status else 'غیرفعال'

    await callback.message.edit_text(
        f'وضعیت بررسی لیست سیاه: {status_text}\n\n'
        f'برای تغییر وضعیت بررسی لیست سیاه، مقدار\n'
        f'<code>BLACKLIST_CHECK_ENABLED</code> را در فایل <code>.env</code> تغییر دهید',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی وضعیت', callback_data='admin_blacklist_settings')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_blacklist_settings')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def update_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    لیست سیاه را از GitHub به‌روزرسانی می‌کند
    """
    success, message = await blacklist_service.force_update_blacklist()

    if success:
        await callback.message.edit_text(
            f'✅ {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📋 مشاهده لیست', callback_data='admin_blacklist_view')],
                    [types.InlineKeyboardButton(text='🔄 به‌روزرسانی دستی', callback_data='admin_blacklist_update')],
                    [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_blacklist_settings')],
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            f'❌ خطا در به‌روزرسانی: {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 تلاش مجدد', callback_data='admin_blacklist_update')],
                    [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_blacklist_settings')],
                ]
            ),
        )
    await callback.answer()


@admin_required
@error_handler
async def show_blacklist_users(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    لیست کاربران در لیست سیاه را نمایش می‌دهد
    """
    blacklist_users = await blacklist_service.get_all_blacklisted_users()

    if not blacklist_users:
        text = 'لیست سیاه خالی است'
    else:
        text = f'🔐 <b>لیست سیاه ({len(blacklist_users)} رکورد)</b>\n\n'

        # اولین ۲۰ رکورد را نمایش می‌دهیم
        for i, (tg_id, username, reason) in enumerate(blacklist_users[:20], 1):
            text += f'{i}. <code>{tg_id}</code> {html.escape(username or "")} — {html.escape(reason or "")}\n'

        if len(blacklist_users) > 20:
            text += f'\n... و {len(blacklist_users) - 20} رکورد دیگر'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_blacklist_view')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_blacklist_settings')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_set_blacklist_url(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    فرآیند تنظیم URL لیست سیاه را شروع می‌کند
    """
    current_url = blacklist_service.get_blacklist_github_url() or 'تنظیم نشده'

    await callback.message.edit_text(
        f'URL جدید فایل لیست سیاه در GitHub را وارد کنید\n\n'
        f'URL فعلی: {current_url}\n\n'
        f'مثال: https://raw.githubusercontent.com/username/repository/main/blacklist.txt\n\n'
        f'برای لغو از دستور /cancel استفاده کنید',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_blacklist_settings')]]
        ),
    )

    await state.set_state(BlacklistStates.waiting_for_blacklist_url)
    await callback.answer()


@admin_required
@error_handler
async def process_blacklist_url(message: types.Message, db_user: User, state: FSMContext):
    """
    URL وارد شده برای لیست سیاه را پردازش می‌کند
    """
    # پیام را فقط در صورتی پردازش می‌کنیم که ربات منتظر ورودی URL باشد
    if await state.get_state() != BlacklistStates.waiting_for_blacklist_url.state:
        return

    url = message.text.strip()

    # در پیاده‌سازی واقعی، باید URL را در سیستم تنظیمات ذخیره کرد
    # در پیاده‌سازی فعلی فقط پیام نمایش می‌دهیم
    if url.lower() in ['/cancel', 'لغو', 'cancel']:
        await message.answer(
            'تنظیم URL لغو شد',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='🔐 تنظیمات لیست سیاه', callback_data='admin_blacklist_settings'
                        )
                    ]
                ]
            ),
        )
        await state.clear()
        return

    # بررسی می‌کنیم که URL صحیح به نظر برسد
    if not url.startswith(('http://', 'https://')):
        await message.answer(
            '❌ URL نامعتبر. URL باید با http:// یا https:// شروع شود',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='🔐 تنظیمات لیست سیاه', callback_data='admin_blacklist_settings'
                        )
                    ]
                ]
            ),
        )
        return

    # در سیستم واقعی، اینجا باید URL را در پایگاه داده تنظیمات ذخیره کرد
    # یا در سیستم پیکربندی

    await message.answer(
        f'✅ URL لیست سیاه تنظیم شد:\n<code>{url}</code>\n\n'
        f'برای اعمال تغییرات، ربات را راه‌اندازی مجدد کنید یا مقدار\n'
        f'<code>BLACKLIST_GITHUB_URL</code> را در فایل <code>.env</code> تغییر دهید',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی لیست', callback_data='admin_blacklist_update')],
                [
                    types.InlineKeyboardButton(
                        text='🔐 تنظیمات لیست سیاه', callback_data='admin_blacklist_settings'
                    )
                ],
            ]
        ),
    )
    await state.clear()


def register_blacklist_handlers(dp):
    """
    ثبت هندلرهای لیست سیاه
    """
    # هندلر نمایش تنظیمات لیست سیاه
    # این هندلر باید از منوی کاربران یا به صورت جداگانه فراخوانی شود
    dp.callback_query.register(show_blacklist_settings, lambda c: c.data == 'admin_blacklist_settings')

    # هندلرهای تعامل با لیست سیاه
    dp.callback_query.register(toggle_blacklist, lambda c: c.data == 'admin_blacklist_toggle')

    dp.callback_query.register(update_blacklist, lambda c: c.data == 'admin_blacklist_update')

    dp.callback_query.register(show_blacklist_users, lambda c: c.data == 'admin_blacklist_view')

    dp.callback_query.register(start_set_blacklist_url, lambda c: c.data == 'admin_blacklist_set_url')

    # هندلر پیام‌ها برای تنظیم URL (فقط در وضعیت مورد نیاز کار می‌کند)
    dp.message.register(process_blacklist_url, StateFilter(BlacklistStates.waiting_for_blacklist_url))
