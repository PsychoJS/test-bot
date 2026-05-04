import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    get_all_subscriptions,
    get_expired_subscriptions,
    get_expiring_subscriptions,
    get_subscriptions_statistics,
)
from app.database.models import User
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


def get_country_flag(country_name: str) -> str:
    flags = {
        'USA': '🇺🇸',
        'United States': '🇺🇸',
        'US': '🇺🇸',
        'Germany': '🇩🇪',
        'DE': '🇩🇪',
        'Deutschland': '🇩🇪',
        'Netherlands': '🇳🇱',
        'NL': '🇳🇱',
        'Holland': '🇳🇱',
        'United Kingdom': '🇬🇧',
        'UK': '🇬🇧',
        'GB': '🇬🇧',
        'Japan': '🇯🇵',
        'JP': '🇯🇵',
        'France': '🇫🇷',
        'FR': '🇫🇷',
        'Canada': '🇨🇦',
        'CA': '🇨🇦',
        'Russia': '🇷🇺',
        'RU': '🇷🇺',
        'Singapore': '🇸🇬',
        'SG': '🇸🇬',
    }
    return flags.get(country_name, '🌍')


async def get_users_by_countries(db: AsyncSession) -> dict:
    try:
        result = await db.execute(
            select(User.preferred_location, func.count(User.id))
            .where(User.preferred_location.isnot(None))
            .group_by(User.preferred_location)
        )

        stats = {}
        for location, count in result.fetchall():
            if location:
                stats[location] = count

        return stats
    except Exception as e:
        logger.error('Error fetching country statistics', error=e)
        return {}


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_subscriptions_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_subscriptions_statistics(db)

    text = f"""
📱 <b>مدیریت اشتراک‌ها</b>

📊 <b>آمار:</b>
- مجموع: {stats['total_subscriptions']}
- فعال: {stats['active_subscriptions']}
- پولی: {stats['paid_subscriptions']}
- آزمایشی: {stats['trial_subscriptions']}

📈 <b>فروش:</b>
- امروز: {stats['purchased_today']}
- این هفته: {stats['purchased_week']}
- این ماه: {stats['purchased_month']}

عملیات را انتخاب کنید:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='📋 لیست اشتراک‌ها', callback_data='admin_subs_list'),
            types.InlineKeyboardButton(text='⏰ در حال انقضا', callback_data='admin_subs_expiring'),
        ],
        [
            types.InlineKeyboardButton(text='📊 آمار', callback_data='admin_subs_stats'),
            types.InlineKeyboardButton(text='🌍 جغرافیا', callback_data='admin_subs_countries'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_subscriptions_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    subscriptions, total_count = await get_all_subscriptions(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not subscriptions:
        text = '📱 <b>لیست اشتراک‌ها</b>\n\n❌ اشتراکی یافت نشد.'
    else:
        text = '📱 <b>لیست اشتراک‌ها</b>\n\n'
        text += f'📊 مجموع: {total_count} | صفحه: {page}/{total_pages}\n\n'

        for i, sub in enumerate(subscriptions, 1 + (page - 1) * 10):
            user_info = (
                (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
                if sub.user
                else 'ناشناس'
            )
            sub_type = '🎁' if sub.is_trial else '💎'
            status = '✅ فعال' if sub.is_active else '❌ غیرفعال'

            text += f'{i}. {sub_type} {user_info}\n'
            text += f'   {status} | تا: {format_datetime(sub.end_date)}\n'
            if sub.device_limit > 0:
                text += f'   📱 دستگاه: {sub.device_limit}\n'
            text += '\n'

    keyboard = []

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_subs_list_page_{page - 1}'))

        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text='➡️', callback_data=f'admin_subs_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_subs_list')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_subscriptions')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_expiring_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    expiring_3d = await get_expiring_subscriptions(db, 3)
    expiring_1d = await get_expiring_subscriptions(db, 1)
    expired = await get_expired_subscriptions(db)

    text = f"""
⏰ <b>اشتراک‌های در حال انقضا</b>

📊 <b>آمار:</b>
- منقضی می‌شود در ۳ روز: {len(expiring_3d)}
- فردا منقضی می‌شود: {len(expiring_1d)}
- قبلاً منقضی شده: {len(expired)}

<b>منقضی می‌شود در ۳ روز:</b>
"""

    for sub in expiring_3d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else 'ناشناس'
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_3d) > 5:
        text += f'... و {len(expiring_3d) - 5} مورد دیگر\n'

    text += '\n<b>فردا منقضی می‌شود:</b>\n'
    for sub in expiring_1d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else 'ناشناس'
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_1d) > 5:
        text += f'... و {len(expiring_1d) - 5} مورد دیگر\n'

    keyboard = [
        [types.InlineKeyboardButton(text='📨 ارسال یادآوری', callback_data='admin_send_expiry_reminders')],
        [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_subs_expiring')],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_subscriptions')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_subscriptions_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_subscriptions_statistics(db)

    expiring_3d = await get_expiring_subscriptions(db, 3)
    expiring_7d = await get_expiring_subscriptions(db, 7)
    expired = await get_expired_subscriptions(db)

    text = f"""
📊 <b>آمار تفصیلی اشتراک‌ها</b>

<b>📱 اطلاعات کلی:</b>
• مجموع اشتراک‌ها: {stats['total_subscriptions']}
• فعال: {stats['active_subscriptions']}
• غیرفعال: {stats['total_subscriptions'] - stats['active_subscriptions']}

<b>💎 بر اساس نوع:</b>
• پولی: {stats['paid_subscriptions']}
• آزمایشی: {stats['trial_subscriptions']}

<b>📈 فروش:</b>
• امروز: {stats['purchased_today']}
• این هفته: {stats['purchased_week']}
• این ماه: {stats['purchased_month']}

<b>⏰ انقضا:</b>
• منقضی می‌شود در ۳ روز: {len(expiring_3d)}
• منقضی می‌شود در ۷ روز: {len(expiring_7d)}
• قبلاً منقضی شده: {len(expired)}

<b>💰 تبدیل:</b>
• از آزمایشی به پولی: {stats.get('trial_to_paid_conversion', 0)}%
• تعداد تمدیدها: {stats.get('renewals_count', 0)}
"""

    keyboard = [
        # [
        #     types.InlineKeyboardButton(text="📊 خروجی داده‌ها", callback_data="admin_subs_export"),
        #     types.InlineKeyboardButton(text="📈 نمودارها", callback_data="admin_subs_charts")
        # ],
        # [types.InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data="admin_subs_stats")],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_subscriptions')]
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_countries_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        from app.services.remnawave_service import RemnaWaveService

        remnawave_service = RemnaWaveService()

        nodes_data = await remnawave_service.get_all_nodes()
        squads_data = await remnawave_service.get_all_squads()

        text = '🌍 <b>مدیریت کشورها</b>\n\n'

        if nodes_data:
            text += '<b>سرورهای در دسترس:</b>\n'
            countries = {}

            for node in nodes_data:
                country_code = node.get('country_code', 'XX')
                country_name = country_code

                if country_name not in countries:
                    countries[country_name] = []
                countries[country_name].append(node)

            for country, nodes in countries.items():
                active_nodes = len([n for n in nodes if n.get('is_connected') and n.get('is_node_online')])
                total_nodes = len(nodes)

                country_flag = get_country_flag(country)
                text += f'{country_flag} {country}: {active_nodes}/{total_nodes} سرور\n'

                total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)
                if total_users_online > 0:
                    text += f'   👥 کاربران آنلاین: {total_users_online}\n'
        else:
            text += '❌ بارگذاری اطلاعات سرورها ناموفق بود\n'

        if squads_data:
            text += f'\n<b>مجموع اسکوادها:</b> {len(squads_data)}\n'

            total_members = sum(squad.get('members_count', 0) for squad in squads_data)
            text += f'<b>اعضای اسکوادها:</b> {total_members}\n'

            text += '\n<b>اسکوادها:</b>\n'
            for squad in squads_data[:5]:
                name = squad.get('name', 'نامشخص')
                members = squad.get('members_count', 0)
                inbounds = squad.get('inbounds_count', 0)
                text += f'• {name}: {members} عضو، {inbounds} inbound(s)\n'

            if len(squads_data) > 5:
                text += f'... و {len(squads_data) - 5} اسکواد دیگر\n'

        user_stats = await get_users_by_countries(db)
        if user_stats:
            text += '\n<b>کاربران بر اساس منطقه:</b>\n'
            for country, count in user_stats.items():
                country_flag = get_country_flag(country)
                text += f'{country_flag} {country}: {count} کاربر\n'

    except Exception as e:
        logger.error('Error fetching country data', error=e)
        text = f"""
🌍 <b>مدیریت کشورها</b>

❌ <b>خطا در بارگذاری اطلاعات</b>
اطلاعات سرورها بارگذاری نشد.

اتصال به RemnaWave API را بررسی کنید.

<b>جزئیات خطا:</b> {e!s}
"""

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_subs_countries')],
        [
            types.InlineKeyboardButton(text='📊 آمار نودها', callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text='🔧 اسکوادها', callback_data='admin_rw_squads'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_subscriptions')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def send_expiry_reminders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '📨 در حال ارسال یادآوری‌ها...\n\nلطفاً صبر کنید، ممکن است کمی طول بکشد.', reply_markup=None
    )

    expiring_subs = await get_expiring_subscriptions(db, 1)
    sent_count = 0

    for subscription in expiring_subs:
        if subscription.user:
            try:
                user = subscription.user
                # Skip email-only users (no telegram_id)
                if not user.telegram_id:
                    logger.debug('Skipping email-only user for reminder', user_id=user.id)
                    continue

                days_left = max(1, subscription.days_left)

                tariff_label = ''
                if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
                    tariff_label = f' «{subscription.tariff.name}»'
                reminder_text = f"""
⚠️ <b>اشتراک{tariff_label} در حال انقضاست!</b>

اشتراک شما تا {days_left} روز دیگر منقضی می‌شود.

فراموش نکنید که اشتراک خود را تمدید کنید تا دسترسی به سرورها از دست نرود.

💎 می‌توانید از منوی اصلی اشتراک را تمدید کنید.
"""

                await callback.bot.send_message(chat_id=user.telegram_id, text=reminder_text)
                sent_count += 1

            except Exception as e:
                logger.error('Error sending reminder to user', user_id=subscription.user_id, error=e)

    await callback.message.edit_text(
        f'✅ یادآوری‌ها ارسال شد: {sent_count} از {len(expiring_subs)}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_subs_expiring')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_subscriptions_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_subscriptions_list(callback, db_user, db, page)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_subscriptions_menu, F.data == 'admin_subscriptions')
    dp.callback_query.register(show_subscriptions_list, F.data == 'admin_subs_list')
    dp.callback_query.register(show_expiring_subscriptions, F.data == 'admin_subs_expiring')
    dp.callback_query.register(show_subscriptions_stats, F.data == 'admin_subs_stats')
    dp.callback_query.register(show_countries_management, F.data == 'admin_subs_countries')
    dp.callback_query.register(send_expiry_reminders, F.data == 'admin_send_expiry_reminders')

    dp.callback_query.register(handle_subscriptions_pagination, F.data.startswith('admin_subs_list_page_'))
