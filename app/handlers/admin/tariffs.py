"""Admin panel tariff management."""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import (
    create_tariff,
    delete_tariff,
    get_active_subscriptions_count_by_tariff_id,
    get_tariff_by_id,
    get_tariff_subscriptions_count,
    get_tariffs_with_subscriptions_count,
    update_tariff,
)
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatting import format_period, format_price_kopeks, format_traffic


logger = structlog.get_logger(__name__)

ITEMS_PER_PAGE = 10


def _parse_period_prices(text: str) -> dict[str, int]:
    """
    Parses a period price string.
    Format: "30:9900, 90:24900, 180:44900" or "30=9900; 90=24900"
    """
    prices = {}
    text = text.replace(';', ',').replace('=', ':')

    for part in text.split(','):
        part = part.strip()
        if not part:
            continue

        if ':' not in part:
            continue

        period_str, price_str = part.split(':', 1)
        try:
            period = int(period_str.strip())
            price = int(price_str.strip())
            if period > 0 and price >= 0:
                prices[str(period)] = price
        except ValueError:
            continue

    return prices


def _format_period_prices_display(prices: dict[str, int]) -> str:
    """Formats period prices for display."""
    if not prices:
        return 'تعیین نشده'

    lines = []
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]
        lines.append(f'  • {format_period(period)}: {format_price_kopeks(price)}')

    return '\n'.join(lines)


def _format_period_prices_for_edit(prices: dict[str, int]) -> str:
    """Formats period prices for editing."""
    if not prices:
        return '30:9900, 90:24900, 180:44900'

    parts = []
    for period_str in sorted(prices.keys(), key=int):
        parts.append(f'{period_str}:{prices[period_str]}')

    return ', '.join(parts)


def get_tariffs_list_keyboard(
    tariffs: list[tuple[Tariff, int]],
    language: str,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Creates the tariff list keyboard."""
    texts = get_texts(language)
    buttons = []

    for tariff, subs_count in tariffs:
        status = '✅' if tariff.is_active else '❌'
        button_text = f'{status} {tariff.name} ({subs_count})'
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'admin_tariff_view:{tariff.id}')])

    # Pagination
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text='◀️', callback_data=f'admin_tariffs_page:{page - 1}'))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text='▶️', callback_data=f'admin_tariffs_page:{page + 1}'))
    if nav_buttons:
        buttons.append(nav_buttons)

    # Create button
    buttons.append([InlineKeyboardButton(text='➕ ایجاد تعرفه', callback_data='admin_tariff_create')])

    # Back button
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_view_keyboard(
    tariff: Tariff,
    language: str,
) -> InlineKeyboardMarkup:
    """Creates the tariff view keyboard."""
    texts = get_texts(language)
    buttons = []

    # Field editing
    buttons.append(
        [
            InlineKeyboardButton(text='✏️ نام', callback_data=f'admin_tariff_edit_name:{tariff.id}'),
            InlineKeyboardButton(text='📝 توضیحات', callback_data=f'admin_tariff_edit_desc:{tariff.id}'),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text='📊 ترافیک', callback_data=f'admin_tariff_edit_traffic:{tariff.id}'),
            InlineKeyboardButton(text='📱 دستگاه‌ها', callback_data=f'admin_tariff_edit_devices:{tariff.id}'),
        ]
    )
    # Period prices only for regular tariffs (not daily)
    is_daily = getattr(tariff, 'is_daily', False)
    if not is_daily:
        buttons.append(
            [
                InlineKeyboardButton(text='💰 قیمت‌ها', callback_data=f'admin_tariff_edit_prices:{tariff.id}'),
                InlineKeyboardButton(text='🎚️ سطح', callback_data=f'admin_tariff_edit_tier:{tariff.id}'),
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(text='🎚️ سطح', callback_data=f'admin_tariff_edit_tier:{tariff.id}'),
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text='📱💰 قیمت دستگاه', callback_data=f'admin_tariff_edit_device_price:{tariff.id}'
            ),
            InlineKeyboardButton(
                text='📱🔒 حداکثر دستگاه', callback_data=f'admin_tariff_edit_max_devices:{tariff.id}'
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text='⏰ روزهای آزمایشی', callback_data=f'admin_tariff_edit_trial_days:{tariff.id}'),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text='📈 خرید ترافیک اضافی', callback_data=f'admin_tariff_edit_traffic_topup:{tariff.id}'
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text='🔄 ریست ترافیک', callback_data=f'admin_tariff_edit_reset_mode:{tariff.id}'),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text='🌐 سرورها', callback_data=f'admin_tariff_edit_squads:{tariff.id}'),
            InlineKeyboardButton(text='👥 گروه‌های تبلیغاتی', callback_data=f'admin_tariff_edit_promo:{tariff.id}'),
        ]
    )

    # Daily mode - settings shown only for already-daily tariffs
    # New tariffs become daily only at creation
    if is_daily:
        buttons.append(
            [
                InlineKeyboardButton(
                    text='💰 قیمت روزانه', callback_data=f'admin_tariff_edit_daily_price:{tariff.id}'
                ),
            ]
        )
        # Note: disabling daily mode was removed - it's an irreversible decision at creation

    # Trial toggle
    if tariff.is_trial_available:
        buttons.append(
            [InlineKeyboardButton(text='🎁 ❌ حذف آزمایشی', callback_data=f'admin_tariff_toggle_trial:{tariff.id}')]
        )
    else:
        buttons.append(
            [InlineKeyboardButton(text='🎁 تبدیل به آزمایشی', callback_data=f'admin_tariff_toggle_trial:{tariff.id}')]
        )

    # Activity toggle
    if tariff.is_active:
        buttons.append(
            [InlineKeyboardButton(text='❌ غیرفعال‌کردن', callback_data=f'admin_tariff_toggle:{tariff.id}')]
        )
    else:
        buttons.append([InlineKeyboardButton(text='✅ فعال‌کردن', callback_data=f'admin_tariff_toggle:{tariff.id}')])

    # Delete
    buttons.append([InlineKeyboardButton(text='🗑️ حذف', callback_data=f'admin_tariff_delete:{tariff.id}')])

    # Back to list
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_tariffs')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_traffic_reset_mode(mode: str | None) -> str:
    """Formats traffic reset mode for display."""
    mode_labels = {
        'DAY': '📅 روزانه',
        'WEEK': '📆 هفتگی',
        'MONTH': '🗓️ ماهانه',
        'MONTH_ROLLING': '🔄 ماه متحرک',
        'NO_RESET': '🚫 هرگز',
    }
    if mode is None:
        return f'🌐 تنظیم سراسری ({settings.DEFAULT_TRAFFIC_RESET_STRATEGY})'
    return mode_labels.get(mode, f'⚠️ ناشناخته ({mode})')


def _format_traffic_topup_packages(tariff: Tariff) -> str:
    """Formats traffic top-up packages for display."""
    if not getattr(tariff, 'traffic_topup_enabled', False):
        return '❌ غیرفعال'

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if not packages:
        return '✅ فعال، اما بسته‌ها پیکربندی نشده‌اند'

    lines = ['✅ فعال']
    for gb in sorted(packages.keys()):
        price = packages[gb]
        lines.append(f'  • {gb} GB: {format_price_kopeks(price)}')

    return '\n'.join(lines)


def format_tariff_info(tariff: Tariff, language: str, subs_count: int = 0) -> str:
    """Formats tariff information."""
    get_texts(language)

    status = '✅ فعال' if tariff.is_active else '❌ غیرفعال'
    traffic = format_traffic(tariff.traffic_limit_gb)
    prices_display = _format_period_prices_display(tariff.period_prices or {})

    # Format servers list
    squads_list = tariff.allowed_squads or []
    squads_display = f'{len(squads_list)} سرور' if squads_list else 'همه سرورها'

    # Format promo groups
    promo_groups = tariff.allowed_promo_groups or []
    if promo_groups:
        promo_display = ', '.join(pg.name for pg in promo_groups)
    else:
        promo_display = 'برای همه در دسترس'

    trial_status = '✅ بله' if tariff.is_trial_available else '❌ خیر'

    # Format trial days
    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        trial_days_display = f'{trial_days} روز'
    else:
        trial_days_display = f'پیش‌فرض ({settings.TRIAL_DURATION_DAYS} روز)'

    # Format device price
    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        device_price_display = format_price_kopeks(device_price) + '/ماه'
    else:
        device_price_display = 'در دسترس نیست'

    # Format max devices
    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        max_devices_display = str(max_devices)
    else:
        max_devices_display = '∞ (بدون محدودیت)'

    # Format traffic top-up
    traffic_topup_display = _format_traffic_topup_packages(tariff)

    # Format traffic reset mode
    traffic_reset_mode = getattr(tariff, 'traffic_reset_mode', None)
    traffic_reset_display = _format_traffic_reset_mode(traffic_reset_mode)

    # Format daily tariff
    is_daily = getattr(tariff, 'is_daily', False)
    daily_price_kopeks = getattr(tariff, 'daily_price_kopeks', 0)

    # Build price block based on tariff type
    if is_daily:
        price_block = f'<b>💰 قیمت روزانه:</b> {format_price_kopeks(daily_price_kopeks)}/روز'
        tariff_type = '🔄 روزانه'
    else:
        price_block = f'<b>قیمت‌ها:</b>\n{prices_display}'
        tariff_type = '📅 دوره‌ای'

    return f"""📦 <b>تعرفه: {html.escape(tariff.name)}</b>

{status} | {tariff_type}
🎚️ سطح: {tariff.tier_level}
📊 ترتیب: {tariff.display_order}

<b>پارامترها:</b>
• ترافیک: {traffic}
• دستگاه‌ها: {tariff.device_limit}
• حداکثر دستگاه‌ها: {max_devices_display}
• قیمت دستگاه اضافی: {device_price_display}
• آزمایشی: {trial_status}
• روزهای آزمایشی: {trial_days_display}

<b>خرید ترافیک اضافی:</b>
{traffic_topup_display}

<b>ریست ترافیک:</b> {traffic_reset_display}

{price_block}

<b>سرورها:</b> {squads_display}
<b>گروه‌های تبلیغاتی:</b> {promo_display}

📊 اشتراک‌های این تعرفه: {subs_count}

{f'📝 {html.escape(tariff.description)}' if tariff.description else ''}"""


@admin_required
@error_handler
async def show_tariffs_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Shows tariff list."""
    await state.clear()
    texts = get_texts(db_user.language)

    # Check sales mode
    if not settings.is_tariffs_mode():
        await callback.message.edit_text(
            '⚠️ <b>حالت تعرفه غیرفعال است</b>\n\n'
            'برای استفاده از تعرفه‌ها تنظیم کنید:\n'
            '<code>SALES_MODE=tariffs</code>\n\n'
            'حالت فعلی: <code>classic</code>',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            '📦 <b>تعرفه‌ها</b>\n\nهیچ تعرفه‌ای ایجاد نشده است.\nاولین تعرفه را برای شروع ایجاد کنید.',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='➕ ایجاد تعرفه', callback_data='admin_tariff_create')],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_data = tariffs_data[:ITEMS_PER_PAGE]

    total_subs = sum(count for _, count in tariffs_data)
    active_count = sum(1 for t, _ in tariffs_data if t.is_active)

    await callback.message.edit_text(
        f'📦 <b>تعرفه‌ها</b>\n\n'
        f'مجموع: {len(tariffs_data)} (فعال: {active_count})\n'
        f'اشتراک‌های تعرفه‌ها: {total_subs}\n\n'
        'تعرفه‌ای را برای مشاهده و ویرایش انتخاب کنید:',
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, 0, total_pages),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariffs_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Shows tariff list page."""
    get_texts(db_user.language)
    page = int(callback.data.split(':')[1])

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)
    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_data = tariffs_data[start_idx:end_idx]

    total_subs = sum(count for _, count in tariffs_data)
    active_count = sum(1 for t, _ in tariffs_data if t.is_active)

    await callback.message.edit_text(
        f'📦 <b>تعرفه‌ها</b> (ص. {page + 1}/{total_pages})\n\n'
        f'مجموع: {len(tariffs_data)} (فعال: {active_count})\n'
        f'اشتراک‌های تعرفه‌ها: {total_subs}\n\n'
        'تعرفه‌ای را برای مشاهده و ویرایش انتخاب کنید:',
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, page, total_pages),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def view_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """View tariff."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles tariff activity."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    tariff = await update_tariff(db, tariff, is_active=not tariff.is_active)
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    status = 'فعال شد' if tariff.is_active else 'غیرفعال شد'
    await callback.answer(f'تعرفه {status}', show_alert=True)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def toggle_trial_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles tariff trial mode."""
    from app.database.crud.tariff import clear_trial_tariff, set_trial_tariff

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    if tariff.is_trial_available:
        # Remove trial flag
        await clear_trial_tariff(db)
        await callback.answer('آزمایشی از تعرفه برداشته شد', show_alert=True)
    else:
        # Set this tariff as trial (removes flag from others)
        await set_trial_tariff(db, tariff_id)
        await callback.answer(f'تعرفه «{tariff.name}» به عنوان آزمایشی تنظیم شد', show_alert=True)

    # Reload tariff
    tariff = await get_tariff_by_id(db, tariff_id)
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def toggle_daily_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles daily tariff mode."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # Disable daily mode
        tariff = await update_tariff(db, tariff, is_daily=False, daily_price_kopeks=0)
        await callback.answer('حالت روزانه غیرفعال شد', show_alert=True)
    else:
        # Enable daily mode (with default price)
        tariff = await update_tariff(db, tariff, is_daily=True, daily_price_kopeks=5000)  # 50 rub default
        await callback.answer(
            'حالت روزانه فعال شد. قیمت: 50 ₽/روز\nقیمت را از طریق دکمه «💰 قیمت روزانه» تنظیم کنید', show_alert=True
        )

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_daily_price(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing daily price."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    current_price = getattr(tariff, 'daily_price_kopeks', 0)
    current_price / 100 if current_price else 0

    await state.set_state(AdminStates.editing_tariff_daily_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        f'💰 <b>ویرایش قیمت روزانه</b>\n\n'
        f'تعرفه: {html.escape(tariff.name)}\n'
        f'قیمت فعلی: {format_price_kopeks(current_price)}/روز\n\n'
        'قیمت جدید روزانه را وارد کنید.\n'
        'مثال: <code>50</code> یا <code>99.90</code>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_daily_price_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes daily price input (creation and editing)."""
    get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    # Parse price
    try:
        price_rubles = float(message.text.strip().replace(',', '.'))
        if price_rubles <= 0:
            raise ValueError('Price must be positive')

        price_kopeks = int(price_rubles * 100)
    except ValueError:
        await message.answer(
            '❌ قیمت نادرست است. یک عدد مثبت وارد کنید.\nمثال: <code>50</code> یا <code>99.90</code>',
            parse_mode='HTML',
        )
        return

    # Check if creating or editing
    is_creating = data.get('tariff_is_daily') and not tariff_id

    if is_creating:
        # Create new daily tariff
        tariff = await create_tariff(
            db,
            name=data['tariff_name'],
            traffic_limit_gb=data['tariff_traffic'],
            device_limit=data['tariff_devices'],
            tier_level=data['tariff_tier'],
            period_prices={},
            is_active=True,
            is_daily=True,
            daily_price_kopeks=price_kopeks,
        )
        await state.clear()

        await message.answer(
            '✅ <b>تعرفه روزانه ایجاد شد!</b>\n\n' + format_tariff_info(tariff, db_user.language, 0),
            reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
            parse_mode='HTML',
        )
    else:
        # Edit existing tariff
        if not tariff_id:
            await state.clear()
            return

        tariff = await get_tariff_by_id(db, tariff_id)
        if not tariff:
            await message.answer('تعرفه یافت نشد')
            await state.clear()
            return

        tariff = await update_tariff(db, tariff, daily_price_kopeks=price_kopeks)
        await state.clear()

        subs_count = await get_tariff_subscriptions_count(db, tariff_id)

        await message.answer(
            f'✅ قیمت روزانه تعیین شد: {format_price_kopeks(price_kopeks)}/روز\n\n'
            + format_tariff_info(tariff, db_user.language, subs_count),
            reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
            parse_mode='HTML',
        )


# ============ TARIFF CREATION ============


@admin_required
@error_handler
async def start_create_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts tariff creation."""
    texts = get_texts(db_user.language)

    await state.set_state(AdminStates.creating_tariff_name)
    await state.update_data(language=db_user.language)

    await callback.message.edit_text(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        'گام 1/6: نام تعرفه را وارد کنید\n\n'
        'مثال: <i>پایه</i>، <i>پریمیوم</i>، <i>بیزنس</i>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_tariff_name(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes tariff name."""
    texts = get_texts(db_user.language)
    name = message.text.strip()

    if len(name) < 2:
        await message.answer('نام باید حداقل 2 کاراکتر داشته باشد')
        return

    if len(name) > 50:
        await message.answer('نام نباید بیشتر از 50 کاراکتر داشته باشد')
        return

    await state.update_data(tariff_name=name)
    await state.set_state(AdminStates.creating_tariff_traffic)

    await message.answer(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        f'نام: <b>{name}</b>\n\n'
        'گام 2/6: محدودیت ترافیک را به GB وارد کنید\n\n'
        'برای ترافیک نامحدود <code>0</code> وارد کنید\n'
        'مثال: <i>100</i>، <i>500</i>، <i>0</i>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes traffic limit."""
    texts = get_texts(db_user.language)

    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد معتبر (0 یا بیشتر) وارد کنید')
        return

    data = await state.get_data()
    await state.update_data(tariff_traffic=traffic)
    await state.set_state(AdminStates.creating_tariff_devices)

    traffic_display = format_traffic(traffic)

    await message.answer(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        f'نام: <b>{data["tariff_name"]}</b>\n'
        f'ترافیک: <b>{traffic_display}</b>\n\n'
        'گام 3/6: محدودیت دستگاه‌ها را وارد کنید\n\n'
        'مثال: <i>1</i>، <i>3</i>، <i>5</i>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes device limit."""
    texts = get_texts(db_user.language)

    try:
        devices = int(message.text.strip())
        if devices < 1:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد معتبر (1 یا بیشتر) وارد کنید')
        return

    data = await state.get_data()
    await state.update_data(tariff_devices=devices)
    await state.set_state(AdminStates.creating_tariff_tier)

    traffic_display = format_traffic(data['tariff_traffic'])

    await message.answer(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        f'نام: <b>{data["tariff_name"]}</b>\n'
        f'ترافیک: <b>{traffic_display}</b>\n'
        f'دستگاه‌ها: <b>{devices}</b>\n\n'
        'گام 4/6: سطح تعرفه را وارد کنید (1-10)\n\n'
        'سطح برای نمایش بصری استفاده می‌شود\n'
        '1 - پایه، 10 - حداکثر\n'
        'مثال: <i>1</i>، <i>2</i>، <i>3</i>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_tier(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes tariff tier."""
    texts = get_texts(db_user.language)

    try:
        tier = int(message.text.strip())
        if tier < 1 or tier > 10:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد از 1 تا 10 وارد کنید')
        return

    data = await state.get_data()
    await state.update_data(tariff_tier=tier)

    traffic_display = format_traffic(data['tariff_traffic'])

    # Step 5/6: Choose tariff type
    await message.answer(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        f'نام: <b>{data["tariff_name"]}</b>\n'
        f'ترافیک: <b>{traffic_display}</b>\n'
        f'دستگاه‌ها: <b>{data["tariff_devices"]}</b>\n'
        f'سطح: <b>{tier}</b>\n\n'
        'گام 5/6: نوع تعرفه را انتخاب کنید',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='📅 دوره‌ای (ماه‌ها)', callback_data='tariff_type_periodic')],
                [InlineKeyboardButton(text='🔄 روزانه (پرداخت روزی)', callback_data='tariff_type_daily')],
                [InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')],
            ]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def select_tariff_type_periodic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Selects periodic tariff type."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    await state.update_data(tariff_is_daily=False)
    await state.set_state(AdminStates.creating_tariff_prices)

    traffic_display = format_traffic(data['tariff_traffic'])

    await callback.message.edit_text(
        '📦 <b>ایجاد تعرفه</b>\n\n'
        f'نام: <b>{data["tariff_name"]}</b>\n'
        f'ترافیک: <b>{traffic_display}</b>\n'
        f'دستگاه‌ها: <b>{data["tariff_devices"]}</b>\n'
        f'سطح: <b>{data["tariff_tier"]}</b>\n'
        f'نوع: <b>📅 دوره‌ای</b>\n\n'
        'گام 6/6: قیمت‌های دوره‌ها را وارد کنید\n\n'
        'فرمت: <code>روز:قیمت_به_کوپک</code>\n'
        'چند دوره را با کاما جدا کنید\n\n'
        'مثال:\n<code>30:9900, 90:24900, 180:44900, 360:79900</code>',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def select_tariff_type_daily(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Selects daily tariff type."""
    from app.states import AdminStates

    texts = get_texts(db_user.language)
    data = await state.get_data()

    await state.update_data(tariff_is_daily=True)
    await state.set_state(AdminStates.editing_tariff_daily_price)

    traffic_display = format_traffic(data['tariff_traffic'])

    await callback.message.edit_text(
        '📦 <b>ایجاد تعرفه روزانه</b>\n\n'
        f'نام: <b>{data["tariff_name"]}</b>\n'
        f'ترافیک: <b>{traffic_display}</b>\n'
        f'دستگاه‌ها: <b>{data["tariff_devices"]}</b>\n'
        f'سطح: <b>{data["tariff_tier"]}</b>\n'
        f'نوع: <b>🔄 روزانه</b>\n\n'
        'گام 6/6: قیمت روزانه را به روبل وارد کنید\n\n'
        'مثال: <i>50</i> (50 ₽/روز)، <i>99.90</i> (99.90 ₽/روز)',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_tariff_prices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes tariff prices."""
    get_texts(db_user.language)

    prices = _parse_period_prices(message.text.strip())

    if not prices:
        await message.answer(
            'خواندن قیمت‌ها ناموفق بود.\n\n'
            'فرمت: <code>روز:قیمت_به_کوپک</code>\n'
            'مثال: <code>30:9900, 90:24900</code>',
            parse_mode='HTML',
        )
        return

    data = await state.get_data()
    await state.update_data(tariff_prices=prices)

    format_traffic(data['tariff_traffic'])
    _format_period_prices_display(prices)

    # Create tariff
    tariff = await create_tariff(
        db,
        name=data['tariff_name'],
        traffic_limit_gb=data['tariff_traffic'],
        device_limit=data['tariff_devices'],
        tier_level=data['tariff_tier'],
        period_prices=prices,
        is_active=True,
    )

    await state.clear()

    subs_count = 0

    await message.answer(
        '✅ <b>تعرفه ایجاد شد!</b>\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ TARIFF EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_name(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing tariff name."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_name)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        f'✏️ <b>ویرایش نام</b>\n\nنام فعلی: <b>{html.escape(tariff.name)}</b>\n\nنام جدید را وارد کنید:',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_name(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new tariff name."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer('نام باید بین 2 تا 50 کاراکتر باشد')
        return

    tariff = await update_tariff(db, tariff, name=name)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ نام تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_description(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing tariff description."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_description)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_desc = tariff.description or 'تعیین نشده'

    await callback.message.edit_text(
        f'📝 <b>ویرایش توضیحات</b>\n\n'
        f'توضیحات فعلی:\n{current_desc}\n\n'
        'توضیحات جدید را وارد کنید (یا <code>-</code> برای حذف):',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_description(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new tariff description."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    description = message.text.strip()
    if description == '-':
        description = None

    tariff = await update_tariff(db, tariff, description=description)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ توضیحات تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing tariff traffic."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_traffic)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_traffic = format_traffic(tariff.traffic_limit_gb)

    await callback.message.edit_text(
        f'📊 <b>ویرایش ترافیک</b>\n\n'
        f'محدودیت فعلی: <b>{current_traffic}</b>\n\n'
        'محدودیت جدید را به GB وارد کنید (0 = نامحدود):',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new traffic limit."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد معتبر (0 یا بیشتر) وارد کنید')
        return

    tariff = await update_tariff(db, tariff, traffic_limit_gb=traffic)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ ترافیک تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing device limit."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        f'📱 <b>ویرایش دستگاه‌ها</b>\n\n'
        f'محدودیت فعلی: <b>{tariff.device_limit}</b>\n\n'
        'محدودیت جدید دستگاه‌ها را وارد کنید:',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new device limit."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
        if devices < 1:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد معتبر (1 یا بیشتر) وارد کنید')
        return

    tariff = await update_tariff(db, tariff, device_limit=devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ محدودیت دستگاه‌ها تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_tier(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing tariff tier."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_tier)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        f'🎚️ <b>ویرایش سطح</b>\n\n'
        f'سطح فعلی: <b>{tariff.tier_level}</b>\n\n'
        'سطح جدید را وارد کنید (1-10):',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_tier(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new tariff tier."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    try:
        tier = int(message.text.strip())
        if tier < 1 or tier > 10:
            raise ValueError
    except ValueError:
        await message.answer('یک عدد از 1 تا 10 وارد کنید')
        return

    tariff = await update_tariff(db, tariff, tier_level=tier)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ سطح تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_prices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing tariff prices."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_prices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_prices = _format_period_prices_for_edit(tariff.period_prices or {})
    prices_display = _format_period_prices_display(tariff.period_prices or {})

    await callback.message.edit_text(
        f'💰 <b>ویرایش قیمت‌ها</b>\n\n'
        f'قیمت‌های فعلی:\n{prices_display}\n\n'
        'قیمت‌های جدید را به فرمت زیر وارد کنید:\n'
        f'<code>{current_prices}</code>\n\n'
        '(روز:قیمت_به_کوپک، با کاما جدا شود)',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_prices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new tariff prices."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    prices = _parse_period_prices(message.text.strip())
    if not prices:
        await message.answer(
            'خواندن قیمت‌ها ناموفق بود.\nفرمت: <code>روز:قیمت</code>\nمثال: <code>30:9900, 90:24900</code>',
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, period_prices=prices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ قیمت‌ها تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ DEVICE PRICE EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_device_price(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing device price."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_device_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        current_price = format_price_kopeks(device_price) + '/ماه'
    else:
        current_price = 'در دسترس نیست (خرید دستگاه اضافی غیرفعال)'

    await callback.message.edit_text(
        f'📱💰 <b>ویرایش قیمت دستگاه</b>\n\n'
        f'قیمت فعلی: <b>{current_price}</b>\n\n'
        'قیمت به کوپک برای یک دستگاه در ماه را وارد کنید.\n\n'
        '• <code>0</code> یا <code>-</code> — خرید دستگاه اضافی غیرفعال\n'
        '• مثلاً: <code>5000</code> = 50₽/ماه برای دستگاه',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_device_price(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new device price."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        device_price = None
    else:
        try:
            device_price = int(text)
            if device_price < 0:
                raise ValueError
        except ValueError:
            await message.answer(
                'یک عدد معتبر (0 یا بیشتر) وارد کنید.\n'
                'برای غیرفعال‌کردن <code>0</code> یا <code>-</code> وارد کنید',
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, device_price_kopeks=device_price)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ قیمت دستگاه تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ MAX DEVICES EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_max_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing max devices."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_max_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        current_max = str(max_devices)
    else:
        current_max = '∞ (بدون محدودیت)'

    await callback.message.edit_text(
        f'📱🔒 <b>ویرایش حداکثر دستگاه‌ها</b>\n\n'
        f'مقدار فعلی: <b>{current_max}</b>\n'
        f'تعداد پایه دستگاه‌ها: <b>{tariff.device_limit}</b>\n\n'
        'حداکثر تعداد دستگاه‌هایی که کاربر می‌تواند خریداری کند را وارد کنید.\n\n'
        '• <code>0</code> یا <code>-</code> — بدون محدودیت\n'
        '• مثلاً: <code>5</code> = حداکثر 5 دستگاه در تعرفه',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_max_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new max devices count."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        max_devices = None
    else:
        try:
            max_devices = int(text)
            if max_devices < 1:
                raise ValueError
        except ValueError:
            await message.answer(
                'یک عدد معتبر (1 یا بیشتر) وارد کنید.\n'
                'برای برداشتن محدودیت <code>0</code> یا <code>-</code> وارد کنید',
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, max_device_limit=max_devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ حداکثر دستگاه‌ها تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ TRIAL DAYS EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_trial_days(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing trial days."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_trial_days)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        current_days = f'{trial_days} روز'
    else:
        current_days = f'پیش‌فرض ({settings.TRIAL_DURATION_DAYS} روز)'

    await callback.message.edit_text(
        f'⏰ <b>ویرایش روزهای آزمایشی</b>\n\n'
        f'مقدار فعلی: <b>{current_days}</b>\n\n'
        'تعداد روزهای آزمایشی را وارد کنید.\n\n'
        f'• <code>0</code> یا <code>-</code> — استفاده از تنظیم پیش‌فرض ({settings.TRIAL_DURATION_DAYS} روز)\n'
        '• مثلاً: <code>7</code> = 7 روز آزمایشی',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_trial_days(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new trial days count."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        trial_days = None
    else:
        try:
            trial_days = int(text)
            if trial_days < 1:
                raise ValueError
        except ValueError:
            await message.answer(
                'یک عدد معتبر از روزها (1 یا بیشتر) وارد کنید.\n'
                'برای استفاده از تنظیم پیش‌فرض <code>0</code> یا <code>-</code> وارد کنید',
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, trial_duration_days=trial_days)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        '✅ روزهای آزمایشی تغییر یافت!\n\n' + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ TRAFFIC TOP-UP EDITING ============


def _parse_traffic_topup_packages(text: str) -> dict[int, int]:
    """
    Parses a traffic top-up package string.
    Format: "5:5000, 10:9000, 20:15000" (GB:price_in_kopeks)
    """
    packages = {}
    text = text.replace(';', ',').replace('=', ':')

    for part in text.split(','):
        part = part.strip()
        if not part:
            continue

        if ':' not in part:
            continue

        gb_str, price_str = part.split(':', 1)
        try:
            gb = int(gb_str.strip())
            price = int(price_str.strip())
            if gb > 0 and price > 0:
                packages[gb] = price
        except ValueError:
            continue

    return packages


def _format_traffic_topup_packages_for_edit(packages: dict[int, int]) -> str:
    """Formats top-up packages for editing."""
    if not packages:
        return '5:5000, 10:9000, 20:15000'

    parts = []
    for gb in sorted(packages.keys()):
        parts.append(f'{gb}:{packages[gb]}')

    return ', '.join(parts)


@admin_required
@error_handler
async def start_edit_tariff_traffic_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Shows traffic top-up settings menu."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    # Check if tariff is unlimited
    if tariff.is_unlimited_traffic:
        await callback.answer('خرید ترافیک اضافی برای تعرفه نامحدود در دسترس نیست', show_alert=True)
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    # Format current settings
    if is_enabled:
        status = '✅ فعال'
        if packages:
            packages_display = '\n'.join(
                f'  • {gb} GB: {format_price_kopeks(price)}' for gb, price in sorted(packages.items())
            )
        else:
            packages_display = '  بسته‌ها پیکربندی نشده‌اند'
    else:
        status = '❌ غیرفعال'
        packages_display = '  -'

    # Format limit
    if max_topup_traffic > 0:
        max_limit_display = f'{max_topup_traffic} GB'
    else:
        max_limit_display = 'بدون محدودیت'

    buttons = []

    # Toggle on/off
    if is_enabled:
        buttons.append(
            [InlineKeyboardButton(text='❌ غیرفعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')]
        )
    else:
        buttons.append(
            [InlineKeyboardButton(text='✅ فعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')]
        )

    # Package and limit editing (only if enabled)
    if is_enabled:
        buttons.append(
            [
                InlineKeyboardButton(
                    text='📦 تنظیم بسته‌ها', callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}'
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text='📊 حداکثر محدودیت ترافیک', callback_data=f'admin_tariff_edit_max_topup:{tariff_id}'
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    await callback.message.edit_text(
        f'📈 <b>خرید ترافیک اضافی برای «{html.escape(tariff.name)}»</b>\n\n'
        f'وضعیت: {status}\n\n'
        f'<b>بسته‌ها:</b>\n{packages_display}\n\n'
        f'<b>حداکثر محدودیت:</b> {max_limit_display}\n\n'
        'کاربران می‌توانند ترافیک اضافی را با قیمت‌های تعیین‌شده خریداری کنند.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_traffic_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles traffic top-up on/off."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    new_value = not is_enabled

    tariff = await update_tariff(db, tariff, traffic_topup_enabled=new_value)

    status_text = 'فعال شد' if new_value else 'غیرفعال شد'
    await callback.answer(f'خرید ترافیک اضافی {status_text}')

    # Redraw menu
    texts = get_texts(db_user.language)
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    if new_value:
        status = '✅ فعال'
        if packages:
            packages_display = '\n'.join(
                f'  • {gb} GB: {format_price_kopeks(price)}' for gb, price in sorted(packages.items())
            )
        else:
            packages_display = '  بسته‌ها پیکربندی نشده‌اند'
    else:
        status = '❌ غیرفعال'
        packages_display = '  -'

    # Format limit
    if max_topup_traffic > 0:
        max_limit_display = f'{max_topup_traffic} GB'
    else:
        max_limit_display = 'بدون محدودیت'

    buttons = []

    if new_value:
        buttons.append(
            [InlineKeyboardButton(text='❌ غیرفعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text='📦 تنظیم بسته‌ها', callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}'
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text='📊 حداکثر محدودیت ترافیک', callback_data=f'admin_tariff_edit_max_topup:{tariff_id}'
                )
            ]
        )
    else:
        buttons.append(
            [InlineKeyboardButton(text='✅ فعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'📈 <b>خرید ترافیک اضافی برای «{html.escape(tariff.name)}»</b>\n\n'
            f'وضعیت: {status}\n\n'
            f'<b>بسته‌ها:</b>\n{packages_display}\n\n'
            f'<b>حداکثر محدودیت:</b> {max_limit_display}\n\n'
            'کاربران می‌توانند ترافیک اضافی را با قیمت‌های تعیین‌شده خریداری کنند.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


@admin_required
@error_handler
async def start_edit_traffic_topup_packages(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing traffic top-up packages."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_traffic_topup_packages)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    current_packages = _format_traffic_topup_packages_for_edit(packages)

    if packages:
        packages_display = '\n'.join(
            f'  • {gb} GB: {format_price_kopeks(price)}' for gb, price in sorted(packages.items())
        )
    else:
        packages_display = '  پیکربندی نشده‌اند'

    await callback.message.edit_text(
        f'📦 <b>تنظیم بسته‌های خرید ترافیک اضافی</b>\n\n'
        f'تعرفه: <b>{html.escape(tariff.name)}</b>\n\n'
        f'<b>بسته‌های فعلی:</b>\n{packages_display}\n\n'
        'بسته‌ها را به فرمت زیر وارد کنید:\n'
        f'<code>{current_packages}</code>\n\n'
        '(GB:قیمت_به_کوپک، با کاما جدا شود)\n'
        'مثلاً: <code>5:5000, 10:9000</code> = 5GB به 50₽، 10GB به 90₽',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_traffic_topup:{tariff_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_traffic_topup_packages(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new traffic top-up packages."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    if not message.text:
        await message.answer(
            'لطفاً یک پیام متنی ارسال کنید.\n\n'
            'فرمت: <code>GB:قیمت_به_کوپک</code>\n'
            'مثال: <code>5:5000, 10:9000, 20:15000</code>',
            parse_mode='HTML',
        )
        return

    packages = _parse_traffic_topup_packages(message.text.strip())

    if not packages:
        await message.answer(
            'خواندن بسته‌ها ناموفق بود.\n\n'
            'فرمت: <code>GB:قیمت_به_کوپک</code>\n'
            'مثال: <code>5:5000, 10:9000, 20:15000</code>',
            parse_mode='HTML',
        )
        return

    # Convert to JSON format (string keys)
    packages_json = {str(gb): price for gb, price in packages.items()}

    tariff = await update_tariff(db, tariff, traffic_topup_packages=packages_json)
    await state.clear()

    # Show updated menu
    texts = get_texts(db_user.language)
    packages_display = '\n'.join(f'  • {gb} GB: {format_price_kopeks(price)}' for gb, price in sorted(packages.items()))
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    max_limit_display = f'{max_topup_traffic} GB' if max_topup_traffic > 0 else 'بدون محدودیت'

    buttons = [
        [InlineKeyboardButton(text='❌ غیرفعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')],
        [
            InlineKeyboardButton(
                text='📦 تنظیم بسته‌ها', callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}'
            )
        ],
        [InlineKeyboardButton(text='📊 حداکثر محدودیت ترافیک', callback_data=f'admin_tariff_edit_max_topup:{tariff_id}')],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        f'✅ <b>بسته‌ها به‌روز شدند!</b>\n\n'
        f'📈 <b>خرید ترافیک اضافی برای «{html.escape(tariff.name)}»</b>\n\n'
        f'وضعیت: ✅ فعال\n\n'
        f'<b>بسته‌ها:</b>\n{packages_display}\n\n'
        f'<b>حداکثر محدودیت:</b> {max_limit_display}\n\n'
        'کاربران می‌توانند ترافیک اضافی را با قیمت‌های تعیین‌شده خریداری کنند.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )


# ============ MAX TRAFFIC TOP-UP LIMIT ============


@admin_required
@error_handler
async def start_edit_max_topup_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts editing max traffic top-up limit."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_max_topup_traffic)
    await state.update_data(tariff_id=tariff_id)

    current_limit = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    if current_limit > 0:
        current_display = f'{current_limit} GB'
    else:
        current_display = 'بدون محدودیت'

    await callback.message.edit_text(
        f'📊 <b>حداکثر محدودیت ترافیک</b>\n\n'
        f'تعرفه: <b>{html.escape(tariff.name)}</b>\n'
        f'محدودیت فعلی: <b>{current_display}</b>\n\n'
        f'حداکثر حجم کل ترافیک (به GB) را که می‌توان روی اشتراک داشت پس از تمام خریدها وارد کنید.\n\n'
        f'• مثلاً اگر تعرفه 100 GB می‌دهد و لیمیت 200 GB باشد — کاربر می‌تواند 100 GB دیگر بخرد\n'
        f'• برای برداشتن محدودیت <code>0</code> وارد کنید',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_traffic_topup:{tariff_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_max_topup_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes new max traffic top-up limit."""
    texts = get_texts(db_user.language)
    state_data = await state.get_data()
    tariff_id = state_data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer('تعرفه یافت نشد')
        await state.clear()
        return

    # Parse value
    text = message.text.strip()
    try:
        new_limit = int(text)
        if new_limit < 0:
            raise ValueError('Negative value')
    except ValueError:
        await message.answer(
            'یک عدد صحیح (0 یا بیشتر) وارد کنید.\n\n'
            '• <code>0</code> — بدون محدودیت\n'
            '• <code>200</code> — حداکثر 200 GB روی اشتراک',
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, max_topup_traffic_gb=new_limit)
    await state.clear()

    # Show updated menu
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if packages:
        packages_display = '\n'.join(
            f'  • {gb} GB: {format_price_kopeks(price)}' for gb, price in sorted(packages.items())
        )
    else:
        packages_display = '  بسته‌ها پیکربندی نشده‌اند'

    max_limit_display = f'{new_limit} GB' if new_limit > 0 else 'بدون محدودیت'

    buttons = [
        [InlineKeyboardButton(text='❌ غیرفعال‌کردن', callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}')],
        [
            InlineKeyboardButton(
                text='📦 تنظیم بسته‌ها', callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}'
            )
        ],
        [InlineKeyboardButton(text='📊 حداکثر محدودیت ترافیک', callback_data=f'admin_tariff_edit_max_topup:{tariff_id}')],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        f'✅ <b>محدودیت به‌روز شد!</b>\n\n'
        f'📈 <b>خرید ترافیک اضافی برای «{html.escape(tariff.name)}»</b>\n\n'
        f'وضعیت: ✅ فعال\n\n'
        f'<b>بسته‌ها:</b>\n{packages_display}\n\n'
        f'<b>حداکثر محدودیت:</b> {max_limit_display}\n\n'
        'کاربران می‌توانند ترافیک اضافی را با قیمت‌های تعیین‌شده خریداری کنند.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )


# ============ TARIFF DELETION ============


@admin_required
@error_handler
async def confirm_delete_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Asks for tariff deletion confirmation."""
    get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    active_count = await get_active_subscriptions_count_by_tariff_id(db, tariff_id)

    if active_count > 0:
        total_count = await get_tariff_subscriptions_count(db, tariff_id)
        await callback.message.edit_text(
            f'🗑️ <b>حذف تعرفه</b>\n\n'
            f'حذف تعرفه <b>{html.escape(tariff.name)}</b> ممکن نیست.\n\n'
            f'⚠️ <b>اشتراک‌های فعال:</b> {active_count} (مجموع: {total_count})\n'
            f'ابتدا تعرفه را غیرفعال کنید و منتظر پایان تمام اشتراک‌های فعال باشید، '
            f'یا اشتراک‌ها را به تعرفه دیگری منتقل کنید.',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='◀️ بازگشت به تعرفه', callback_data=f'admin_tariff_view:{tariff_id}')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    warning = ''
    if subs_count > 0:
        warning = (
            f'\n\n⚠️ <b>توجه!</b> این تعرفه {subs_count} اشتراک غیرفعال دارد.\nاتصال آن‌ها به تعرفه از دست می‌رود.'
        )

    await callback.message.edit_text(
        f'🗑️ <b>حذف تعرفه</b>\n\nآیا مطمئن هستید که می‌خواهید تعرفه <b>{html.escape(tariff.name)}</b> را حذف کنید؟{warning}',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ بله، حذف شود', callback_data=f'admin_tariff_delete_confirm:{tariff_id}'
                    ),
                    InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_tariff_view:{tariff_id}'),
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_tariff_confirmed(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Deletes tariff after confirmation."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    # Protection against deleting tariff with active subscriptions (FK RESTRICT)
    active_count = await get_active_subscriptions_count_by_tariff_id(db, tariff.id)
    if active_count > 0:
        await callback.answer(
            f'حذف تعرفه ممکن نیست: {active_count} اشتراک فعال. ابتدا تعرفه را غیرفعال کنید.',
            show_alert=True,
        )
        return

    tariff_name = tariff.name
    await delete_tariff(db, tariff)

    await callback.answer(f'تعرفه «{tariff_name}» حذف شد', show_alert=True)

    # Return to list
    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            '📦 <b>تعرفه‌ها</b>\n\nهیچ تعرفه‌ای ایجاد نشده است.',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='➕ ایجاد تعرفه', callback_data='admin_tariff_create')],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
                ]
            ),
            parse_mode='HTML',
        )
        return

    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_data = tariffs_data[:ITEMS_PER_PAGE]

    await callback.message.edit_text(
        f'📦 <b>تعرفه‌ها</b>\n\n✅ تعرفه «{tariff_name}» حذف شد\n\nمجموع: {len(tariffs_data)}',
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, 0, total_pages),
        parse_mode='HTML',
    )


# ============ SERVERS EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Shows server selection menu for tariff."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    squads, _ = await get_all_server_squads(db, limit=10000)

    if not squads:
        await callback.answer('سرور در دسترسی وجود ندارد', show_alert=True)
        return

    current_squads = set(tariff.allowed_squads or [])

    buttons = []
    for squad in squads:
        is_selected = squad.squad_uuid in current_squads
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_squads:{tariff_id}'),
            InlineKeyboardButton(text='✅ انتخاب همه', callback_data=f'admin_tariff_select_all_squads:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_squads)

    await callback.message.edit_text(
        f'🌐 <b>سرورهای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
        f'انتخاب‌شده: {selected_count} از {len(squads)}\n\n'
        'اگر هیچ سروری انتخاب نشود - همه در دسترس خواهند بود.\n'
        'روی سرور کلیک کنید تا انتخاب/لغو شود:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_squad(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles server selection for tariff."""
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    squad_uuid = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    current_squads = set(tariff.allowed_squads or [])

    if squad_uuid in current_squads:
        current_squads.remove(squad_uuid)
    else:
        current_squads.add(squad_uuid)

    tariff = await update_tariff(db, tariff, allowed_squads=list(current_squads))

    # Redraw menu
    squads, _ = await get_all_server_squads(db, limit=10000)
    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        is_selected = squad.squad_uuid in current_squads
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_squads:{tariff_id}'),
            InlineKeyboardButton(text='✅ انتخاب همه', callback_data=f'admin_tariff_select_all_squads:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'🌐 <b>سرورهای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
            f'انتخاب‌شده: {len(current_squads)} از {len(squads)}\n\n'
            'اگر هیچ سروری انتخاب نشود - همه در دسترس خواهند بود.\n'
            'روی سرور کلیک کنید تا انتخاب/لغو شود:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    await callback.answer()

    # Apply server changes to existing subscriptions
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, list(current_squads))
    if propagate_result.failed_ids:
        await callback.message.answer(
            f'⚠️ {len(propagate_result.failed_ids)} از {propagate_result.total} اشتراک با RemnaWave همگام‌سازی نشدند',
        )


@admin_required
@error_handler
async def clear_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Clears the tariff servers list."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    tariff = await update_tariff(db, tariff, allowed_squads=[])
    await callback.answer('همه سرورها پاک شدند')

    # Redraw menu
    squads, _ = await get_all_server_squads(db, limit=10000)
    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'⬜ {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_squads:{tariff_id}'),
            InlineKeyboardButton(text='✅ انتخاب همه', callback_data=f'admin_tariff_select_all_squads:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'🌐 <b>سرورهای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
            f'انتخاب‌شده: 0 از {len(squads)}\n\n'
            'اگر هیچ سروری انتخاب نشود - همه در دسترس خواهند بود.\n'
            'روی سرور کلیک کنید تا انتخاب/لغو شود:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    # Apply server changes to existing subscriptions (empty list = all servers)
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, [])
    if propagate_result.failed_ids:
        await callback.message.answer(
            f'⚠️ {len(propagate_result.failed_ids)} از {propagate_result.total} اشتراک با RemnaWave همگام‌سازی نشدند',
        )


@admin_required
@error_handler
async def select_all_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Selects all servers for tariff."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    squads, _ = await get_all_server_squads(db, limit=10000)
    all_uuids = [s.squad_uuid for s in squads if s.squad_uuid]

    tariff = await update_tariff(db, tariff, allowed_squads=all_uuids)
    await callback.answer('همه سرورها انتخاب شدند')

    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'✅ {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_squads:{tariff_id}'),
            InlineKeyboardButton(text='✅ انتخاب همه', callback_data=f'admin_tariff_select_all_squads:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'🌐 <b>سرورهای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
            f'انتخاب‌شده: {len(squads)} از {len(squads)}\n\n'
            'اگر هیچ سروری انتخاب نشود - همه در دسترس خواهند بود.\n'
            'روی سرور کلیک کنید تا انتخاب/لغو شود:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    # Apply server changes to existing subscriptions
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, all_uuids)
    if propagate_result.failed_ids:
        await callback.message.answer(
            f'⚠️ {len(propagate_result.failed_ids)} از {propagate_result.total} اشتراک با RemnaWave همگام‌سازی نشدند',
        )


# ============ PROMO GROUPS EDITING ============


@admin_required
@error_handler
async def start_edit_tariff_promo_groups(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Shows promo group selection menu for tariff."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)

    if not promo_groups_data:
        await callback.answer('گروه تبلیغاتی‌ای وجود ندارد', show_alert=True)
        return

    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    buttons = []
    for promo_group, _ in promo_groups_data:
        is_selected = promo_group.id in current_groups
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_promo:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_groups)

    await callback.message.edit_text(
        f'👥 <b>گروه‌های تبلیغاتی تعرفه «{html.escape(tariff.name)}»</b>\n\n'
        f'انتخاب‌شده: {selected_count}\n\n'
        'اگر هیچ گروهی انتخاب نشود - تعرفه برای همه در دسترس است.\n'
        'گروه‌هایی را که به این تعرفه دسترسی دارند انتخاب کنید:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_promo_group(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Toggles promo group selection for tariff."""
    from app.database.crud.tariff import add_promo_group_to_tariff, remove_promo_group_from_tariff

    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    promo_group_id = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    if promo_group_id in current_groups:
        await remove_promo_group_from_tariff(db, tariff, promo_group_id)
        current_groups.remove(promo_group_id)
    else:
        await add_promo_group_to_tariff(db, tariff, promo_group_id)
        current_groups.add(promo_group_id)

    # Reload tariff from DB
    tariff = await get_tariff_by_id(db, tariff_id)
    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    # Redraw menu
    promo_groups_data = await get_promo_groups_with_counts(db)
    texts = get_texts(db_user.language)

    buttons = []
    for promo_group, _ in promo_groups_data:
        is_selected = promo_group.id in current_groups
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_promo:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'👥 <b>گروه‌های تبلیغاتی تعرفه «{html.escape(tariff.name)}»</b>\n\n'
            f'انتخاب‌شده: {len(current_groups)}\n\n'
            'اگر هیچ گروهی انتخاب نشود - تعرفه برای همه در دسترس است.\n'
            'گروه‌هایی را که به این تعرفه دسترسی دارند انتخاب کنید:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@admin_required
@error_handler
async def clear_tariff_promo_groups(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Clears the tariff promo groups list."""
    from app.database.crud.tariff import set_tariff_promo_groups

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    await set_tariff_promo_groups(db, tariff, [])
    await callback.answer('همه گروه‌های تبلیغاتی پاک شدند')

    # Redraw menu
    promo_groups_data = await get_promo_groups_with_counts(db)
    texts = get_texts(db_user.language)

    buttons = []
    for promo_group, _ in promo_groups_data:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'⬜ {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text='🔄 پاک‌کردن همه', callback_data=f'admin_tariff_clear_promo:{tariff_id}'),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            f'👥 <b>گروه‌های تبلیغاتی تعرفه «{html.escape(tariff.name)}»</b>\n\n'
            f'انتخاب‌شده: 0\n\n'
            'اگر هیچ گروهی انتخاب نشود - تعرفه برای همه در دسترس است.\n'
            'گروه‌هایی را که به این تعرفه دسترسی دارند انتخاب کنید:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


# ==================== Traffic Reset Mode ====================

TRAFFIC_RESET_MODES = [
    ('DAY', '📅 روزانه', 'ترافیک هر روز ریست می‌شود'),
    ('WEEK', '📆 هفتگی', 'ترافیک هر هفته ریست می‌شود'),
    ('MONTH', '🗓️ ماهانه', 'ترافیک هر ماه ریست می‌شود'),
    ('MONTH_ROLLING', '🔄 ماه متحرک', 'ترافیک 30 روز پس از اولین اتصال ریست می‌شود'),
    ('NO_RESET', '🚫 هرگز', 'ترافیک به‌صورت خودکار ریست نمی‌شود'),
]


def get_traffic_reset_mode_keyboard(tariff_id: int, current_mode: str | None, language: str) -> InlineKeyboardMarkup:
    """Creates traffic reset mode selection keyboard."""
    texts = get_texts(language)
    buttons = []

    # Global settings button
    global_label = (
        f'{"✅ " if current_mode is None else ""}🌐 تنظیم سراسری ({settings.DEFAULT_TRAFFIC_RESET_STRATEGY})'
    )
    buttons.append(
        [InlineKeyboardButton(text=global_label, callback_data=f'admin_tariff_set_reset_mode:{tariff_id}:GLOBAL')]
    )

    # Buttons for each mode
    for mode_value, mode_label, mode_desc in TRAFFIC_RESET_MODES:
        is_selected = current_mode == mode_value
        label = f'{"✅ " if is_selected else ""}{mode_label}'
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f'admin_tariff_set_reset_mode:{tariff_id}:{mode_value}')]
        )

    # Back button
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def start_edit_traffic_reset_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Starts editing traffic reset mode."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    current_mode = getattr(tariff, 'traffic_reset_mode', None)

    await callback.message.edit_text(
        f'🔄 <b>حالت ریست ترافیک برای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
        f'حالت فعلی: {_format_traffic_reset_mode(current_mode)}\n\n'
        'انتخاب کنید چه زمانی ترافیک مصرف‌شده مشترکان این تعرفه ریست شود:\n\n'
        '• <b>تنظیم سراسری</b> — استفاده از مقدار پیکربندی ربات\n'
        '• <b>روزانه</b> — ریست هر روز\n'
        '• <b>هفتگی</b> — ریست هر هفته\n'
        '• <b>ماهانه</b> — ریست هر ماه\n'
        '• <b>هرگز</b> — ترافیک در کل دوره اشتراک تجمیع می‌شود',
        reply_markup=get_traffic_reset_mode_keyboard(tariff_id, current_mode, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_traffic_reset_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Sets traffic reset mode for tariff."""
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    new_mode = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('تعرفه یافت نشد', show_alert=True)
        return

    # Convert GLOBAL to None
    if new_mode == 'GLOBAL':
        new_mode = None

    # Update tariff
    tariff = await update_tariff(db, tariff, traffic_reset_mode=new_mode)

    mode_display = _format_traffic_reset_mode(new_mode)
    await callback.answer(f'حالت ریست تغییر یافت: {mode_display}', show_alert=True)

    # Update keyboard
    await callback.message.edit_text(
        f'🔄 <b>حالت ریست ترافیک برای تعرفه «{html.escape(tariff.name)}»</b>\n\n'
        f'حالت فعلی: {mode_display}\n\n'
        'انتخاب کنید چه زمانی ترافیک مصرف‌شده مشترکان این تعرفه ریست شود:\n\n'
        '• <b>تنظیم سراسری</b> — استفاده از مقدار پیکربندی ربات\n'
        '• <b>روزانه</b> — ریست هر روز\n'
        '• <b>هفتگی</b> — ریست هر هفته\n'
        '• <b>ماهانه</b> — ریست هر ماه\n'
        '• <b>هرگز</b> — ترافیک در کل دوره اشتراک تجمیع می‌شود',
        reply_markup=get_traffic_reset_mode_keyboard(tariff_id, new_mode, db_user.language),
        parse_mode='HTML',
    )


def register_handlers(dp: Dispatcher):
    """Registers handlers for tariff management."""
    # Tariff list
    dp.callback_query.register(show_tariffs_list, F.data == 'admin_tariffs')
    dp.callback_query.register(show_tariffs_page, F.data.startswith('admin_tariffs_page:'))

    # View and toggle
    dp.callback_query.register(view_tariff, F.data.startswith('admin_tariff_view:'))
    dp.callback_query.register(
        toggle_tariff,
        F.data.startswith('admin_tariff_toggle:')
        & ~F.data.startswith('admin_tariff_toggle_trial:')
        & ~F.data.startswith('trf_sq:')
        & ~F.data.startswith('admin_tariff_toggle_promo:')
        & ~F.data.startswith('admin_tariff_toggle_traffic_topup:')
        & ~F.data.startswith('admin_tariff_toggle_daily:'),
    )
    dp.callback_query.register(toggle_trial_tariff, F.data.startswith('admin_tariff_toggle_trial:'))

    # Tariff creation
    dp.callback_query.register(start_create_tariff, F.data == 'admin_tariff_create')
    dp.message.register(process_tariff_name, AdminStates.creating_tariff_name)
    dp.message.register(process_tariff_traffic, AdminStates.creating_tariff_traffic)
    dp.message.register(process_tariff_devices, AdminStates.creating_tariff_devices)
    dp.message.register(process_tariff_tier, AdminStates.creating_tariff_tier)
    dp.callback_query.register(select_tariff_type_periodic, F.data == 'tariff_type_periodic')
    dp.callback_query.register(select_tariff_type_daily, F.data == 'tariff_type_daily')
    dp.message.register(process_tariff_prices, AdminStates.creating_tariff_prices)

    # Name editing
    dp.callback_query.register(start_edit_tariff_name, F.data.startswith('admin_tariff_edit_name:'))
    dp.message.register(process_edit_tariff_name, AdminStates.editing_tariff_name)

    # Description editing
    dp.callback_query.register(start_edit_tariff_description, F.data.startswith('admin_tariff_edit_desc:'))
    dp.message.register(process_edit_tariff_description, AdminStates.editing_tariff_description)

    # Traffic editing (traffic_topup BEFORE traffic to avoid prefix conflict)
    dp.callback_query.register(start_edit_tariff_traffic_topup, F.data.startswith('admin_tariff_edit_traffic_topup:'))
    dp.callback_query.register(start_edit_tariff_traffic, F.data.startswith('admin_tariff_edit_traffic:'))
    dp.message.register(process_edit_tariff_traffic, AdminStates.editing_tariff_traffic)

    # Device limit editing
    dp.callback_query.register(start_edit_tariff_devices, F.data.startswith('admin_tariff_edit_devices:'))
    dp.message.register(process_edit_tariff_devices, AdminStates.editing_tariff_devices)

    # Tier editing
    dp.callback_query.register(start_edit_tariff_tier, F.data.startswith('admin_tariff_edit_tier:'))
    dp.message.register(process_edit_tariff_tier, AdminStates.editing_tariff_tier)

    # Price editing
    dp.callback_query.register(start_edit_tariff_prices, F.data.startswith('admin_tariff_edit_prices:'))
    dp.message.register(process_edit_tariff_prices, AdminStates.editing_tariff_prices)

    # Device price editing
    dp.callback_query.register(start_edit_tariff_device_price, F.data.startswith('admin_tariff_edit_device_price:'))
    dp.message.register(process_edit_tariff_device_price, AdminStates.editing_tariff_device_price)

    # Max devices editing
    dp.callback_query.register(start_edit_tariff_max_devices, F.data.startswith('admin_tariff_edit_max_devices:'))
    dp.message.register(process_edit_tariff_max_devices, AdminStates.editing_tariff_max_devices)

    # Trial days editing
    dp.callback_query.register(start_edit_tariff_trial_days, F.data.startswith('admin_tariff_edit_trial_days:'))
    dp.message.register(process_edit_tariff_trial_days, AdminStates.editing_tariff_trial_days)

    # Traffic top-up editing (start_edit_tariff_traffic_topup registered above with traffic)
    dp.callback_query.register(toggle_tariff_traffic_topup, F.data.startswith('admin_tariff_toggle_traffic_topup:'))
    dp.callback_query.register(
        start_edit_traffic_topup_packages, F.data.startswith('admin_tariff_edit_topup_packages:')
    )
    dp.message.register(process_edit_traffic_topup_packages, AdminStates.editing_tariff_traffic_topup_packages)

    # Max traffic top-up limit editing
    dp.callback_query.register(start_edit_max_topup_traffic, F.data.startswith('admin_tariff_edit_max_topup:'))
    dp.message.register(process_edit_max_topup_traffic, AdminStates.editing_tariff_max_topup_traffic)

    # Delete (delete_confirm BEFORE delete to avoid prefix conflict)
    dp.callback_query.register(delete_tariff_confirmed, F.data.startswith('admin_tariff_delete_confirm:'))
    dp.callback_query.register(confirm_delete_tariff, F.data.startswith('admin_tariff_delete:'))

    # Servers editing
    dp.callback_query.register(start_edit_tariff_squads, F.data.startswith('admin_tariff_edit_squads:'))
    dp.callback_query.register(toggle_tariff_squad, F.data.startswith('trf_sq:'))
    dp.callback_query.register(clear_tariff_squads, F.data.startswith('admin_tariff_clear_squads:'))
    dp.callback_query.register(select_all_tariff_squads, F.data.startswith('admin_tariff_select_all_squads:'))

    # Promo groups editing
    dp.callback_query.register(start_edit_tariff_promo_groups, F.data.startswith('admin_tariff_edit_promo:'))
    dp.callback_query.register(toggle_tariff_promo_group, F.data.startswith('admin_tariff_toggle_promo:'))
    dp.callback_query.register(clear_tariff_promo_groups, F.data.startswith('admin_tariff_clear_promo:'))

    # Daily mode
    dp.callback_query.register(toggle_daily_tariff, F.data.startswith('admin_tariff_toggle_daily:'))
    dp.callback_query.register(start_edit_daily_price, F.data.startswith('admin_tariff_edit_daily_price:'))
    dp.message.register(process_daily_price_input, AdminStates.editing_tariff_daily_price)

    # Traffic reset mode
    dp.callback_query.register(start_edit_traffic_reset_mode, F.data.startswith('admin_tariff_edit_reset_mode:'))
    dp.callback_query.register(set_traffic_reset_mode, F.data.startswith('admin_tariff_set_reset_mode:'))
