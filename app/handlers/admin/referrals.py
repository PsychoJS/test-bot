import asyncio
import html
import json
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import (
    get_referral_statistics,
    get_top_referrers_by_period,
)
from app.database.crud.user import get_user_by_id, get_user_by_telegram_id
from app.database.models import ReferralEarning, User, WithdrawalRequest, WithdrawalRequestStatus
from app.localization.texts import get_texts
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        stats = await get_referral_statistics(db)

        avg_per_referrer = 0
        if stats.get('active_referrers', 0) > 0:
            avg_per_referrer = stats.get('total_paid_kopeks', 0) / stats['active_referrers']

        current_time = datetime.now(UTC).strftime('%H:%M:%S')

        text = f"""
🤝 <b>آمار ارجاعات</b>

<b>شاخص‌های کلی:</b>
- کاربران با معرفی: {stats.get('users_with_referrals', 0)}
- معرفان فعال: {stats.get('active_referrers', 0)}
- مجموع پرداخت‌شده: {settings.format_price(stats.get('total_paid_kopeks', 0))}

<b>بر اساس دوره:</b>
- امروز: {settings.format_price(stats.get('today_earnings_kopeks', 0))}
- هفت روز گذشته: {settings.format_price(stats.get('week_earnings_kopeks', 0))}
- ماه گذشته: {settings.format_price(stats.get('month_earnings_kopeks', 0))}

<b>میانگین شاخص‌ها:</b>
- به ازای هر معرف: {settings.format_price(int(avg_per_referrer))}

<b>۵ معرف برتر:</b>
"""

        top_referrers = stats.get('top_referrers', [])
        if top_referrers:
            for i, referrer in enumerate(top_referrers[:5], 1):
                earned = referrer.get('total_earned_kopeks', 0)
                count = referrer.get('referrals_count', 0)
                user_id = referrer.get('user_id', 'N/A')

                if count > 0:
                    text += f'{i}. ID {user_id}: {settings.format_price(earned)} ({count} ref.)\n'
                else:
                    logger.warning('Referrer has referrals but appears in top', user_id=user_id, count=count)
        else:
            text += 'داده‌ای وجود ندارد\n'

        text += f"""

<b>تنظیمات سیستم معرفی:</b>
- حداقل شارژ: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
- بونوس اول شارژ: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
- بونوس دعوت‌کننده: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}
- کمیسیون خریدها: {settings.REFERRAL_COMMISSION_PERCENT}%
- اعلان‌ها: {'✅ فعال' if settings.REFERRAL_NOTIFICATIONS_ENABLED else '❌ غیرفعال'}

<i>🕐 به‌روزشده: {current_time}</i>
"""

        keyboard_rows = [
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_referrals')],
            [types.InlineKeyboardButton(text='👥 برترین معرف‌ها', callback_data='admin_referrals_top')],
            [types.InlineKeyboardButton(text='🔍 تشخیص لاگ‌ها', callback_data='admin_referral_diagnostics')],
        ]

        # Withdrawal request button (if feature is enabled)
        if settings.is_referral_withdrawal_enabled():
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='💸 درخواست‌های برداشت', callback_data='admin_withdrawal_requests')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='⚙️ تنظیمات', callback_data='admin_referrals_settings')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_panel')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer('به‌روزرسانی شد')
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer('داده‌ها به‌روز هستند')
            else:
                logger.error('Message edit error', edit_error=edit_error)
                await callback.answer('خطا در به‌روزرسانی')

    except Exception as e:
        logger.error('Error in show_referral_statistics', error=e, exc_info=True)

        current_time = datetime.now(UTC).strftime('%H:%M:%S')
        text = f"""
🤝 <b>آمار معرفی</b>

❌ <b>خطا در بارگذاری داده‌ها</b>

<b>تنظیمات فعلی:</b>
- حداقل شارژ: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
- بونوس اول شارژ: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
- بونوس دعوت‌کننده: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}
- کمیسیون خریدها: {settings.REFERRAL_COMMISSION_PERCENT}%

<i>🕐 زمان: {current_time}</i>
"""

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 تکرار', callback_data='admin_referrals')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_panel')],
            ]
        )

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except:
            pass
        await callback.answer('خطا در بارگذاری آمار')


def _get_top_keyboard(period: str, sort_by: str) -> types.InlineKeyboardMarkup:
    """Creates keyboard for period and sorting selection."""
    period_week = '✅ هفته' if period == 'week' else 'هفته'
    period_month = '✅ ماه' if period == 'month' else 'ماه'
    sort_earnings = '✅ بر اساس درآمد' if sort_by == 'earnings' else 'بر اساس درآمد'
    sort_invited = '✅ بر اساس دعوت‌شدگان' if sort_by == 'invited' else 'بر اساس دعوت‌شدگان'

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=period_week, callback_data=f'admin_top_ref:week:{sort_by}'),
                types.InlineKeyboardButton(text=period_month, callback_data=f'admin_top_ref:month:{sort_by}'),
            ],
            [
                types.InlineKeyboardButton(text=sort_earnings, callback_data=f'admin_top_ref:{period}:earnings'),
                types.InlineKeyboardButton(text=sort_invited, callback_data=f'admin_top_ref:{period}:invited'),
            ],
            [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data=f'admin_top_ref:{period}:{sort_by}')],
            [types.InlineKeyboardButton(text='⬅️ به آمار', callback_data='admin_referrals')],
        ]
    )


@admin_required
@error_handler
async def show_top_referrers(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Shows top referrers (default: week, by earnings)."""
    await _show_top_referrers_filtered(callback, db, period='week', sort_by='earnings')


@admin_required
@error_handler
async def show_top_referrers_filtered(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Processes period and sorting selection."""
    # Parse callback_data: admin_top_ref:period:sort_by
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('خطا در پارامترها')
        return

    period = parts[1]  # week or month
    sort_by = parts[2]  # earnings or invited

    if period not in ('week', 'month'):
        period = 'week'
    if sort_by not in ('earnings', 'invited'):
        sort_by = 'earnings'

    await _show_top_referrers_filtered(callback, db, period, sort_by)


async def _show_top_referrers_filtered(callback: types.CallbackQuery, db: AsyncSession, period: str, sort_by: str):
    """Internal function to display top with filters."""
    try:
        top_referrers = await get_top_referrers_by_period(db, period=period, sort_by=sort_by)

        period_text = 'هفتگی' if period == 'week' else 'ماهانه'
        sort_text = 'بر اساس درآمد' if sort_by == 'earnings' else 'بر اساس دعوت‌شدگان'

        text = f'🏆 <b>برترین معرف‌ها {period_text}</b>\n'
        text += f'<i>مرتب‌سازی: {sort_text}</i>\n\n'

        if top_referrers:
            for i, referrer in enumerate(top_referrers[:20], 1):
                earned = referrer.get('earnings_kopeks', 0)
                count = referrer.get('invited_count', 0)
                display_name = referrer.get('display_name', 'N/A')
                username = referrer.get('username', '')
                telegram_id = referrer.get('telegram_id')
                user_email = referrer.get('email', '')
                user_id = referrer.get('user_id', '')
                id_display = telegram_id or user_email or f'#{user_id}' if user_id else 'N/A'

                if username:
                    display_text = f'@{html.escape(username)} (ID{id_display})'
                elif display_name and display_name != f'ID{id_display}':
                    display_text = f'{html.escape(display_name)} (ID{id_display})'
                else:
                    display_text = f'ID{id_display}'

                emoji = ''
                if i == 1:
                    emoji = '🥇 '
                elif i == 2:
                    emoji = '🥈 '
                elif i == 3:
                    emoji = '🥉 '

                # Highlight the main metric based on sorting
                if sort_by == 'invited':
                    text += f'{emoji}{i}. {display_text}\n'
                    text += f'   👥 <b>{count} دعوت‌شده</b> | 💰 {settings.format_price(earned)}\n\n'
                else:
                    text += f'{emoji}{i}. {display_text}\n'
                    text += f'   💰 <b>{settings.format_price(earned)}</b> | 👥 {count} دعوت‌شده\n\n'
        else:
            text += 'داده‌ای برای دوره انتخاب‌شده وجود ندارد\n'

        keyboard = _get_top_keyboard(period, sort_by)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer('داده‌ها به‌روز هستند')
            else:
                raise

    except Exception as e:
        logger.error('Error in show_top_referrers_filtered', error=e, exc_info=True)
        await callback.answer('خطا در بارگذاری برترین معرف‌ها')


@admin_required
@error_handler
async def show_referral_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = f"""
⚙️ <b>تنظیمات سیستم معرفی</b>

<b>بونوس‌ها و جوایز:</b>
• حداقل مبلغ شارژ برای شرکت: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
• بونوس اول شارژ معرفی‌شده: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
• بونوس دعوت‌کننده برای اول شارژ: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}

<b>کمیسیون:</b>
• درصد از هر خرید معرفی‌شده: {settings.REFERRAL_COMMISSION_PERCENT}%

<b>اعلان‌ها:</b>
• وضعیت: {'✅ فعال' if settings.REFERRAL_NOTIFICATIONS_ENABLED else '❌ غیرفعال'}
• تعداد تلاش ارسال: {getattr(settings, 'REFERRAL_NOTIFICATION_RETRY_ATTEMPTS', 3)}

<i>💡 برای تغییر تنظیمات، فایل .env را ویرایش کرده و بات را ری‌استارت کنید</i>
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ به آمار', callback_data='admin_referrals')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_pending_withdrawal_requests(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Shows list of pending withdrawal requests."""
    requests = await referral_withdrawal_service.get_pending_requests(db)

    if not requests:
        text = '📋 <b>درخواست‌های برداشت</b>\n\nهیچ درخواست در انتظاری وجود ندارد.'

        keyboard_rows = []
        # Test accrual button (test mode only)
        if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='🧪 پرداخت آزمایشی', callback_data='admin_test_referral_earning')]
            )
        keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_referrals')])

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
        await callback.answer()
        return

    text = f'📋 <b>درخواست‌های برداشت ({len(requests)})</b>\n\n'

    for req in requests[:10]:
        user = await get_user_by_id(db, req.user_id)
        user_name = html.escape(user.full_name) if user and user.full_name else 'ناشناس'
        user_tg_id = user.telegram_id if user else 'N/A'

        risk_emoji = (
            '🟢' if req.risk_score < 30 else '🟡' if req.risk_score < 50 else '🟠' if req.risk_score < 70 else '🔴'
        )

        text += f'<b>#{req.id}</b> — {user_name} (ID{user_tg_id})\n'
        text += f'💰 {req.amount_kopeks / 100:.0f}₽ | {risk_emoji} ریسک: {req.risk_score}/100\n'
        text += f'📅 {req.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    keyboard_rows = []
    for req in requests[:5]:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{req.id} — {req.amount_kopeks / 100:.0f}₽', callback_data=f'admin_withdrawal_view_{req.id}'
                )
            ]
        )

    # Test accrual button (test mode only)
    if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        keyboard_rows.append(
            [types.InlineKeyboardButton(text='🧪 پرداخت آزمایشی', callback_data='admin_test_referral_earning')]
        )

    keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_referrals')])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()


@admin_required
@error_handler
async def view_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Shows details of a withdrawal request."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('درخواست یافت نشد', show_alert=True)
        return

    user = await get_user_by_id(db, request.user_id)
    user_name = html.escape(user.full_name) if user and user.full_name else 'ناشناس'
    user_tg_id = (user.telegram_id or user.email or f'#{user.id}') if user else 'N/A'

    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    status_text = {
        WithdrawalRequestStatus.PENDING.value: '⏳ در انتظار',
        WithdrawalRequestStatus.APPROVED.value: '✅ تأیید شده',
        WithdrawalRequestStatus.REJECTED.value: '❌ رد شده',
        WithdrawalRequestStatus.COMPLETED.value: '✅ انجام شده',
        WithdrawalRequestStatus.CANCELLED.value: '🚫 لغو شده',
    }.get(request.status, request.status)

    text = f"""
📋 <b>درخواست #{request.id}</b>

👤 کاربر: {user_name}
🆔 ID: <code>{user_tg_id}</code>
💰 مبلغ: <b>{request.amount_kopeks / 100:.0f}₽</b>
📊 وضعیت: {status_text}

💳 <b>اطلاعات پرداخت:</b>
<code>{html.escape(request.payment_details or '')}</code>

📅 ایجاد شده: {request.created_at.strftime('%d.%m.%Y %H:%M')}

{referral_withdrawal_service.format_analysis_for_admin(analysis)}
"""

    keyboard = []

    if request.status == WithdrawalRequestStatus.PENDING.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(text='✅ تأیید', callback_data=f'admin_withdrawal_approve_{request.id}'),
                types.InlineKeyboardButton(text='❌ رد', callback_data=f'admin_withdrawal_reject_{request.id}'),
            ]
        )

    if request.status == WithdrawalRequestStatus.APPROVED.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text='✅ پول واریز شد', callback_data=f'admin_withdrawal_complete_{request.id}'
                )
            ]
        )

    if user:
        keyboard.append(
            [types.InlineKeyboardButton(text='👤 پروفایل کاربر', callback_data=f'admin_user_manage_{user.id}')]
        )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ به لیست', callback_data='admin_withdrawal_requests')])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def approve_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Approves a withdrawal request."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('درخواست یافت نشد', show_alert=True)
        return

    success, error = await referral_withdrawal_service.approve_request(db, request_id, db_user.id)

    if success:
        # Notify user (only if telegram_id exists)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_APPROVED',
                        '✅ <b>درخواست برداشت #{id} تأیید شد!</b>\n\n'
                        'مبلغ: <b>{amount}</b>\n'
                        'وجه از موجودی کسر شد.\n\n'
                        'منتظر واریز به اطلاعات پرداختی خود باشید.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Failed to send notification to user', error=e)

        await callback.answer('✅ درخواست تأیید شد، مبلغ از موجودی کسر شد')

        # Update display
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(f'❌ {error}', show_alert=True)


@admin_required
@error_handler
async def reject_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Rejects a withdrawal request."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('درخواست یافت نشد', show_alert=True)
        return

    success, _error = await referral_withdrawal_service.reject_request(
        db, request_id, db_user.id, 'Rejected by admin'
    )

    if success:
        # Notify user (only if telegram_id exists)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_REJECTED',
                        '❌ <b>درخواست برداشت #{id} رد شد</b>\n\n'
                        'مبلغ: <b>{amount}</b>\n\n'
                        'در صورت داشتن سوال با پشتیبانی تماس بگیرید.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Failed to send notification to user', error=e)

        await callback.answer('❌ درخواست رد شد')

        # Update display
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer('❌ خطا در رد درخواست', show_alert=True)


@admin_required
@error_handler
async def complete_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Marks a request as completed (money transferred)."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('درخواست یافت نشد', show_alert=True)
        return

    success, _error = await referral_withdrawal_service.complete_request(db, request_id, db_user.id, 'Transfer completed')

    if success:
        # Notify user (only if telegram_id exists)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_COMPLETED',
                        '💸 <b>پرداخت برداشت #{id} انجام شد!</b>\n\n'
                        'مبلغ: <b>{amount}</b>\n\n'
                        'پول به اطلاعات پرداختی شما ارسال شد.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Failed to send notification to user', error=e)

        await callback.answer('✅ درخواست انجام شد')

        # Update display
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer('❌ خطا در انجام درخواست', show_alert=True)


@admin_required
@error_handler
async def start_test_referral_earning(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Starts the process of test referral income accrual."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await callback.answer('حالت آزمایشی غیرفعال است', show_alert=True)
        return

    await state.set_state(AdminStates.test_referral_earning_input)

    text = """
🧪 <b>پرداخت آزمایشی درآمد معرفی</b>

داده‌ها را به فرمت زیر وارد کنید:
<code>telegram_id مبلغ_به_تومان</code>

مثال‌ها:
• <code>123456789 500</code> — 500₽ به کاربر 123456789 پرداخت می‌شود
• <code>987654321 1000</code> — 1000₽ به کاربر 987654321 پرداخت می‌شود

⚠️ این یک رکورد واقعی ReferralEarning ایجاد می‌کند، گویی کاربر از معرفی درآمد کسب کرده است.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_withdrawal_requests')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def process_test_referral_earning(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Processes test accrual input."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await message.answer('❌ حالت آزمایشی غیرفعال است')
        await state.clear()
        return

    text_input = message.text.strip()
    parts = text_input.split()

    if len(parts) != 2:
        await message.answer(
            '❌ فرمت نادرست. وارد کنید: <code>telegram_id مبلغ</code>\n\nمثلاً: <code>123456789 500</code>'
        )
        return

    try:
        target_telegram_id = int(parts[0])
        amount_rubles = float(parts[1].replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer('❌ مبلغ باید مثبت باشد')
            return

        if amount_kopeks > 10000000:  # Limit 100,000₽
            await message.answer('❌ حداکثر مبلغ پرداخت آزمایشی: 100,000₽')
            return

    except ValueError:
        await message.answer(
            '❌ فرمت اعداد نادرست. وارد کنید: <code>telegram_id مبلغ</code>\n\nمثلاً: <code>123456789 500</code>'
        )
        return

    # Find target user
    target_user = await get_user_by_telegram_id(db, target_telegram_id)
    if not target_user:
        await message.answer(f'❌ کاربر با ID {target_telegram_id} در پایگاه داده یافت نشد')
        return

    # Create test accrual
    earning = ReferralEarning(
        user_id=target_user.id,
        referral_id=target_user.id,  # Self-referral (test)
        amount_kopeks=amount_kopeks,
        reason='test_earning',
    )
    db.add(earning)

    # Add to user balance
    from app.database.crud.user import lock_user_for_update

    target_user = await lock_user_for_update(db, target_user)
    target_user.balance_kopeks += amount_kopeks

    await db.commit()
    await state.clear()

    await message.answer(
        f'✅ <b>پرداخت آزمایشی ایجاد شد!</b>\n\n'
        f'👤 کاربر: {html.escape(target_user.full_name) if target_user.full_name else "بدون نام"}\n'
        f'🆔 ID: <code>{target_telegram_id}</code>\n'
        f'💰 مبلغ: <b>{amount_rubles:.0f}₽</b>\n'
        f'💳 موجودی جدید: <b>{target_user.balance_kopeks / 100:.0f}₽</b>\n\n'
        f'پرداخت به عنوان درآمد معرفی اضافه شد.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📋 به درخواست‌ها', callback_data='admin_withdrawal_requests')],
                [types.InlineKeyboardButton(text='👤 پروفایل', callback_data=f'admin_user_manage_{target_user.id}')],
            ]
        ),
    )

    logger.info(
        'Test accrual: admin credited ₽ to user',
        telegram_id=db_user.telegram_id,
        amount_rubles=amount_rubles,
        target_telegram_id=target_telegram_id,
    )


def _get_period_dates(period: str) -> tuple[datetime, datetime]:
    """Returns start and end dates for the given period."""
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == 'today':
        start_date = today
        end_date = today + timedelta(days=1)
    elif period == 'yesterday':
        start_date = today - timedelta(days=1)
        end_date = today
    elif period == 'week':
        start_date = today - timedelta(days=7)
        end_date = today + timedelta(days=1)
    elif period == 'month':
        start_date = today - timedelta(days=30)
        end_date = today + timedelta(days=1)
    else:
        # Default — today
        start_date = today
        end_date = today + timedelta(days=1)

    return start_date, end_date


def _get_period_display_name(period: str) -> str:
    """Returns a human-readable period name."""
    names = {'today': 'امروز', 'yesterday': 'دیروز', 'week': '7 روز', 'month': '30 روز'}
    return names.get(period, 'امروز')


async def _show_diagnostics_for_period(callback: types.CallbackQuery, db: AsyncSession, state: FSMContext, period: str):
    """Internal function to display diagnostics for a given period."""
    try:
        await callback.answer('در حال تحلیل لاگ‌ها...')

        from app.services.referral_diagnostics_service import referral_diagnostics_service

        # Save period in state
        await state.update_data(diagnostics_period=period)
        from app.states import AdminStates

        await state.set_state(AdminStates.referral_diagnostics_period)

        # Get period dates
        start_date, end_date = _get_period_dates(period)

        # Analyze logs
        report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)

        # Build report
        period_display = _get_period_display_name(period)

        text = f"""
🔍 <b>تشخیص معرفی‌ها — {period_display}</b>

<b>📊 آمار کلیک‌ها:</b>
• مجموع کلیک‌های لینک معرفی: {report.total_ref_clicks}
• کاربران یکتا: {report.unique_users_clicked}
• معرفی‌های از‌دست‌رفته: {len(report.lost_referrals)}
"""

        if report.lost_referrals:
            text += '\n<b>❌ معرفی‌های از‌دست‌رفته:</b>\n'
            text += '<i>(از لینک آمدند، اما معرف ثبت نشد)</i>\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # User status
                if not lost.registered:
                    status = '⚠️ در DB نیست'
                elif not lost.has_referrer:
                    status = '❌ بدون معرف'
                else:
                    status = f'⚡ معرف دیگر (ID{lost.current_referrer_id})'

                # Name or ID
                if lost.username:
                    user_name = f'@{html.escape(lost.username)}'
                elif lost.full_name:
                    user_name = html.escape(lost.full_name)
                else:
                    user_name = f'ID{lost.telegram_id}'

                # Expected referrer
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {html.escape(lost.expected_referrer_name)}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Time
                time_str = lost.click_time.strftime('%H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{html.escape(lost.referral_code)}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += f'\n<i>... و {len(report.lost_referrals) - 15} مورد دیگر</i>\n'
        else:
            text += '\n✅ <b>همه معرفی‌ها ثبت شده‌اند!</b>\n'

        # Log file info
        log_path = referral_diagnostics_service.log_path
        log_exists = await asyncio.to_thread(log_path.exists)
        log_size = (await asyncio.to_thread(log_path.stat)).st_size if log_exists else 0

        text += f'\n<i>📂 {log_path.name}'
        if log_exists:
            text += f' ({log_size / 1024:.0f} KB)'
            text += f' | سطرها: {report.lines_in_period}'
        else:
            text += ' (یافت نشد!)'
        text += '</i>'

        # Buttons: only "Today" (current log) and "Upload file" (old logs)
        keyboard_rows = [
            [
                types.InlineKeyboardButton(text='📅 امروز (لاگ فعلی)', callback_data='admin_ref_diag:today'),
            ],
            [types.InlineKeyboardButton(text='📤 آپلود فایل لاگ', callback_data='admin_ref_diag_upload')],
            [types.InlineKeyboardButton(text='🔍 بررسی بونوس‌ها (از DB)', callback_data='admin_ref_check_bonuses')],
            [
                types.InlineKeyboardButton(
                    text='🏆 همگام‌سازی با مسابقه', callback_data='admin_ref_sync_contest'
                )
            ],
        ]

        # Action buttons (only if there are lost referrals)
        if report.lost_referrals:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='📋 پیش‌نمایش اصلاحات', callback_data='admin_ref_fix_preview')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data=f'admin_ref_diag:{period}')],
                [types.InlineKeyboardButton(text='⬅️ به آمار', callback_data='admin_referrals')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in _show_diagnostics_for_period', error=e, exc_info=True)
        await callback.answer('خطا در تحلیل لاگ‌ها', show_alert=True)


@admin_required
@error_handler
async def show_referral_diagnostics(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Shows referral system diagnostics from logs."""
    # Determine period from callback_data or default to "today"
    if ':' in callback.data:
        period = callback.data.split(':')[1]
    else:
        period = 'today'

    await _show_diagnostics_for_period(callback, db, state, period)


@admin_required
@error_handler
async def preview_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Shows preview of lost referral fixes."""
    try:
        await callback.answer('در حال تحلیل...')

        # Get period from state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Check if working with uploaded file
        if period == 'uploaded_file':
            # Use saved report from uploaded file (deserialize)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer('گزارش فایل آپلودشده یافت نشد', show_alert=True)
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = 'فایل آپلودشده'
        else:
            # Get period dates
            start_date, end_date = _get_period_dates(period)

            # Analyze logs
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period)

        if not report.lost_referrals:
            await callback.answer('معرفی از‌دست‌رفته‌ای برای اصلاح وجود ندارد', show_alert=True)
            return

        # Start fix preview
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=False)

        # Build report
        text = f"""
📋 <b>پیش‌نمایش اصلاحات — {period_display}</b>

<b>📊 چه اقداماتی انجام خواهد شد:</b>
• معرفی‌های اصلاح‌شده: {fix_report.users_fixed}
• بونوس معرفی‌شدگان: {settings.format_price(fix_report.bonuses_to_referrals)}
• بونوس معرف‌ها: {settings.format_price(fix_report.bonuses_to_referrers)}
• خطاها: {fix_report.errors}

<b>🔍 جزئیات:</b>
"""

        # Show first 10 details
        for i, detail in enumerate(fix_report.details[:10], 1):
            if detail.username:
                user_name = f'@{html.escape(detail.username)}'
            elif detail.full_name:
                user_name = html.escape(detail.full_name)
            else:
                user_name = f'ID{detail.telegram_id}'

            if detail.error:
                text += f'{i}. {user_name} — ❌ {html.escape(str(detail.error))}\n'
            else:
                text += f'{i}. {user_name}\n'
                if detail.referred_by_set:
                    referrer_display = (
                        html.escape(detail.referrer_name) if detail.referrer_name else f'ID{detail.referrer_id}'
                    )
                    text += f'   • معرف: {referrer_display}\n'
                if detail.had_first_topup:
                    text += f'   • اول شارژ: {settings.format_price(detail.topup_amount_kopeks)}\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += f'   • بونوس معرفی‌شده: {settings.format_price(detail.bonus_to_referral_kopeks)}\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += f'   • بونوس معرف: {settings.format_price(detail.bonus_to_referrer_kopeks)}\n'

        if len(fix_report.details) > 10:
            text += f'\n<i>... و {len(fix_report.details) - 10} مورد دیگر</i>\n'

        text += '\n⚠️ <b>توجه!</b> این فقط پیش‌نمایش است. برای اعمال اصلاحات «اعمال» را بفشارید.'

        # Back button depends on source
        back_button_text = '⬅️ به تشخیص'
        back_button_callback = f'admin_ref_diag:{period}' if period != 'uploaded_file' else 'admin_referral_diagnostics'

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='✅ اعمال اصلاحات', callback_data='admin_ref_fix_apply')],
                [types.InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in preview_referral_fixes', error=e, exc_info=True)
        await callback.answer('خطا در ایجاد پیش‌نمایش', show_alert=True)


@admin_required
@error_handler
async def apply_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Applies lost referral fixes."""
    try:
        await callback.answer('در حال اعمال اصلاحات...')

        # Get period from state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Check if working with uploaded file
        if period == 'uploaded_file':
            # Use saved report from uploaded file (deserialize)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer('گزارش فایل آپلودشده یافت نشد', show_alert=True)
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = 'فایل آپلودشده'
        else:
            # Get period dates
            start_date, end_date = _get_period_dates(period)

            # Analyze logs
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period)

        if not report.lost_referrals:
            await callback.answer('معرفی از‌دست‌رفته‌ای برای اصلاح وجود ندارد', show_alert=True)
            return

        # Apply fixes
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=True)

        # Build report
        text = f"""
✅ <b>اصلاحات اعمال شد — {period_display}</b>

<b>📊 نتایج:</b>
• معرفی‌های اصلاح‌شده: {fix_report.users_fixed}
• بونوس معرفی‌شدگان: {settings.format_price(fix_report.bonuses_to_referrals)}
• بونوس معرف‌ها: {settings.format_price(fix_report.bonuses_to_referrers)}
• خطاها: {fix_report.errors}

<b>🔍 جزئیات:</b>
"""

        # Show first 10 successful details
        success_count = 0
        for detail in fix_report.details:
            if not detail.error and success_count < 10:
                success_count += 1
                if detail.username:
                    user_name = f'@{html.escape(detail.username)}'
                elif detail.full_name:
                    user_name = html.escape(detail.full_name)
                else:
                    user_name = f'ID{detail.telegram_id}'

                text += f'{success_count}. {user_name}\n'
                if detail.referred_by_set:
                    referrer_display = (
                        html.escape(detail.referrer_name) if detail.referrer_name else f'ID{detail.referrer_id}'
                    )
                    text += f'   • معرف: {referrer_display}\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += f'   • بونوس معرفی‌شده: {settings.format_price(detail.bonus_to_referral_kopeks)}\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += f'   • بونوس معرف: {settings.format_price(detail.bonus_to_referrer_kopeks)}\n'

        if fix_report.users_fixed > 10:
            text += f'\n<i>... و {fix_report.users_fixed - 10} اصلاح دیگر</i>\n'

        # Show errors
        if fix_report.errors > 0:
            text += '\n<b>❌ خطاها:</b>\n'
            error_count = 0
            for detail in fix_report.details:
                if detail.error and error_count < 5:
                    error_count += 1
                    if detail.username:
                        user_name = f'@{html.escape(detail.username)}'
                    elif detail.full_name:
                        user_name = html.escape(detail.full_name)
                    else:
                        user_name = f'ID{detail.telegram_id}'
                    text += f'• {user_name}: {html.escape(str(detail.error))}\n'
            if fix_report.errors > 5:
                text += f'<i>... و {fix_report.errors - 5} خطای دیگر</i>\n'

        # Buttons depend on source
        keyboard_rows = []
        if period != 'uploaded_file':
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی تشخیص', callback_data=f'admin_ref_diag:{period}')]
            )
        keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ به آمار', callback_data='admin_referrals')])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

        # Clear saved report from state
        if period == 'uploaded_file':
            await state.update_data(uploaded_file_report=None)

    except Exception as e:
        logger.error('Error in apply_referral_fixes', error=e, exc_info=True)
        await callback.answer('خطا در اعمال اصلاحات', show_alert=True)


# =============================================================================
# Bonus check by DB
# =============================================================================


@admin_required
@error_handler
async def check_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Checks by DB whether all referrals received bonuses."""
    from app.services.referral_diagnostics_service import (
        referral_diagnostics_service,
    )

    await callback.answer('🔍 در حال بررسی بونوس‌ها...')

    try:
        report = await referral_diagnostics_service.check_missing_bonuses(db)

        # Save report to state for later application
        await state.update_data(missing_bonuses_report=report.to_dict())

        text = f"""
🔍 <b>بررسی بونوس‌ها از DB</b>

📊 <b>آمار:</b>
• مجموع معرفی‌شدگان: {report.total_referrals_checked}
• با شارژ ≥ حداقل: {report.referrals_with_topup}
• <b>بدون بونوس: {len(report.missing_bonuses)}</b>
"""

        if report.missing_bonuses:
            text += f"""
💰 <b>نیاز به پرداخت:</b>
• به معرفی‌شدگان: {report.total_missing_to_referrals / 100:.0f}₽
• به معرف‌ها: {report.total_missing_to_referrers / 100:.0f}₽
• <b>جمع: {(report.total_missing_to_referrals + report.total_missing_to_referrers) / 100:.0f}₽</b>

👤 <b>لیست ({len(report.missing_bonuses)} نفر):</b>
"""
            for i, mb in enumerate(report.missing_bonuses[:15], 1):
                referral_name = html.escape(
                    mb.referral_full_name or mb.referral_username or str(mb.referral_telegram_id)
                )
                referrer_name = html.escape(
                    mb.referrer_full_name or mb.referrer_username or str(mb.referrer_telegram_id)
                )
                text += f'\n{i}. <b>{referral_name}</b>'
                text += f'\n   └ معرف: {referrer_name}'
                text += f'\n   └ شارژ: {mb.first_topup_amount_kopeks / 100:.0f}₽'
                text += f'\n   └ بونوس‌ها: {mb.referral_bonus_amount / 100:.0f}₽ + {mb.referrer_bonus_amount / 100:.0f}₽'

            if len(report.missing_bonuses) > 15:
                text += f'\n\n<i>... و {len(report.missing_bonuses) - 15} نفر دیگر</i>'

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='✅ پرداخت همه بونوس‌ها', callback_data='admin_ref_bonus_apply')],
                    [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_ref_check_bonuses')],
                    [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')],
                ]
            )
        else:
            text += '\n✅ <b>همه بونوس‌ها پرداخت شده‌اند!</b>'
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_ref_check_bonuses')],
                    [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')],
                ]
            )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in check_missing_bonuses', error=e, exc_info=True)
        await callback.answer('خطا در بررسی بونوس‌ها', show_alert=True)


@admin_required
@error_handler
async def apply_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Applies missing bonus accrual."""
    from app.services.referral_diagnostics_service import (
        MissingBonusReport,
        referral_diagnostics_service,
    )

    await callback.answer('💰 در حال پرداخت بونوس‌ها...')

    try:
        # Get saved report
        data = await state.get_data()
        report_dict = data.get('missing_bonuses_report')

        if not report_dict:
            await callback.answer('❌ گزارش یافت نشد. بررسی را به‌روز کنید.', show_alert=True)
            return

        report = MissingBonusReport.from_dict(report_dict)

        if not report.missing_bonuses:
            await callback.answer('✅ بونوسی برای پرداخت وجود ندارد', show_alert=True)
            return

        # Apply fixes
        fix_report = await referral_diagnostics_service.fix_missing_bonuses(db, report.missing_bonuses, apply=True)

        text = f"""
✅ <b>بونوس‌ها پرداخت شدند!</b>

📊 <b>نتیجه:</b>
• پردازش‌شده: {fix_report.users_fixed} کاربر
• پرداخت به معرفی‌شدگان: {fix_report.bonuses_to_referrals / 100:.0f}₽
• پرداخت به معرف‌ها: {fix_report.bonuses_to_referrers / 100:.0f}₽
• <b>جمع: {(fix_report.bonuses_to_referrals + fix_report.bonuses_to_referrers) / 100:.0f}₽</b>
"""

        if fix_report.errors > 0:
            text += f'\n⚠️ خطاها: {fix_report.errors}'

        # Clear report from state
        await state.update_data(missing_bonuses_report=None)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔍 بررسی مجدد', callback_data='admin_ref_check_bonuses')],
                [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in apply_missing_bonuses', error=e, exc_info=True)
        await callback.answer('خطا در پرداخت بونوس‌ها', show_alert=True)


@admin_required
@error_handler
async def sync_referrals_with_contest(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Syncs all referrals with active contests."""
    from app.database.crud.referral_contest import get_contests_for_events
    from app.services.referral_contest_service import referral_contest_service

    await callback.answer('🏆 در حال همگام‌سازی با مسابقات...')

    try:
        now_utc = datetime.now(UTC)

        # Get active contests
        paid_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_paid'])
        reg_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_registered'])

        all_contests = list(paid_contests) + list(reg_contests)

        if not all_contests:
            await callback.message.edit_text(
                '❌ <b>مسابقات معرفی فعالی وجود ندارد</b>\n\n'
                'برای همگام‌سازی، یک مسابقه در بخش «مسابقات» ایجاد کنید.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')]
                    ]
                ),
            )
            return

        # Sync each contest
        total_created = 0
        total_updated = 0
        total_skipped = 0
        contest_results = []

        for contest in all_contests:
            stats = await referral_contest_service.sync_contest(db, contest.id)
            if 'error' not in stats:
                total_created += stats.get('created', 0)
                total_updated += stats.get('updated', 0)
                total_skipped += stats.get('skipped', 0)
                contest_results.append(f'• {html.escape(contest.title)}: +{stats.get("created", 0)} جدید')
            else:
                contest_results.append(f'• {html.escape(contest.title)}: خطا')

        text = f"""
🏆 <b>همگام‌سازی با مسابقات انجام شد!</b>

📊 <b>نتیجه:</b>
• مسابقات پردازش‌شده: {len(all_contests)}
• رویدادهای جدید اضافه‌شده: {total_created}
• به‌روزشده: {total_updated}
• رد شده (از قبل وجود دارد): {total_skipped}

📋 <b>به تفکیک مسابقه:</b>
"""
        text += '\n'.join(contest_results)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 همگام‌سازی مجدد', callback_data='admin_ref_sync_contest')],
                [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in sync_referrals_with_contest', error=e, exc_info=True)
        await callback.answer('خطا در همگام‌سازی', show_alert=True)


@admin_required
@error_handler
async def request_log_file_upload(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Requests log file upload for analysis."""
    await state.set_state(AdminStates.waiting_for_log_file)

    text = """
📤 <b>آپلود فایل لاگ برای تحلیل</b>

فایل لاگ را ارسال کنید (پسوند .log یا .txt).

فایل برای تمام مدت ثبت‌شده در لاگ تحلیل خواهد شد.

⚠️ <b>توجه:</b>
• فایل باید متنی (.log, .txt) باشد
• حداکثر اندازه: 50 MB
• پس از تحلیل، فایل به صورت خودکار حذف می‌شود

اگر چرخش لاگ داده‌های قدیمی را حذف کرده، نسخه پشتیبان را آپلود کنید.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_referral_diagnostics')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def receive_log_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Receives and analyzes the uploaded log file."""
    import tempfile
    from pathlib import Path

    if not message.document:
        await message.answer(
            '❌ لطفاً فایل را به عنوان سند ارسال کنید.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Check file extension
    file_name = message.document.file_name or 'unknown'
    file_ext = Path(file_name).suffix.lower()

    if file_ext not in ['.log', '.txt']:
        await message.answer(
            f'❌ فرمت فایل نادرست: {html.escape(file_ext)}\n\nفقط فایل‌های متنی (.log, .txt) پشتیبانی می‌شوند',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Check file size
    max_size = 50 * 1024 * 1024  # 50 MB
    if message.document.file_size > max_size:
        await message.answer(
            f'❌ فایل خیلی بزرگ است: {message.document.file_size / 1024 / 1024:.1f} MB\n\nحداکثر اندازه: 50 MB',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Notify about upload start
    status_message = await message.answer(
        f'📥 در حال دانلود فایل {html.escape(file_name)} ({message.document.file_size / 1024 / 1024:.1f} MB)...'
    )

    temp_file_path = None

    try:
        # Download file to temp directory
        temp_dir = tempfile.gettempdir()
        temp_file_path = str(Path(temp_dir) / f'ref_diagnostics_{message.from_user.id}_{file_name}')

        # Download file
        file = await message.bot.get_file(message.document.file_id)
        await message.bot.download_file(file.file_path, temp_file_path)

        logger.info('File uploaded (bytes)', temp_file_path=temp_file_path, file_size=message.document.file_size)

        # Update status
        await status_message.edit_text(
            f'🔍 در حال تحلیل فایل {html.escape(file_name)}...\n\nممکن است کمی طول بکشد.'
        )

        # Analyze file
        from app.services.referral_diagnostics_service import referral_diagnostics_service

        report = await referral_diagnostics_service.analyze_file(db, temp_file_path)

        # Build report
        text = f"""
🔍 <b>تحلیل فایل لاگ: {html.escape(file_name)}</b>

<b>📊 آمار کلیک‌ها:</b>
• مجموع کلیک‌های لینک معرفی: {report.total_ref_clicks}
• کاربران یکتا: {report.unique_users_clicked}
• معرفی‌های از‌دست‌رفته: {len(report.lost_referrals)}
• سطرهای فایل: {report.lines_in_period}
"""

        if report.lost_referrals:
            text += '\n<b>❌ معرفی‌های از‌دست‌رفته:</b>\n'
            text += '<i>(از لینک آمدند، اما معرف ثبت نشد)</i>\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # User status
                if not lost.registered:
                    status = '⚠️ در DB نیست'
                elif not lost.has_referrer:
                    status = '❌ بدون معرف'
                else:
                    status = f'⚡ معرف دیگر (ID{lost.current_referrer_id})'

                # Name or ID
                if lost.username:
                    user_name = f'@{html.escape(lost.username)}'
                elif lost.full_name:
                    user_name = html.escape(lost.full_name)
                else:
                    user_name = f'ID{lost.telegram_id}'

                # Expected referrer
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {html.escape(lost.expected_referrer_name)}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Time
                time_str = lost.click_time.strftime('%d.%m.%Y %H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{html.escape(lost.referral_code)}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += f'\n<i>... و {len(report.lost_referrals) - 15} مورد دیگر</i>\n'
        else:
            text += '\n✅ <b>همه معرفی‌ها ثبت شده‌اند!</b>\n'

        # Save report to state for later use (serialize to dict)
        await state.update_data(
            diagnostics_period='uploaded_file',
            uploaded_file_report=report.to_dict(),
        )

        # Action buttons
        keyboard_rows = []

        if report.lost_referrals:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='📋 پیش‌نمایش اصلاحات', callback_data='admin_ref_fix_preview')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics')],
                [types.InlineKeyboardButton(text='⬅️ به آمار', callback_data='admin_referrals')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        # Delete status message
        await status_message.delete()

        # Send result
        await message.answer(text, reply_markup=keyboard)

        # Clear state
        await state.set_state(AdminStates.referral_diagnostics_period)

    except Exception as e:
        logger.error('Error processing file', error=e, exc_info=True)

        try:
            await status_message.edit_text(
                f'❌ <b>خطا در تحلیل فایل</b>\n\n'
                f'فایل: {html.escape(file_name)}\n'
                f'خطا: {html.escape(str(e))}\n\n'
                f'بررسی کنید که فایل یک لاگ متنی بات باشد.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔄 تلاش مجدد', callback_data='admin_ref_diag_upload'
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text='⬅️ به تشخیص', callback_data='admin_referral_diagnostics'
                            )
                        ],
                    ]
                ),
            )
        except:
            await message.answer(
                f'❌ خطا در تحلیل فایل: {html.escape(str(e))}',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_referral_diagnostics')]
                    ]
                ),
            )

    finally:
        # Delete temp file
        if temp_file_path and await asyncio.to_thread(Path(temp_file_path).exists):
            try:
                await asyncio.to_thread(Path(temp_file_path).unlink)
                logger.info('Temp file deleted', temp_file_path=temp_file_path)
            except Exception as e:
                logger.error('Error deleting temp file', error=e)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_referrals')
    dp.callback_query.register(show_top_referrers, F.data == 'admin_referrals_top')
    dp.callback_query.register(show_top_referrers_filtered, F.data.startswith('admin_top_ref:'))
    dp.callback_query.register(show_referral_settings, F.data == 'admin_referrals_settings')
    dp.callback_query.register(show_referral_diagnostics, F.data == 'admin_referral_diagnostics')
    dp.callback_query.register(show_referral_diagnostics, F.data.startswith('admin_ref_diag:'))
    dp.callback_query.register(preview_referral_fixes, F.data == 'admin_ref_fix_preview')
    dp.callback_query.register(apply_referral_fixes, F.data == 'admin_ref_fix_apply')

    # Log file upload
    dp.callback_query.register(request_log_file_upload, F.data == 'admin_ref_diag_upload')
    dp.message.register(receive_log_file, AdminStates.waiting_for_log_file)

    # Bonus check by DB
    dp.callback_query.register(check_missing_bonuses, F.data == 'admin_ref_check_bonuses')
    dp.callback_query.register(apply_missing_bonuses, F.data == 'admin_ref_bonus_apply')
    dp.callback_query.register(sync_referrals_with_contest, F.data == 'admin_ref_sync_contest')

    # Withdrawal request handlers
    dp.callback_query.register(show_pending_withdrawal_requests, F.data == 'admin_withdrawal_requests')
    dp.callback_query.register(view_withdrawal_request, F.data.startswith('admin_withdrawal_view_'))
    dp.callback_query.register(approve_withdrawal_request, F.data.startswith('admin_withdrawal_approve_'))
    dp.callback_query.register(reject_withdrawal_request, F.data.startswith('admin_withdrawal_reject_'))
    dp.callback_query.register(complete_withdrawal_request, F.data.startswith('admin_withdrawal_complete_'))

    # Test accrual
    dp.callback_query.register(start_test_referral_earning, F.data == 'admin_test_referral_earning')
    dp.message.register(process_test_referral_earning, AdminStates.test_referral_earning_input)
