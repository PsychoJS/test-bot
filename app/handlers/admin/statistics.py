from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_referral_statistics
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import get_revenue_by_period, get_transactions_statistics
from app.database.models import User
from app.keyboards.admin import get_admin_statistics_keyboard
from app.services.user_service import UserService
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_percentage


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
📊 <b>آمار سیستم</b>

بخشی را برای مشاهده آمار انتخاب کنید:
"""

    await callback.message.edit_text(text, reply_markup=get_admin_statistics_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    total_users = stats['total_users']
    active_rate = format_percentage(stats['active_users'] / total_users * 100 if total_users > 0 else 0)

    current_time = format_datetime(datetime.now(UTC))

    text = f"""
👥 <b>آمار کاربران</b>

<b>شاخص‌های کلی:</b>
- کل ثبت‌نام‌شدگان: {stats['total_users']}
- فعال: {stats['active_users']} ({active_rate})
- مسدودشده: {stats['blocked_users']}

<b>ثبت‌نام‌های جدید:</b>
- امروز: {stats['new_today']}
- این هفته: {stats['new_week']}
- این ماه: {stats['new_month']}

<b>فعالیت:</b>
- نرخ فعالیت: {active_rate}
- رشد ماهانه: +{stats['new_month']} ({format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0)})

<b>به‌روزرسانی:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_stats_users')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 داده‌ها به‌روز هستند', show_alert=False)
        else:
            logger.error('Error updating user statistics', error=e)
            await callback.answer('❌ خطا در به‌روزرسانی داده‌ها', show_alert=True)
            return

    await callback.answer('✅ آمار به‌روزرسانی شد')


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.now(UTC))

    text = f"""
📱 <b>آمار اشتراک‌ها</b>

<b>شاخص‌های کلی:</b>
- کل اشتراک‌ها: {stats['total_subscriptions']}
- فعال: {stats['active_subscriptions']}
- پولی: {stats['paid_subscriptions']}
- آزمایشی: {stats['trial_subscriptions']}

<b>تبدیل:</b>
- از آزمایشی به پولی: {conversion_rate}
- اشتراک‌های پولی فعال: {stats['paid_subscriptions']}

<b>فروش:</b>
- امروز: {stats['purchased_today']}
- این هفته: {stats['purchased_week']}
- این ماه: {stats['purchased_month']}

<b>به‌روزرسانی:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_stats_subs')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ آمار به‌روزرسانی شد')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 داده‌ها به‌روز هستند', show_alert=False)
        else:
            logger.error('Error updating subscription statistics', error=e)
            await callback.answer('❌ خطا در به‌روزرسانی داده‌ها', show_alert=True)


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=now)
    current_time = format_datetime(datetime.now(UTC))

    text = f"""
💰 <b>آمار درآمدها</b>

<b>ماه جاری:</b>
- درآمد: {settings.format_price(month_stats['totals']['income_kopeks'])}
- هزینه: {settings.format_price(month_stats['totals']['expenses_kopeks'])}
- سود: {settings.format_price(month_stats['totals']['profit_kopeks'])}
- از اشتراک‌ها: {settings.format_price(abs(month_stats['totals']['subscription_income_kopeks']))}

<b>امروز:</b>
- تراکنش‌ها: {month_stats['today']['transactions_count']}
- درآمد: {settings.format_price(month_stats['today']['income_kopeks'])}

<b>همه زمان‌ها:</b>
- کل درآمد: {settings.format_price(all_time_stats['totals']['income_kopeks'])}
- کل سود: {settings.format_price(all_time_stats['totals']['profit_kopeks'])}

<b>روش‌های پرداخت:</b>
"""

    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            text += f'• {method}: {data["count"]} ({settings.format_price(data["amount"])})\n'

    text += f'\n<b>به‌روزرسانی:</b> {current_time}'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            # [types.InlineKeyboardButton(text="📈 Период", callback_data="admin_revenue_period")],
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_stats_revenue')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ آمار به‌روزرسانی شد')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 داده‌ها به‌روز هستند', show_alert=False)
        else:
            logger.error('Error updating revenue statistics', error=e)
            await callback.answer('❌ خطا در به‌روزرسانی داده‌ها', show_alert=True)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.now(UTC))

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    text = f"""
🤝 <b>آمار ارجاع‌ها</b>

<b>شاخص‌های کلی:</b>
- کاربران دارای ارجاع: {stats['users_with_referrals']}
- ارجاع‌دهندگان فعال: {stats['active_referrers']}
- مجموع پرداخت‌شده: {settings.format_price(stats['total_paid_kopeks'])}

<b>بازه زمانی:</b>
- امروز: {settings.format_price(stats['today_earnings_kopeks'])}
- این هفته: {settings.format_price(stats['week_earnings_kopeks'])}
- این ماه: {settings.format_price(stats['month_earnings_kopeks'])}

<b>میانگین:</b>
- به ازای هر ارجاع‌دهنده: {settings.format_price(int(avg_per_referrer))}

<b>برترین ارجاع‌دهندگان:</b>
"""

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = referrer['display_name']
            earned = settings.format_price(referrer['total_earned_kopeks'])
            count = referrer['referrals_count']
            text += f'{i}. {name}: {earned} ({count} نفر)\n'
    else:
        text += 'هنوز هیچ ارجاع‌دهنده فعالی وجود ندارد'

    text += f'\n<b>به‌روزرسانی:</b> {current_time}'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_stats_referrals')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ آمار به‌روزرسانی شد')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 داده‌ها به‌روز هستند', show_alert=False)
        else:
            logger.error('Error updating referral statistics', error=e)
            await callback.answer('❌ خطا در به‌روزرسانی داده‌ها', show_alert=True)


@admin_required
@error_handler
async def show_summary_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    user_stats = await user_service.get_user_statistics(db)
    sub_stats = await get_subscriptions_statistics(db)

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_stats = await get_transactions_statistics(db, month_start, now)
    current_time = format_datetime(datetime.now(UTC))

    conversion_rate = 0
    if user_stats['total_users'] > 0:
        conversion_rate = sub_stats['paid_subscriptions'] / user_stats['total_users'] * 100

    arpu = 0
    if user_stats['active_users'] > 0:
        arpu = revenue_stats['totals']['income_kopeks'] / user_stats['active_users']

    text = f"""
📊 <b>خلاصه کلی سیستم</b>

<b>کاربران:</b>
- کل: {user_stats['total_users']}
- فعال: {user_stats['active_users']}
- جدید این ماه: {user_stats['new_month']}

<b>اشتراک‌ها:</b>
- فعال: {sub_stats['active_subscriptions']}
- پولی: {sub_stats['paid_subscriptions']}
- تبدیل: {format_percentage(conversion_rate)}

<b>مالی (ماه):</b>
- درآمد: {settings.format_price(revenue_stats['totals']['income_kopeks'])}
- ARPU: {settings.format_price(int(arpu))}
- تراکنش‌ها: {sum(data['count'] for data in revenue_stats['by_type'].values())}

<b>رشد:</b>
- کاربران: +{user_stats['new_month']} این ماه
- فروش: +{sub_stats['purchased_month']} این ماه

<b>به‌روزرسانی:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_stats_summary')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ آمار به‌روزرسانی شد')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 داده‌ها به‌روز هستند', show_alert=False)
        else:
            logger.error('Error updating summary statistics', error=e)
            await callback.answer('❌ خطا در به‌روزرسانی داده‌ها', show_alert=True)


@admin_required
@error_handler
async def show_revenue_by_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    period = callback.data.split('_')[-1]

    period_map = {'today': 1, 'yesterday': 1, 'week': 7, 'month': 30, 'all': 365}

    days = period_map.get(period, 30)
    revenue_data = await get_revenue_by_period(db, days)

    if period == 'yesterday':
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        revenue_data = [r for r in revenue_data if r['date'] == yesterday]
    elif period == 'today':
        today = datetime.now(UTC).date()
        revenue_data = [r for r in revenue_data if r['date'] == today]

    total_revenue = sum(r['amount_kopeks'] for r in revenue_data)
    avg_daily = total_revenue / len(revenue_data) if revenue_data else 0

    text = f"""
📈 <b>درآمدها برای دوره: {period}</b>

<b>خلاصه:</b>
- کل درآمد: {settings.format_price(total_revenue)}
- روزهای دارای داده: {len(revenue_data)}
- میانگین درآمد روزانه: {settings.format_price(int(avg_daily))}

<b>به تفکیک روز:</b>
"""

    for revenue in revenue_data[-10:]:
        text += f'• {revenue["date"].strftime("%d.%m")}: {settings.format_price(revenue["amount_kopeks"])}\n'

    if len(revenue_data) > 10:
        text += f'... و {len(revenue_data) - 10} روز دیگر'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📊 دوره دیگر', callback_data='admin_revenue_period')],
                [types.InlineKeyboardButton(text='⬅️ به درآمدها', callback_data='admin_stats_revenue')],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_statistics_menu, F.data == 'admin_statistics')
    dp.callback_query.register(show_users_statistics, F.data == 'admin_stats_users')
    dp.callback_query.register(show_subscriptions_statistics, F.data == 'admin_stats_subs')
    dp.callback_query.register(show_revenue_statistics, F.data == 'admin_stats_revenue')
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_stats_referrals')
    dp.callback_query.register(show_summary_statistics, F.data == 'admin_stats_summary')
    dp.callback_query.register(show_revenue_by_period, F.data.startswith('period_'))

    periods = ['today', 'yesterday', 'week', 'month', 'all']
    for period in periods:
        dp.callback_query.register(show_revenue_by_period, F.data == f'period_{period}')
