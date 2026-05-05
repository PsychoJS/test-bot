import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id, get_promo_groups_with_counts
from app.database.crud.promocode import (
    create_promocode,
    delete_promocode,
    get_promocode_by_code,
    get_promocode_by_id,
    get_promocode_statistics,
    get_promocodes_count,
    get_promocodes_list,
    update_promocode,
)
from app.database.models import PromoCodeType, User
from app.keyboards.admin import (
    get_admin_pagination_keyboard,
    get_admin_promocodes_keyboard,
    get_promocode_type_keyboard,
)
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_promocodes_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = f"""
🎫 <b>مدیریت کدهای تخفیف</b>

📊 <b>آمار:</b>
- مجموع کدها: {total_codes}
- فعال: {active_codes}
- غیرفعال: {total_codes - active_codes}

یک عملیات را انتخاب کنید:
"""

    await callback.message.edit_text(text, reply_markup=get_admin_promocodes_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    limit = 10
    offset = (page - 1) * limit

    promocodes = await get_promocodes_list(db, offset=offset, limit=limit)
    total_count = await get_promocodes_count(db)
    total_pages = (total_count + limit - 1) // limit

    if not promocodes:
        await callback.message.edit_text(
            '🎫 کد تخفیفی یافت نشد',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_promocodes')]]
            ),
        )
        await callback.answer()
        return

    text = f'🎫 <b>لیست کدهای تخفیف</b> (صفحه {page}/{total_pages})\n\n'
    keyboard = []

    for promo in promocodes:
        status_emoji = '✅' if promo.is_active else '❌'
        type_emoji = {
            'balance': '💰',
            'subscription_days': '📅',
            'trial_subscription': '🎁',
            'promo_group': '🏷️',
            'discount': '💸',
        }.get(promo.type, '🎫')

        text += f'{status_emoji} {type_emoji} <code>{promo.code}</code>\n'
        text += f'📊 استفاده‌ها: {promo.current_uses}/{promo.max_uses}\n'

        if promo.type == PromoCodeType.BALANCE.value:
            text += f'💰 پاداش: {settings.format_price(promo.balance_bonus_kopeks)}\n'
        elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
            text += f'📅 روزها: {promo.subscription_days}\n'
        elif promo.type == PromoCodeType.PROMO_GROUP.value:
            if promo.promo_group:
                text += f'🏷️ گروه تبلیغاتی: {html.escape(promo.promo_group.name)}\n'
        elif promo.type == PromoCodeType.DISCOUNT.value:
            discount_hours = promo.subscription_days
            if discount_hours > 0:
                text += f'💸 تخفیف: {promo.balance_bonus_kopeks}% ({discount_hours} ساعت)\n'
            else:
                text += f'💸 تخفیف: {promo.balance_bonus_kopeks}% (تا خرید)\n'

        if promo.valid_until:
            text += f'⏰ تا: {format_datetime(promo.valid_until)}\n'

        keyboard.append([types.InlineKeyboardButton(text=f'🎫 {promo.code}', callback_data=f'promo_manage_{promo.id}')])

        text += '\n'

    if total_pages > 1:
        pagination_row = get_admin_pagination_keyboard(
            page, total_pages, 'admin_promo_list', 'admin_promocodes', db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='➕ ایجاد', callback_data='admin_promo_create')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_promocodes')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocodes_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Handler for promo code list pagination."""
    try:
        page = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        page = 1
    await show_promocodes_list(callback, db_user, db, page=page)


@admin_required
@error_handler
async def show_promocode_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    status_emoji = '✅' if promo.is_active else '❌'
    type_emoji = {
        'balance': '💰',
        'subscription_days': '📅',
        'trial_subscription': '🎁',
        'promo_group': '🏷️',
        'discount': '💸',
    }.get(promo.type, '🎫')

    text = f"""
🎫 <b>مدیریت کد تخفیف</b>

{type_emoji} <b>کد:</b> <code>{promo.code}</code>
{status_emoji} <b>وضعیت:</b> {'فعال' if promo.is_active else 'غیرفعال'}
📊 <b>استفاده‌ها:</b> {promo.current_uses}/{promo.max_uses}
"""

    if promo.type == PromoCodeType.BALANCE.value:
        text += f'💰 <b>پاداش:</b> {settings.format_price(promo.balance_bonus_kopeks)}\n'
    elif promo.type == PromoCodeType.SUBSCRIPTION_DAYS.value:
        text += f'📅 <b>روزها:</b> {promo.subscription_days}\n'
    elif promo.type == PromoCodeType.PROMO_GROUP.value:
        if promo.promo_group:
            text += f'🏷️ <b>گروه تبلیغاتی:</b> {html.escape(promo.promo_group.name)} (اولویت: {promo.promo_group.priority})\n'
        elif promo.promo_group_id:
            text += f'🏷️ <b>شناسه گروه تبلیغاتی:</b> {promo.promo_group_id} (یافت نشد)\n'
    elif promo.type == PromoCodeType.DISCOUNT.value:
        discount_hours = promo.subscription_days
        if discount_hours > 0:
            text += f'💸 <b>تخفیف:</b> {promo.balance_bonus_kopeks}% (مدت: {discount_hours} ساعت)\n'
        else:
            text += f'💸 <b>تخفیف:</b> {promo.balance_bonus_kopeks}% (تا اولین خرید)\n'

    if promo.valid_until:
        text += f'⏰ <b>معتبر تا:</b> {format_datetime(promo.valid_until)}\n'

    first_purchase_only = getattr(promo, 'first_purchase_only', False)
    first_purchase_emoji = '✅' if first_purchase_only else '❌'
    text += f'🆕 <b>فقط اولین خرید:</b> {first_purchase_emoji}\n'

    text += f'📅 <b>ایجاد شده:</b> {format_datetime(promo.created_at)}\n'

    first_purchase_btn_text = '🆕 اولین خرید: ✅' if first_purchase_only else '🆕 اولین خرید: ❌'

    keyboard = [
        [
            types.InlineKeyboardButton(text='✏️ ویرایش', callback_data=f'promo_edit_{promo.id}'),
            types.InlineKeyboardButton(text='🔄 تغییر وضعیت', callback_data=f'promo_toggle_{promo.id}'),
        ],
        [types.InlineKeyboardButton(text=first_purchase_btn_text, callback_data=f'promo_toggle_first_{promo.id}')],
        [
            types.InlineKeyboardButton(text='📊 آمار', callback_data=f'promo_stats_{promo.id}'),
            types.InlineKeyboardButton(text='🗑️ حذف', callback_data=f'promo_delete_{promo.id}'),
        ],
        [types.InlineKeyboardButton(text='⬅️ به لیست', callback_data='admin_promo_list')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_promocode_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    text = f"""
✏️ <b>ویرایش کد تخفیف</b> <code>{promo.code}</code>

💰 <b>پارامترهای فعلی:</b>
"""

    if promo.type == PromoCodeType.BALANCE.value:
        text += f'• پاداش: {settings.format_price(promo.balance_bonus_kopeks)}\n'
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        text += f'• روزها: {promo.subscription_days}\n'

    text += f'• استفاده‌ها: {promo.current_uses}/{promo.max_uses}\n'

    if promo.valid_until:
        text += f'• تا: {format_datetime(promo.valid_until)}\n'
    else:
        text += '• مدت: نامحدود\n'

    text += '\nپارامتری را برای تغییر انتخاب کنید:'

    keyboard = [
        [types.InlineKeyboardButton(text='📅 تاریخ انقضا', callback_data=f'promo_edit_date_{promo.id}')],
        [types.InlineKeyboardButton(text='📊 تعداد استفاده‌ها', callback_data=f'promo_edit_uses_{promo.id}')],
    ]

    if promo.type == PromoCodeType.BALANCE.value:
        keyboard.insert(
            1, [types.InlineKeyboardButton(text='💰 مقدار پاداش', callback_data=f'promo_edit_amount_{promo.id}')]
        )
    elif promo.type in [PromoCodeType.SUBSCRIPTION_DAYS.value, PromoCodeType.TRIAL_SUBSCRIPTION.value]:
        keyboard.insert(
            1, [types.InlineKeyboardButton(text='📅 تعداد روزها', callback_data=f'promo_edit_days_{promo.id}')]
        )

    keyboard.extend([[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'promo_manage_{promo.id}')]])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_date(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='date')

    text = f"""
📅 <b>تغییر تاریخ انقضای کد تخفیف</b>

تعداد روزها تا انقضا را وارد کنید (از لحظه حال):
• <b>0</b> را برای کد تخفیف نامحدود وارد کنید
• یک عدد مثبت برای تعیین مدت وارد کنید

<i>مثال: 30 (کد تخفیف 30 روز معتبر خواهد بود)</i>

شناسه کد تخفیف: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_expiry)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_amount(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='amount')

    text = f"""
💰 <b>تغییر مبلغ بونوس کد تخفیف</b>

مبلغ جدید را وارد کنید:
<i>مثال: 500</i>

شناسه کد تخفیف: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_days(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    # FIX: take the last element as ID
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='days')

    text = f"""
📅 <b>تغییر تعداد روزهای اشتراک</b>

تعداد روزهای جدید را وارد کنید:
<i>مثال: 30</i>

شناسه کد تخفیف: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_value)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_promocode_uses(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    await state.update_data(editing_promo_id=promo_id, edit_action='uses')

    text = f"""
📊 <b>تغییر حداکثر تعداد استفاده</b>

تعداد استفاده جدید را وارد کنید:
• <b>0</b> را برای استفاده نامحدود وارد کنید
• یک عدد مثبت برای محدودسازی وارد کنید

<i>مثال: 100</i>

شناسه کد تخفیف: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data=f'promo_edit_{promo_id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def start_promocode_creation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await callback.message.edit_text(
        '🎫 <b>ایجاد کد تخفیف</b>\n\nنوع کد تخفیف را انتخاب کنید:',
        reply_markup=get_promocode_type_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_promocode_type(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    promo_type = callback.data.split('_')[-1]

    type_names = {
        'balance': '💰 شارژ موجودی',
        'days': '📅 روزهای اشتراک',
        'trial': '🎁 اشتراک آزمایشی',
        'group': '🏷️ گروه تبلیغاتی',
        'discount': '💸 تخفیف یک‌بار مصرف',
    }

    await state.update_data(promocode_type=promo_type)

    await callback.message.edit_text(
        f'🎫 <b>ایجاد کد تخفیف</b>\n\n'
        f'نوع: {type_names.get(promo_type, promo_type)}\n\n'
        f'کد تخفیف را وارد کنید (فقط حروف لاتین و اعداد):',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_promocodes')]]
        ),
    )

    await state.set_state(AdminStates.creating_promocode)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_code(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer('❌ کد باید فقط شامل حروف لاتین و اعداد باشد (3-20 کاراکتر)')
        return

    existing = await get_promocode_by_code(db, code)
    if existing:
        await message.answer('❌ کد تخفیف با این کد از قبل وجود دارد')
        return

    await state.update_data(promocode_code=code)

    data = await state.get_data()
    promo_type = data.get('promocode_type')

    if promo_type == 'balance':
        await message.answer(f'💰 <b>کد تخفیف:</b> <code>{code}</code>\n\nمبلغ شارژ موجودی را وارد کنید:')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'days':
        await message.answer(f'📅 <b>کد تخفیف:</b> <code>{code}</code>\n\nتعداد روزهای اشتراک را وارد کنید:')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'trial':
        await message.answer(f'🎁 <b>کد تخفیف:</b> <code>{code}</code>\n\nتعداد روزهای اشتراک آزمایشی را وارد کنید:')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'discount':
        await message.answer(f'💸 <b>کد تخفیف:</b> <code>{code}</code>\n\nدرصد تخفیف را وارد کنید (1-100):')
        await state.set_state(AdminStates.setting_promocode_value)
    elif promo_type == 'group':
        # Show promo group selection
        groups_with_counts = await get_promo_groups_with_counts(db, limit=50)

        if not groups_with_counts:
            await message.answer(
                '❌ گروه تبلیغاتی یافت نشد. حداقل یک گروه تبلیغاتی ایجاد کنید.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_promocodes')]]
                ),
            )
            await state.clear()
            return

        keyboard = []
        text = f'🏷️ <b>کد تخفیف:</b> <code>{code}</code>\n\nگروه تبلیغاتی را برای تخصیص انتخاب کنید:\n\n'

        for promo_group, user_count in groups_with_counts:
            text += (
                f'• {html.escape(promo_group.name)} (اولویت: {promo_group.priority}, کاربران: {user_count})\n'
            )
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{promo_group.name} (↑{promo_group.priority})',
                        callback_data=f'promo_select_group_{promo_group.id}',
                    )
                ]
            )

        keyboard.append([types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_promocodes')])

        await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
        await state.set_state(AdminStates.selecting_promo_group)


@admin_required
@error_handler
async def process_promo_group_selection(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession
):
    """Handle promo group selection for promocode"""
    try:
        promo_group_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه گروه تبلیغاتی', show_alert=True)
        return

    promo_group = await get_promo_group_by_id(db, promo_group_id)
    if not promo_group:
        await callback.answer('❌ گروه تبلیغاتی یافت نشد', show_alert=True)
        return

    await state.update_data(promo_group_id=promo_group_id, promo_group_name=promo_group.name)

    await callback.message.edit_text(
        f'🏷️ <b>کد تخفیف برای گروه تبلیغاتی</b>\n\n'
        f'گروه تبلیغاتی: {html.escape(promo_group.name)}\n'
        f'اولویت: {promo_group.priority}\n\n'
        f'📊 تعداد استفاده از کد تخفیف را وارد کنید (یا 0 برای نامحدود):'
    )

    await state.set_state(AdminStates.setting_promocode_uses)
    await callback.answer()


@admin_required
@error_handler
async def process_promocode_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_value(message, db_user, state, db)
        return

    try:
        value = int(message.text.strip())

        promo_type = data.get('promocode_type')

        if promo_type == 'balance' and (value < 1 or value > 10000):
            await message.answer('❌ مبلغ باید بین 1 تا 10,000 باشد')
            return
        if promo_type in ['days', 'trial'] and (value < 1 or value > 3650):
            await message.answer('❌ تعداد روزها باید بین 1 تا 3650 باشد')
            return
        if promo_type == 'discount' and (value < 1 or value > 100):
            await message.answer('❌ درصد تخفیف باید بین 1 تا 100 باشد')
            return

        await state.update_data(promocode_value=value)

        await message.answer('📊 تعداد استفاده از کد تخفیف را وارد کنید (یا 0 برای نامحدود):')
        await state.set_state(AdminStates.setting_promocode_uses)

    except ValueError:
        await message.answer('❌ یک عدد معتبر وارد کنید')


async def handle_edit_value(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')
    edit_action = data.get('edit_action')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ کد تخفیف یافت نشد')
        await state.clear()
        return

    try:
        value = int(message.text.strip())

        if edit_action == 'amount':
            if value < 1 or value > 10000:
                await message.answer('❌ مبلغ باید بین 1 تا 10,000 باشد')
                return

            await update_promocode(db, promo, balance_bonus_kopeks=value * 100)
            await message.answer(
                f'✅ مبلغ بونوس به {value} تغییر یافت',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🎫 به کد تخفیف', callback_data=f'promo_manage_{promo_id}')]
                    ]
                ),
            )

        elif edit_action == 'days':
            if value < 1 or value > 3650:
                await message.answer('❌ تعداد روزها باید بین 1 تا 3650 باشد')
                return

            await update_promocode(db, promo, subscription_days=value)
            await message.answer(
                f'✅ تعداد روزها به {value} تغییر یافت',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🎫 به کد تخفیف', callback_data=f'promo_manage_{promo_id}')]
                    ]
                ),
            )

        await state.clear()
        logger.info(
            'Promo code edited by admin',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            edit_action=edit_action,
            value=value,
        )

    except ValueError:
        await message.answer('❌ یک عدد معتبر وارد کنید')


@admin_required
@error_handler
async def process_promocode_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_uses(message, db_user, state, db)
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer('❌ تعداد استفاده باید بین 0 تا 100,000 باشد')
            return

        if max_uses == 0:
            max_uses = 999999

        await state.update_data(promocode_max_uses=max_uses)

        await message.answer('⏰ مدت اعتبار کد تخفیف را به روز وارد کنید (یا 0 برای نامحدود):')
        await state.set_state(AdminStates.setting_promocode_expiry)

    except ValueError:
        await message.answer('❌ یک عدد معتبر وارد کنید')


async def handle_edit_uses(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ کد تخفیف یافت نشد')
        await state.clear()
        return

    try:
        max_uses = int(message.text.strip())

        if max_uses < 0 or max_uses > 100000:
            await message.answer('❌ تعداد استفاده باید بین 0 تا 100,000 باشد')
            return

        if max_uses == 0:
            max_uses = 999999

        if max_uses < promo.current_uses:
            await message.answer(
                f'❌ محدودیت جدید ({max_uses}) نمی‌تواند کمتر از استفاده‌های فعلی ({promo.current_uses}) باشد'
            )
            return

        await update_promocode(db, promo, max_uses=max_uses)

        uses_text = 'نامحدود' if max_uses == 999999 else str(max_uses)
        await message.answer(
            f'✅ حداکثر تعداد استفاده به {uses_text} تغییر یافت',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🎫 به کد تخفیف', callback_data=f'promo_manage_{promo_id}')]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Promo code max_uses edited by admin',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            max_uses=max_uses,
        )

    except ValueError:
        await message.answer('❌ یک عدد معتبر وارد کنید')


@admin_required
@error_handler
async def process_promocode_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()

    if data.get('editing_promo_id'):
        await handle_edit_expiry(message, db_user, state, db)
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer('❌ مدت اعتبار باید بین 0 تا 3650 روز باشد')
            return

        code = data.get('promocode_code')
        promo_type = data.get('promocode_type')
        value = data.get('promocode_value', 0)
        max_uses = data.get('promocode_max_uses', 1)
        promo_group_id = data.get('promo_group_id')
        promo_group_name = data.get('promo_group_name')

        # For DISCOUNT type, additionally ask for discount validity in hours
        if promo_type == 'discount':
            await state.update_data(promocode_expiry_days=expiry_days)
            await message.answer(
                f'⏰ <b>کد تخفیف:</b> <code>{code}</code>\n\n'
                f'مدت اعتبار تخفیف را به ساعت وارد کنید (0-8760):\n'
                f'0 = تا اولین خرید نامحدود'
            )
            await state.set_state(AdminStates.setting_discount_hours)
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        type_map = {
            'balance': PromoCodeType.BALANCE,
            'days': PromoCodeType.SUBSCRIPTION_DAYS,
            'trial': PromoCodeType.TRIAL_SUBSCRIPTION,
            'group': PromoCodeType.PROMO_GROUP,
        }

        promocode = await create_promocode(
            db=db,
            code=code,
            type=type_map[promo_type],
            balance_bonus_kopeks=value * 100 if promo_type == 'balance' else 0,
            subscription_days=value if promo_type in ['days', 'trial'] else 0,
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=promo_group_id if promo_type == 'group' else None,
        )

        type_names = {
            'balance': 'شارژ موجودی',
            'days': 'روزهای اشتراک',
            'trial': 'اشتراک آزمایشی',
            'group': 'گروه تبلیغاتی',
        }

        summary_text = f"""
✅ <b>کد تخفیف ایجاد شد!</b>

🎫 <b>کد:</b> <code>{promocode.code}</code>
📝 <b>نوع:</b> {type_names.get(promo_type)}
"""

        if promo_type == 'balance':
            summary_text += f'💰 <b>مبلغ:</b> {settings.format_price(promocode.balance_bonus_kopeks)}\n'
        elif promo_type in ['days', 'trial']:
            summary_text += f'📅 <b>روزها:</b> {promocode.subscription_days}\n'
        elif promo_type == 'group' and promo_group_name:
            summary_text += f'🏷️ <b>گروه تبلیغاتی:</b> {promo_group_name}\n'

        summary_text += f'📊 <b>استفاده:</b> {promocode.max_uses}\n'

        if promocode.valid_until:
            summary_text += f'⏰ <b>معتبر تا:</b> {format_datetime(promocode.valid_until)}\n'

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🎫 به کدهای تخفیف', callback_data='admin_promocodes')]]
            ),
        )

        await state.clear()
        logger.info('Promo code created by admin', code=code, telegram_id=db_user.telegram_id)

    except ValueError:
        await message.answer('❌ یک عدد معتبر برای روز وارد کنید')


@admin_required
@error_handler
async def process_discount_hours(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Handler for discount validity input in hours for DISCOUNT promo code."""
    data = await state.get_data()

    try:
        discount_hours = int(message.text.strip())

        if discount_hours < 0 or discount_hours > 8760:
            await message.answer('❌ مدت اعتبار تخفیف باید بین 0 تا 8760 ساعت باشد')
            return

        code = data.get('promocode_code')
        value = data.get('promocode_value', 0)  # Discount percent
        max_uses = data.get('promocode_max_uses', 1)
        expiry_days = data.get('promocode_expiry_days', 0)

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        # Create DISCOUNT promo code
        # balance_bonus_kopeks = discount percent (NOT kopecks!)
        # subscription_days = discount validity in hours (NOT days!)
        promocode = await create_promocode(
            db=db,
            code=code,
            type=PromoCodeType.DISCOUNT,
            balance_bonus_kopeks=value,  # Percent (1-100)
            subscription_days=discount_hours,  # Hours (0-8760)
            max_uses=max_uses,
            valid_until=valid_until,
            created_by=db_user.id,
            promo_group_id=None,
        )

        summary_text = f"""
✅ <b>کد تخفیف ایجاد شد!</b>

🎫 <b>کد:</b> <code>{promocode.code}</code>
📝 <b>نوع:</b> تخفیف یک‌بار مصرف
💸 <b>تخفیف:</b> {promocode.balance_bonus_kopeks}%
"""

        if discount_hours > 0:
            summary_text += f'⏰ <b>مدت تخفیف:</b> {discount_hours} ساعت\n'
        else:
            summary_text += '⏰ <b>مدت تخفیف:</b> تا اولین خرید\n'

        summary_text += f'📊 <b>استفاده:</b> {promocode.max_uses}\n'

        if promocode.valid_until:
            summary_text += f'⏳ <b>کد تخفیف معتبر تا:</b> {format_datetime(promocode.valid_until)}\n'

        await message.answer(
            summary_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='🎫 به کدهای تخفیف', callback_data='admin_promocodes')]]
            ),
        )

        await state.clear()
        logger.info(
            'DISCOUNT promo code (%, h) created by admin',
            code=code,
            value=value,
            discount_hours=discount_hours,
            telegram_id=db_user.telegram_id,
        )

    except ValueError:
        await message.answer('❌ یک عدد معتبر برای ساعت وارد کنید')


async def handle_edit_expiry(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    promo_id = data.get('editing_promo_id')

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await message.answer('❌ کد تخفیف یافت نشد')
        await state.clear()
        return

    try:
        expiry_days = int(message.text.strip())

        if expiry_days < 0 or expiry_days > 3650:
            await message.answer('❌ مدت اعتبار باید بین 0 تا 3650 روز باشد')
            return

        valid_until = None
        if expiry_days > 0:
            valid_until = datetime.now(UTC) + timedelta(days=expiry_days)

        await update_promocode(db, promo, valid_until=valid_until)

        if valid_until:
            expiry_text = f'تا {format_datetime(valid_until)}'
        else:
            expiry_text = 'نامحدود'

        await message.answer(
            f'✅ مدت اعتبار کد تخفیف به {expiry_text} تغییر یافت',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🎫 به کد تخفیف', callback_data=f'promo_manage_{promo_id}')]
                ]
            ),
        )

        await state.clear()
        logger.info(
            'Promo code expiry edited by admin',
            code=promo.code,
            telegram_id=db_user.telegram_id,
            expiry_days=expiry_days,
        )

    except ValueError:
        await message.answer('❌ یک عدد معتبر برای روز وارد کنید')


@admin_required
@error_handler
async def toggle_promocode_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    new_status = not promo.is_active
    await update_promocode(db, promo, is_active=new_status)

    status_text = 'فعال شد' if new_status else 'غیرفعال شد'
    await callback.answer(f'✅ کد تخفیف {status_text}', show_alert=True)

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def toggle_promocode_first_purchase(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Toggles 'first purchase only' mode."""
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    new_status = not getattr(promo, 'first_purchase_only', False)
    await update_promocode(db, promo, first_purchase_only=new_status)

    status_text = 'فعال شد' if new_status else 'غیرفعال شد'
    await callback.answer(f"✅ حالت 'اولین خرید' {status_text}", show_alert=True)

    await show_promocode_management(callback, db_user, db)


@admin_required
@error_handler
async def confirm_delete_promocode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    text = f"""
⚠️ <b>تأیید حذف</b>

آیا واقعاً می‌خواهید کد تخفیف <code>{promo.code}</code> را حذف کنید؟

📊 <b>اطلاعات کد تخفیف:</b>
• استفاده: {promo.current_uses}/{promo.max_uses}
• وضعیت: {'فعال' if promo.is_active else 'غیرفعال'}

<b>⚠️ توجه:</b> این عمل قابل برگشت نیست!

ID: {promo_id}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='✅ بله، حذف کن', callback_data=f'promo_delete_confirm_{promo.id}'),
                types.InlineKeyboardButton(text='❌ لغو', callback_data=f'promo_manage_{promo.id}'),
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_promocode_confirmed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        promo_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ خطا در دریافت شناسه کد تخفیف', show_alert=True)
        return

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    code = promo.code
    success = await delete_promocode(db, promo)

    if success:
        await callback.answer(f'✅ کد تخفیف {code} حذف شد', show_alert=True)
        await show_promocodes_list(callback, db_user, db)
    else:
        await callback.answer('❌ خطا در حذف کد تخفیف', show_alert=True)


@admin_required
@error_handler
async def show_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    promo_id = int(callback.data.split('_')[-1])

    promo = await get_promocode_by_id(db, promo_id)
    if not promo:
        await callback.answer('❌ کد تخفیف یافت نشد', show_alert=True)
        return

    stats = await get_promocode_statistics(db, promo_id)

    text = f"""
📊 <b>آمار کد تخفیف</b> <code>{promo.code}</code>

📈 <b>آمار کلی:</b>
- مجموع استفاده: {stats['total_uses']}
- استفاده امروز: {stats['today_uses']}
- استفاده باقی‌مانده: {promo.max_uses - promo.current_uses}

📅 <b>آخرین استفاده‌ها:</b>
"""

    if stats['recent_uses']:
        for use in stats['recent_uses'][:5]:
            use_date = format_datetime(use.used_at)

            if hasattr(use, 'user_username') and use.user_username:
                user_display = f'@{html.escape(use.user_username)}'
            elif hasattr(use, 'user_full_name') and use.user_full_name:
                user_display = html.escape(use.user_full_name)
            elif hasattr(use, 'user_telegram_id'):
                user_display = f'ID{use.user_telegram_id}'
            else:
                user_display = f'ID{use.user_id}'

            text += f'- {use_date} | {user_display}\n'
    else:
        text += '- تا کنون استفاده‌ای نشده است\n'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'promo_manage_{promo.id}')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_general_promocode_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    total_codes = await get_promocodes_count(db)
    active_codes = await get_promocodes_count(db, is_active=True)

    text = f"""
📊 <b>آمار کلی کدهای تخفیف</b>

📈 <b>شاخص‌های اصلی:</b>
- مجموع کدهای تخفیف: {total_codes}
- فعال: {active_codes}
- غیرفعال: {total_codes - active_codes}

برای آمار دقیق، یک کد تخفیف خاص را از لیست انتخاب کنید.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🎫 به کدهای تخفیف', callback_data='admin_promo_list')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_promocodes')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_promocodes_menu, F.data == 'admin_promocodes')
    dp.callback_query.register(show_promocodes_list, F.data == 'admin_promo_list')
    dp.callback_query.register(show_promocodes_list_page, F.data.startswith('admin_promo_list_page_'))
    dp.callback_query.register(start_promocode_creation, F.data == 'admin_promo_create')
    dp.callback_query.register(select_promocode_type, F.data.startswith('promo_type_'))
    dp.callback_query.register(process_promo_group_selection, F.data.startswith('promo_select_group_'))

    dp.callback_query.register(show_promocode_management, F.data.startswith('promo_manage_'))
    dp.callback_query.register(toggle_promocode_first_purchase, F.data.startswith('promo_toggle_first_'))
    dp.callback_query.register(toggle_promocode_status, F.data.startswith('promo_toggle_'))
    dp.callback_query.register(show_promocode_stats, F.data.startswith('promo_stats_'))

    dp.callback_query.register(start_edit_promocode_date, F.data.startswith('promo_edit_date_'))
    dp.callback_query.register(start_edit_promocode_amount, F.data.startswith('promo_edit_amount_'))
    dp.callback_query.register(start_edit_promocode_days, F.data.startswith('promo_edit_days_'))
    dp.callback_query.register(start_edit_promocode_uses, F.data.startswith('promo_edit_uses_'))
    dp.callback_query.register(show_general_promocode_stats, F.data == 'admin_promo_general_stats')

    dp.callback_query.register(show_promocode_edit_menu, F.data.regexp(r'^promo_edit_\d+$'))

    dp.callback_query.register(delete_promocode_confirmed, F.data.startswith('promo_delete_confirm_'))
    dp.callback_query.register(confirm_delete_promocode, F.data.startswith('promo_delete_'))

    dp.message.register(process_promocode_code, AdminStates.creating_promocode)
    dp.message.register(process_promocode_value, AdminStates.setting_promocode_value)
    dp.message.register(process_promocode_uses, AdminStates.setting_promocode_uses)
    dp.message.register(process_promocode_expiry, AdminStates.setting_promocode_expiry)
    dp.message.register(process_discount_hours, AdminStates.setting_discount_hours)
