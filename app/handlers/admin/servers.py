import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import (
    delete_server_squad,
    get_all_server_squads,
    get_available_server_squads,
    get_server_connected_users,
    get_server_squad_by_id,
    get_server_statistics,
    sync_with_remnawave,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.models import User
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.cache import cache
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _build_server_edit_view(server):
    status_emoji = '✅ در دسترس' if server.is_available else '❌ غیرقابل دسترس'
    price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'رایگان'
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else 'انتخاب نشده'
    )

    trial_status = '✅ بله' if server.is_trial_eligible else '⚪️ خیر'

    text = f"""
🌐 <b>ویرایش سرور</b>

<b>اطلاعات:</b>
• ID: {server.id}
• UUID: <code>{server.squad_uuid}</code>
• نام: {html.escape(server.display_name)}
• نام اصلی: {html.escape(server.original_name) if server.original_name else 'تعیین نشده'}
• وضعیت: {status_emoji}

<b>تنظیمات:</b>
• قیمت: {price_text}
• کد کشور: {server.country_code or 'تعیین نشده'}
• محدودیت کاربران: {server.max_users or 'بدون محدودیت'}
• کاربران فعلی: {server.current_users}
• گروه‌های تبلیغاتی: {promo_groups_text}
• اعطای آزمایشی: {trial_status}

<b>توضیحات:</b>
{server.description or 'تعیین نشده'}

چه چیزی را تغییر دهید:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='✏️ نام', callback_data=f'admin_server_edit_name_{server.id}'),
            types.InlineKeyboardButton(text='💰 قیمت', callback_data=f'admin_server_edit_price_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='🌍 کشور', callback_data=f'admin_server_edit_country_{server.id}'),
            types.InlineKeyboardButton(text='👥 محدودیت', callback_data=f'admin_server_edit_limit_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='👥 کاربران', callback_data=f'admin_server_users_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='🎁 اعطا در آزمایشی' if not server.is_trial_eligible else '🚫 عدم اعطا در آزمایشی',
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(text='🎯 گروه‌های تبلیغاتی', callback_data=f'admin_server_edit_promo_{server.id}'),
            types.InlineKeyboardButton(text='📝 توضیحات', callback_data=f'admin_server_edit_desc_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='❌ غیرفعال کردن' if server.is_available else '✅ فعال کردن',
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(text='🗑️ حذف', callback_data=f'admin_server_delete_{server.id}'),
            types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers_list'),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids):
    keyboard = []
    for group in promo_groups:
        emoji = '✅' if group['id'] in selected_ids else '⚪'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {group["name"]}',
                    callback_data=f'admin_server_promo_toggle_{server_id}_{group["id"]}',
                )
            ]
        )

    keyboard.append(
        [types.InlineKeyboardButton(text='💾 ذخیره', callback_data=f'admin_server_promo_save_{server_id}')]
    )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'admin_server_edit_{server_id}')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)

    text = f"""
🌐 <b>مدیریت سرورها</b>

📊 <b>آمار:</b>
• مجموع سرورها: {stats['total_servers']}
• در دسترس: {stats['available_servers']}
• غیرقابل دسترس: {stats['unavailable_servers']}
• با اتصال‌ها: {stats['servers_with_connections']}

💰 <b>درآمد سرورها:</b>
• کل: {int(stats['total_revenue_rubles'])} ₽

عملیات را انتخاب کنید:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='📋 لیست سرورها', callback_data='admin_servers_list'),
            types.InlineKeyboardButton(text='🔄 همگام‌سازی', callback_data='admin_servers_sync'),
        ],
        [
            types.InlineKeyboardButton(text='📊 همگام‌سازی شمارنده‌ها', callback_data='admin_servers_sync_counts'),
            types.InlineKeyboardButton(text='📈 آمار دقیق', callback_data='admin_servers_stats'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = '🌐 <b>لیست سرورها</b>\n\n❌ سروری یافت نشد.'
    else:
        text = '🌐 <b>لیست سرورها</b>\n\n'
        text += f'📊 مجموع: {total_count} | صفحه: {page}/{total_pages}\n\n'

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'رایگان'

            text += f'{i}. {status_emoji} {html.escape(server.display_name)}\n'
            text += f'   💰 قیمت: {price_text}'

            if server.max_users:
                text += f' | 👥 {server.current_users}/{server.max_users}'

            text += f'\n   UUID: <code>{server.squad_uuid}</code>\n\n'

    keyboard = []

    for i, server in enumerate(servers):
        row_num = i // 2
        if len(keyboard) <= row_num:
            keyboard.append([])

        status_emoji = '✅' if server.is_available else '❌'
        keyboard[row_num].append(
            types.InlineKeyboardButton(
                text=f'{status_emoji} {server.display_name[:15]}...', callback_data=f'admin_server_edit_{server.id}'
            )
        )

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_servers_list_page_{page - 1}'))

        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text='➡️', callback_data=f'admin_servers_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend([[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')]])

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🔄 همگام‌سازی با Remnawave...\n\nلطفاً صبر کنید، این ممکن است کمی طول بکشد.', reply_markup=None
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                '❌ دریافت داده‌های سرورها از Remnawave ناموفق بود.\n\nتنظیمات API را بررسی کنید.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')]]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = f"""
✅ <b>همگام‌سازی انجام شد</b>

📊 <b>نتایج:</b>
• سرورهای جدید ایجادشده: {created}
• موارد موجود به‌روزشده: {updated}
• موارد حذف‌شده: {removed}
• مجموع پردازش‌شده: {len(squads)}

ℹ️ سرورهای جدید به عنوان غیرقابل دسترس ایجاد شدند.
آن‌ها را در لیست سرورها پیکربندی کنید.
"""

        keyboard = [
            [
                types.InlineKeyboardButton(text='📋 لیست سرورها', callback_data='admin_servers_list'),
                types.InlineKeyboardButton(text='🔄 تکرار', callback_data='admin_servers_sync'),
            ],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Server sync error', error=e)
        await callback.message.edit_text(
            f'❌ خطا در همگام‌سازی: {e!s}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_server_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    payload = callback.data.split('admin_server_users_', 1)[-1]
    payload_parts = payload.split('_')

    server_id = int(payload_parts[0])
    page = int(payload_parts[1]) if len(payload_parts) > 1 else 1
    page = max(page, 1)
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    users = await get_server_connected_users(db, server_id)
    total_users = len(users)

    page_size = 10
    total_pages = max((total_users + page_size - 1) // page_size, 1)

    page = min(page, total_pages)

    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    page_users = users[start_index:end_index]

    safe_name = html.escape(server.display_name or '—')
    safe_uuid = html.escape(server.squad_uuid or '—')

    header = [
        '🌐 <b>کاربران سرور</b>',
        '',
        f'• سرور: {safe_name}',
        f'• UUID: <code>{safe_uuid}</code>',
        f'• اتصال‌ها: {total_users}',
    ]

    if total_pages > 1:
        header.append(f'• صفحه: {page}/{total_pages}')

    header.append('')

    text = '\n'.join(header)

    def _get_status_icon(status_text: str) -> str:
        if not status_text:
            return ''

        parts = status_text.split(' ', 1)
        return parts[0] if parts else status_text

    if users:
        lines = []
        for index, user in enumerate(page_users, start=start_index + 1):
            safe_user_name = html.escape(user.full_name)
            if user.telegram_id:
                user_link = f'<a href="tg://user?id={user.telegram_id}">{safe_user_name}</a>'
            else:
                user_link = f'<b>{safe_user_name}</b>'
            lines.append(f'{index}. {user_link}')

        text += '\n' + '\n'.join(lines)
    else:
        text += 'کاربری یافت نشد.'

    keyboard: list[list[types.InlineKeyboardButton]] = []

    for user in page_users:
        display_name = user.full_name
        if len(display_name) > 30:
            display_name = display_name[:27] + '...'

        if settings.is_multi_tariff_enabled() and hasattr(user, 'subscriptions') and user.subscriptions:
            status_parts = []
            for sub in user.subscriptions:
                emoji = '🟢' if sub.is_active else '🔴'
                name = sub.tariff.name if sub.tariff else f'#{sub.id}'
                status_parts.append(f'{emoji}{name}')
            subscription_status = ', '.join(status_parts)
        elif user.subscription:
            subscription_status = user.subscription.status_display
        else:
            subscription_status = '❌ بدون اشتراک'
        status_icon = _get_status_icon(subscription_status)

        if status_icon:
            button_text = f'{status_icon} {display_name}'
        else:
            button_text = display_name

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )

    if total_pages > 1:
        navigation_buttons: list[types.InlineKeyboardButton] = []

        if page > 1:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text='⬅️ قبلی',
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=f'صفحه {page}/{total_pages}',
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text='بعدی ➡️',
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append([types.InlineKeyboardButton(text='⬅️ به سرور', callback_data=f'admin_server_edit_{server_id}')])

    keyboard.append([types.InlineKeyboardButton(text='⬅️ به لیست', callback_data='admin_servers_list')])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )

    await callback.answer()


@admin_required
@error_handler
async def toggle_server_availability(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = 'فعال شد' if new_status else 'غیرفعال شد'
    await callback.answer(f'✅ سرور {status_text}!')

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = 'اعطا خواهد شد' if new_status else 'اعطا نخواهد شد'
    await callback.answer(f'✅ سرور {status_text} در آزمایشی')

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'رایگان'

    await callback.message.edit_text(
        f'💰 <b>ویرایش قیمت</b>\n\n'
        f'قیمت فعلی: <b>{current_price}</b>\n\n'
        f'قیمت جدید را ارسال کنید (مثلاً: 15.50) یا 0 برای دسترسی رایگان:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_price_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        price_rubles = float(message.text.replace(',', '.'))

        if price_rubles < 0:
            await message.answer('❌ قیمت نمی‌تواند منفی باشد')
            return

        if price_rubles > 10000:
            await message.answer('❌ قیمت خیلی زیاد است (حداکثر 10,000 ₽)')
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = f'{int(price_rubles)} ₽' if price_kopeks > 0 else 'رایگان'
            await message.answer(
                f'✅ قیمت سرور به <b>{price_text}</b> تغییر یافت',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}'
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer('❌ خطا در به‌روزرسانی سرور')

    except ValueError:
        await message.answer('❌ فرمت قیمت نادرست است. از اعداد استفاده کنید (مثلاً: 15.50)')


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        f'✏️ <b>ویرایش نام</b>\n\n'
        f'نام فعلی: <b>{html.escape(server.display_name)}</b>\n\n'
        f'نام جدید سرور را ارسال کنید:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_name_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_name = message.text.strip()

    if len(new_name) > 255:
        await message.answer('❌ نام خیلی بلند است (حداکثر 255 کاراکتر)')
        return

    if len(new_name) < 3:
        await message.answer('❌ نام خیلی کوتاه است (حداقل 3 کاراکتر)')
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            f'✅ نام سرور به <b>{new_name}</b> تغییر یافت',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ خطا در به‌روزرسانی سرور')


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    text = f"""
🗑️ <b>حذف سرور</b>

آیا واقعاً می‌خواهید این سرور را حذف کنید:
<b>{html.escape(server.display_name)}</b>

⚠️ <b>توجه!</b>
سرور فقط در صورتی حذف می‌شود که اتصال فعالی به آن وجود نداشته باشد.

این عمل قابل برگشت نیست!
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='🗑️ بله، حذف کن', callback_data=f'admin_server_delete_confirm_{server_id}'),
            types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}'),
        ]
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_server_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            f'✅ سرور <b>{html.escape(server.display_name)}</b> با موفقیت حذف شد!',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📋 به لیست سرورها', callback_data='admin_servers_list')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            f'❌ حذف سرور <b>{html.escape(server.display_name)}</b> ناموفق بود.\n\nشاید اتصال فعالی به آن وجود دارد.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_detailed_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)
    available_servers = await get_available_server_squads(db)

    text = f"""
📊 <b>آمار دقیق سرورها</b>

<b>🌐 اطلاعات کلی:</b>
• مجموع سرورها: {stats['total_servers']}
• در دسترس: {stats['available_servers']}
• غیرقابل دسترس: {stats['unavailable_servers']}
• با اتصال‌های فعال: {stats['servers_with_connections']}

<b>💰 آمار مالی:</b>
• درآمد کل: {int(stats['total_revenue_rubles'])} ₽
• میانگین قیمت هر سرور: {int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1))} ₽

<b>🔥 برترین سرورها بر اساس قیمت:</b>
"""

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'رایگان'
        text += f'{i}. {html.escape(server.display_name)} - {price_text}\n'

    if not sorted_servers:
        text += 'سروری در دسترس نیست\n'

    keyboard = [
        [
            types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_servers_stats'),
            types.InlineKeyboardButton(text='📋 لیست', callback_data='admin_servers_list'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or 'تعیین نشده'

    await callback.message.edit_text(
        f'🌍 <b>ویرایش کد کشور</b>\n\n'
        f'کد کشور فعلی: <b>{current_country}</b>\n\n'
        f"کد کشور جدید را ارسال کنید (مثلاً: IR, US, DE) یا '-' برای حذف:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_country_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_country = message.text.strip().upper()

    if new_country == '-':
        new_country = None
    elif len(new_country) > 5:
        await message.answer('❌ کد کشور خیلی بلند است (حداکثر 5 کاراکتر)')
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or 'حذف شد'
        await message.answer(
            f'✅ کد کشور به <b>{country_text}</b> تغییر یافت',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ خطا در به‌روزرسانی سرور')


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or 'بدون محدودیت'

    await callback.message.edit_text(
        f'👥 <b>ویرایش محدودیت کاربران</b>\n\n'
        f'محدودیت فعلی: <b>{current_limit}</b>\n\n'
        f'محدودیت جدید کاربران را ارسال کنید (عدد) یا 0 برای دسترسی نامحدود:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_limit_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        limit = int(message.text.strip())

        if limit < 0:
            await message.answer('❌ محدودیت نمی‌تواند منفی باشد')
            return

        if limit > 10000:
            await message.answer('❌ محدودیت خیلی زیاد است (حداکثر 10,000)')
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = f'{limit} کاربر' if limit > 0 else 'بدون محدودیت'
            await message.answer(
                f'✅ محدودیت کاربران به <b>{limit_text}</b> تغییر یافت',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}'
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer('❌ خطا در به‌روزرسانی سرور')

    except ValueError:
        await message.answer('❌ فرمت عدد نادرست است. یک عدد صحیح وارد کنید.')


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or 'تعیین نشده'

    await callback.message.edit_text(
        f'📝 <b>ویرایش توضیحات</b>\n\n'
        f'توضیحات فعلی:\n<i>{current_desc}</i>\n\n'
        f"توضیحات جدید سرور را ارسال کنید یا '-' برای حذف:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_server_edit_{server_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_description_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_description = message.text.strip()

    if new_description == '-':
        new_description = None
    elif len(new_description) > 1000:
        await message.answer('❌ توضیحات خیلی طولانی است (حداکثر 1000 کاراکتر)')
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or 'حذف شد'
        await cache.delete_pattern('available_countries*')
        await message.answer(
            f'✅ توضیحات سرور تغییر یافت:\n\n<i>{desc_text}</i>',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 به سرور', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ خطا در به‌روزرسانی سرور')


@admin_required
@error_handler
async def start_server_edit_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ سرور یافت نشد!', show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer('❌ گروه تبلیغاتی یافت نشد', show_alert=True)
        return

    selected_ids = {pg.id for pg in server.allowed_promo_groups}
    if not selected_ids:
        default_group = next((pg for pg in promo_groups if pg['is_default']), None)
        if default_group:
            selected_ids.add(default_group['id'])

    await state.set_state(AdminStates.editing_server_promo_groups)
    await state.set_data(
        {
            'server_id': server_id,
            'promo_groups': promo_groups,
            'selected_promo_groups': list(selected_ids),
            'server_name': server.display_name,
        }
    )

    text = (
        '🎯 <b>پیکربندی گروه‌های تبلیغاتی</b>\n\n'
        f'سرور: <b>{html.escape(server.display_name)}</b>\n\n'
        'گروه‌های تبلیغاتی که به این سرور دسترسی خواهند داشت را انتخاب کنید.\n'
        'حداقل یک گروه تبلیغاتی باید انتخاب شود.'
    )

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected_ids),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_server_promo_group(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    parts = callback.data.split('_')
    server_id = int(parts[4])
    group_id = int(parts[5])

    data = await state.get_data()
    if not data or data.get('server_id') != server_id:
        await callback.answer('⚠️ نشست ویرایش منقضی شده است', show_alert=True)
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer('⚠️ نمی‌توان آخرین گروه تبلیغاتی را غیرفعال کرد', show_alert=True)
            return
        selected.remove(group_id)
        message = 'گروه تبلیغاتی غیرفعال شد'
    else:
        selected.add(group_id)
        message = 'گروه تبلیغاتی اضافه شد'

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected)
    )
    await callback.answer(message)


@admin_required
@error_handler
async def save_server_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    data = await state.get_data()
    if not data:
        await callback.answer('⚠️ داده‌ای برای ذخیره وجود ندارد', show_alert=True)
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer('❌ حداقل یک گروه تبلیغاتی انتخاب کنید', show_alert=True)
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer('❌ سرور یافت نشد', show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer('✅ گروه‌های تبلیغاتی به‌روزرسانی شدند!')


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text('🔄 همگام‌سازی شمارنده‌های کاربران...', reply_markup=None)

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = f"""
✅ <b>همگام‌سازی انجام شد</b>

📊 <b>نتیجه:</b>
• سرورهای به‌روزشده: {updated_count}

شمارنده‌های کاربران با داده‌های واقعی همگام‌سازی شدند.
"""

        keyboard = [
            [
                types.InlineKeyboardButton(text='📋 لیست سرورها', callback_data='admin_servers_list'),
                types.InlineKeyboardButton(text='🔄 تکرار', callback_data='admin_servers_sync_counts'),
            ],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Counter sync error', error=e)
        await callback.message.edit_text(
            f'❌ خطا در همگام‌سازی: {e!s}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_servers')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def handle_servers_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_servers_list(callback, db_user, db, page)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_servers_menu, F.data == 'admin_servers')
    dp.callback_query.register(show_servers_list, F.data == 'admin_servers_list')
    dp.callback_query.register(sync_servers_with_remnawave, F.data == 'admin_servers_sync')
    dp.callback_query.register(sync_server_user_counts_handler, F.data == 'admin_servers_sync_counts')
    dp.callback_query.register(show_server_detailed_stats, F.data == 'admin_servers_stats')

    dp.callback_query.register(
        show_server_edit_menu,
        F.data.startswith('admin_server_edit_')
        & ~F.data.contains('name')
        & ~F.data.contains('price')
        & ~F.data.contains('country')
        & ~F.data.contains('limit')
        & ~F.data.contains('desc')
        & ~F.data.contains('promo'),
    )
    dp.callback_query.register(toggle_server_availability, F.data.startswith('admin_server_toggle_'))
    dp.callback_query.register(toggle_server_trial_assignment, F.data.startswith('admin_server_trial_'))
    dp.callback_query.register(show_server_users, F.data.startswith('admin_server_users_'))

    dp.callback_query.register(start_server_edit_name, F.data.startswith('admin_server_edit_name_'))
    dp.callback_query.register(start_server_edit_price, F.data.startswith('admin_server_edit_price_'))
    dp.callback_query.register(start_server_edit_country, F.data.startswith('admin_server_edit_country_'))
    dp.callback_query.register(start_server_edit_promo_groups, F.data.startswith('admin_server_edit_promo_'))
    dp.callback_query.register(start_server_edit_limit, F.data.startswith('admin_server_edit_limit_'))
    dp.callback_query.register(start_server_edit_description, F.data.startswith('admin_server_edit_desc_'))

    dp.message.register(process_server_name_edit, AdminStates.editing_server_name)
    dp.message.register(process_server_price_edit, AdminStates.editing_server_price)
    dp.message.register(process_server_country_edit, AdminStates.editing_server_country)
    dp.message.register(process_server_limit_edit, AdminStates.editing_server_limit)
    dp.message.register(process_server_description_edit, AdminStates.editing_server_description)
    dp.callback_query.register(toggle_server_promo_group, F.data.startswith('admin_server_promo_toggle_'))
    dp.callback_query.register(save_server_promo_groups, F.data.startswith('admin_server_promo_save_'))

    dp.callback_query.register(
        delete_server_confirm, F.data.startswith('admin_server_delete_') & ~F.data.contains('confirm')
    )
    dp.callback_query.register(delete_server_execute, F.data.startswith('admin_server_delete_confirm_'))

    dp.callback_query.register(handle_servers_pagination, F.data.startswith('admin_servers_list_page_'))
