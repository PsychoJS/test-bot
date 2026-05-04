import asyncio
import html
from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.keyboards.admin import get_monitoring_keyboard
from app.localization.texts import get_texts
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.traffic_monitoring_service import (
    traffic_monitoring_scheduler,
)
from app.states import AdminStates
from app.utils.decorators import admin_required
from app.utils.pagination import paginate_list


logger = structlog.get_logger(__name__)
router = Router()


def _format_toggle(enabled: bool) -> str:
    return '🟢 روشن' if enabled else '🔴 خاموش'


def _build_notification_settings_view(language: str):
    get_texts(language)
    config = NotificationSettingsService.get_config()

    second_percent = NotificationSettingsService.get_second_wave_discount_percent()
    second_hours = NotificationSettingsService.get_second_wave_valid_hours()
    third_percent = NotificationSettingsService.get_third_wave_discount_percent()
    third_hours = NotificationSettingsService.get_third_wave_valid_hours()
    third_days = NotificationSettingsService.get_third_wave_trigger_days()

    trial_channel_status = _format_toggle(config.get('trial_channel_unsubscribed', {}).get('enabled', True))
    expired_1d_status = _format_toggle(config['expired_1d'].get('enabled', True))
    second_wave_status = _format_toggle(config['expired_second_wave'].get('enabled', True))
    third_wave_status = _format_toggle(config['expired_third_wave'].get('enabled', True))

    summary_text = (
        '🔔 <b>اعلان‌های کاربران</b>\n\n'
        f'• لغو اشتراک کانال: {trial_channel_status}\n'
        f'• ۱ روز پس از انقضا: {expired_1d_status}\n'
        f'• ۲-۳ روز (تخفیف {second_percent}% / {second_hours} ساعت): {second_wave_status}\n'
        f'• {third_days} روز (تخفیف {third_percent}% / {third_hours} ساعت): {third_wave_status}'
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f'{trial_channel_status} • لغو اشتراک کانال',
                    callback_data='admin_mon_notify_toggle_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text='🧪 تست: لغو اشتراک کانال', callback_data='admin_mon_notify_preview_trial_channel'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'{expired_1d_status} • ۱ روز پس از انقضا',
                    callback_data='admin_mon_notify_toggle_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text='🧪 تست: ۱ روز پس از انقضا', callback_data='admin_mon_notify_preview_expired_1d'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'{second_wave_status} • ۲-۳ روز با تخفیف',
                    callback_data='admin_mon_notify_toggle_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text='🧪 تست: تخفیف ۲-۳ روز', callback_data='admin_mon_notify_preview_expired_2d'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'✏️ تخفیف ۲-۳ روز: {second_percent}%', callback_data='admin_mon_notify_edit_2d_percent'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'⏱️ مدت تخفیف ۲-۳ روز: {second_hours} ساعت', callback_data='admin_mon_notify_edit_2d_hours'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'{third_wave_status} • {third_days} روز با تخفیف',
                    callback_data='admin_mon_notify_toggle_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text='🧪 تست: تخفیف پس از چند روز', callback_data='admin_mon_notify_preview_expired_nd'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'✏️ تخفیف {third_days} روز: {third_percent}%',
                    callback_data='admin_mon_notify_edit_nd_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'⏱️ مدت تخفیف {third_days} روز: {third_hours} ساعت',
                    callback_data='admin_mon_notify_edit_nd_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'📆 آستانه اعلان: {third_days} روز', callback_data='admin_mon_notify_edit_nd_threshold'
                )
            ],
            [InlineKeyboardButton(text='🧪 ارسال همه تست‌ها', callback_data='admin_mon_notify_preview_all')],
            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_settings')],
        ]
    )

    return summary_text, keyboard


async def _build_notification_preview_message(language: str, notification_type: str):
    texts = get_texts(language)
    now = datetime.now(UTC)
    price_30_days = settings.format_price(settings.PRICE_30_DAYS)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.keyboards.inline import get_channel_sub_keyboard
    from app.services.channel_subscription_service import channel_subscription_service

    header = '🧪 <b>اعلان آزمایشی مانیتورینگ</b>\n\n'

    if notification_type == 'trial_channel_unsubscribed':
        template = texts.get(
            'TRIAL_CHANNEL_UNSUBSCRIBED',
            (
                '🚫 <b>دسترسی معلق شد</b>\n\n'
                'اشتراک شما در کانال ما یافت نشد، بنابراین اشتراک آزمایشی غیرفعال شد.\n\n'
                'در کانال عضو شوید و «{check_button}» را بفشارید تا دسترسی بازگردد.'
            ),
        )
        check_button = texts.t('CHANNEL_CHECK_BUTTON', '✅ عضو شدم')
        message = template.format(check_button=check_button)
        # Use all required channels for the preview keyboard
        required_channels = await channel_subscription_service.get_required_channels()
        keyboard = get_channel_sub_keyboard(required_channels, language=language)
    elif notification_type == 'expired_1d':
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_1D',
            (
                '⛔ <b>اشتراک به پایان رسید</b>\n\n'
                'دسترسی در {end_date} قطع شد. اشتراک را تمدید کنید تا به سرویس بازگردید.'
            ),
        )
        message = template.format(
            end_date=(now - timedelta(days=1)).strftime('%d.%m.%Y %H:%M'),
            price=price_30_days,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 تمدید اشتراک'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 شارژ موجودی'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 پشتیبانی'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_2d':
        percent = NotificationSettingsService.get_second_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_SECOND_WAVE',
            (
                '🔥 <b>تخفیف {percent}% برای تمدید</b>\n\n'
                'پیشنهاد را فعال کنید تا تخفیف اضافه دریافت کنید. '
                'این تخفیف با گروه پرومو شما جمع می‌شود و تا {expires_at} معتبر است.'
            ),
        )
        message = template.format(
            percent=percent,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
            trigger_days=3,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='🎁 دریافت تخفیف',
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 تمدید اشتراک'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 شارژ موجودی'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 پشتیبانی'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_nd':
        percent = NotificationSettingsService.get_third_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
        trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_THIRD_WAVE',
            (
                '🎁 <b>تخفیف اختصاصی {percent}%</b>\n\n'
                '{trigger_days} روز بدون اشتراک گذشت — بازگردید و تخفیف اضافه را فعال کنید. '
                'این تخفیف با گروه پرومو جمع می‌شود و تا {expires_at} معتبر است.'
            ),
        )
        message = template.format(
            percent=percent,
            trigger_days=trigger_days,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='🎁 دریافت تخفیف',
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 تمدید اشتراک'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 شارژ موجودی'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 پشتیبانی'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    else:
        raise ValueError(f'Unsupported notification type: {notification_type}')

    footer = '\n\n<i>این پیام فقط برای بررسی قالب‌بندی برای شما ارسال شده است.</i>'
    return header + message + footer, keyboard


async def _send_notification_preview(bot, chat_id: int, language: str, notification_type: str) -> None:
    message, keyboard = await _build_notification_preview_message(language, notification_type)
    await bot.send_message(
        chat_id,
        message,
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def _render_notification_settings(callback: CallbackQuery) -> None:
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    text, keyboard = _build_notification_settings_view(language)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)


async def _render_notification_settings_for_state(
    bot,
    chat_id: int,
    message_id: int,
    language: str,
    business_connection_id: str | None = None,
) -> None:
    text, keyboard = _build_notification_settings_view(language)

    edit_kwargs = {
        'text': text,
        'chat_id': chat_id,
        'message_id': message_id,
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }

    if business_connection_id:
        edit_kwargs['business_connection_id'] = business_connection_id

    try:
        await bot.edit_message_text(**edit_kwargs)
    except TelegramBadRequest as exc:
        if 'no text in the message to edit' in (exc.message or '').lower():
            caption_kwargs = {
                'chat_id': chat_id,
                'message_id': message_id,
                'caption': text,
                'parse_mode': 'HTML',
                'reply_markup': keyboard,
            }

            if business_connection_id:
                caption_kwargs['business_connection_id'] = business_connection_id

            await bot.edit_message_caption(**caption_kwargs)
        else:
            raise


@router.callback_query(F.data == 'admin_monitoring')
@admin_required
async def admin_monitoring_menu(callback: CallbackQuery):
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            running_status = '🟢 در حال اجرا' if status['is_running'] else '🔴 متوقف'
            last_update = status['last_update'].strftime('%H:%M:%S') if status['last_update'] else 'هرگز'

            text = f"""
🔍 <b>سیستم مانیتورینگ</b>

📊 <b>وضعیت:</b> {running_status}
🕐 <b>آخرین به‌روزرسانی:</b> {last_update}
⚙️ <b>بازه بررسی:</b> {settings.MONITORING_INTERVAL} دقیقه

📈 <b>آمار ۲۴ ساعت اخیر:</b>
• کل رویدادها: {status['stats_24h']['total_events']}
• موفق: {status['stats_24h']['successful']}
• خطا: {status['stats_24h']['failed']}
• نرخ موفقیت: {status['stats_24h']['success_rate']}%

🔧 یک عملیات انتخاب کنید:
"""

            language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
            keyboard = get_monitoring_keyboard(language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in admin monitoring menu', error=e)
        await callback.answer('❌ خطا در دریافت اطلاعات', show_alert=True)


@router.callback_query(F.data == 'admin_mon_settings')
@admin_required
async def admin_monitoring_settings(callback: CallbackQuery):
    try:
        global_status = (
            '🟢 فعال' if NotificationSettingsService.are_notifications_globally_enabled() else '🔴 غیرفعال'
        )
        second_percent = NotificationSettingsService.get_second_wave_discount_percent()
        third_percent = NotificationSettingsService.get_third_wave_discount_percent()
        third_days = NotificationSettingsService.get_third_wave_trigger_days()

        text = (
            '⚙️ <b>تنظیمات مانیتورینگ</b>\n\n'
            f'🔔 <b>اعلان‌های کاربران:</b> {global_status}\n'
            f'• تخفیف ۲-۳ روز: {second_percent}%\n'
            f'• تخفیف پس از {third_days} روز: {third_percent}%\n\n'
            'یک بخش برای تنظیم انتخاب کنید.'
        )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🔔 اعلان‌های کاربران', callback_data='admin_mon_notify_settings')],
                [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_submenu_settings')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error displaying monitoring settings', error=e)
        await callback.answer('❌ باز کردن تنظیمات ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_settings')
@admin_required
async def admin_notify_settings(callback: CallbackQuery):
    try:
        await _render_notification_settings(callback)
    except Exception as e:
        logger.error('Error displaying notification settings', error=e)
        await callback.answer('❌ بارگذاری تنظیمات ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_channel')
@admin_required
async def toggle_trial_channel_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
    NotificationSettingsService.set_trial_channel_unsubscribed_enabled(not enabled)
    await callback.answer('✅ فعال شد' if not enabled else '⏸️ غیرفعال شد')
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_channel')
@admin_required
async def preview_trial_channel_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_channel_unsubscribed')
        await callback.answer('✅ نمونه ارسال شد')
    except Exception as exc:
        logger.error('Failed to send trial channel preview', exc=exc)
        await callback.answer('❌ ارسال تست ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_1d')
@admin_required
async def toggle_expired_1d_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_expired_1d_enabled()
    NotificationSettingsService.set_expired_1d_enabled(not enabled)
    await callback.answer('✅ فعال شد' if not enabled else '⏸️ غیرفعال شد')
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_1d')
@admin_required
async def preview_expired_1d_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_1d')
        await callback.answer('✅ نمونه ارسال شد')
    except Exception as exc:
        logger.error('Failed to send expired 1d preview', exc=exc)
        await callback.answer('❌ ارسال تست ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_2d')
@admin_required
async def toggle_second_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_second_wave_enabled()
    NotificationSettingsService.set_second_wave_enabled(not enabled)
    await callback.answer('✅ فعال شد' if not enabled else '⏸️ غیرفعال شد')
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_2d')
@admin_required
async def preview_second_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_2d')
        await callback.answer('✅ نمونه ارسال شد')
    except Exception as exc:
        logger.error('Failed to send second wave preview', exc=exc)
        await callback.answer('❌ ارسال تست ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_nd')
@admin_required
async def toggle_third_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_third_wave_enabled()
    NotificationSettingsService.set_third_wave_enabled(not enabled)
    await callback.answer('✅ فعال شد' if not enabled else '⏸️ غیرفعال شد')
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_nd')
@admin_required
async def preview_third_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_nd')
        await callback.answer('✅ نمونه ارسال شد')
    except Exception as exc:
        logger.error('Failed to send third wave preview', exc=exc)
        await callback.answer('❌ ارسال تست ممکن نشد', show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_preview_all')
@admin_required
async def preview_all_notifications(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        chat_id = callback.from_user.id
        for notification_type in [
            'trial_channel_unsubscribed',
            'expired_1d',
            'expired_2d',
            'expired_nd',
        ]:
            await _send_notification_preview(callback.bot, chat_id, language, notification_type)
        await callback.answer('✅ همه اعلان‌های آزمایشی ارسال شدند')
    except Exception as exc:
        logger.error('Failed to send all notification previews', exc=exc)
        await callback.answer('❌ ارسال تست‌ها ممکن نشد', show_alert=True)


async def _start_notification_value_edit(
    callback: CallbackQuery,
    state: FSMContext,
    setting_key: str,
    field: str,
    prompt_key: str,
    default_prompt: str,
):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    await state.set_state(AdminStates.editing_notification_value)
    await state.update_data(
        notification_setting_key=setting_key,
        notification_setting_field=field,
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_business_connection_id=(
            str(getattr(callback.message, 'business_connection_id', None))
            if getattr(callback.message, 'business_connection_id', None) is not None
            else None
        ),
        settings_language=language,
    )
    texts = get_texts(language)
    await callback.answer()
    await callback.message.answer(texts.get(prompt_key, default_prompt))


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_percent')
@admin_required
async def edit_second_wave_percent(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'percent',
        'NOTIFY_PROMPT_SECOND_PERCENT',
        'درصد تخفیف جدید برای اعلان ۲-۳ روزه را وارد کنید (۰-۱۰۰):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_hours')
@admin_required
async def edit_second_wave_hours(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'hours',
        'NOTIFY_PROMPT_SECOND_HOURS',
        'تعداد ساعات اعتبار تخفیف را وارد کنید (۱-۱۶۸):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_percent')
@admin_required
async def edit_third_wave_percent(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'percent',
        'NOTIFY_PROMPT_THIRD_PERCENT',
        'درصد تخفیف جدید برای پیشنهاد دیرهنگام را وارد کنید (۰-۱۰۰):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_hours')
@admin_required
async def edit_third_wave_hours(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'hours',
        'NOTIFY_PROMPT_THIRD_HOURS',
        'تعداد ساعات اعتبار تخفیف را وارد کنید (۱-۱۶۸):',
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_threshold')
@admin_required
async def edit_third_wave_threshold(callback: CallbackQuery, state: FSMContext):
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'trigger',
        'NOTIFY_PROMPT_THIRD_DAYS',
        'پس از چند روز از انقضا پیشنهاد ارسال شود؟ (حداقل ۲):',
    )


@router.callback_query(F.data == 'admin_mon_start')
@admin_required
async def start_monitoring_callback(callback: CallbackQuery):
    try:
        if monitoring_service.is_running:
            await callback.answer('ℹ️ مانیتورینگ قبلاً شروع شده است')
            return

        if not monitoring_service.bot:
            monitoring_service.bot = callback.bot

        asyncio.create_task(monitoring_service.start_monitoring())

        await callback.answer('✅ مانیتورینگ شروع شد!')

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Error starting monitoring', error=e)
        await callback.answer(f'❌ خطای شروع: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_stop')
@admin_required
async def stop_monitoring_callback(callback: CallbackQuery):
    try:
        if not monitoring_service.is_running:
            await callback.answer('ℹ️ مانیتورینگ قبلاً متوقف شده است')
            return

        monitoring_service.stop_monitoring()
        await callback.answer('⏹️ مانیتورینگ متوقف شد!')

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Error stopping monitoring', error=e)
        await callback.answer(f'❌ خطای توقف: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_force_check')
@admin_required
async def force_check_callback(callback: CallbackQuery):
    try:
        await callback.answer('⏳ در حال بررسی اشتراک‌ها...')

        async with AsyncSessionLocal() as db:
            results = await monitoring_service.force_check_subscriptions(db)

            text = f"""
✅ <b>بررسی اجباری تکمیل شد</b>

📊 <b>نتایج بررسی:</b>
• اشتراک‌های منقضی: {results['expired']}
• اشتراک‌های در حال انقضا: {results['expiring']}
• آماده پرداخت خودکار: {results['autopay_ready']}

🕐 <b>زمان بررسی:</b> {datetime.now(UTC).strftime('%H:%M:%S')}

برای بازگشت به منوی مانیتورینگ «بازگشت» را بفشارید.
"""

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')]]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error during forced check', error=e)
        await callback.answer(f'❌ خطای بررسی: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_traffic_check')
@admin_required
async def traffic_check_callback(callback: CallbackQuery):
    """Ручная проверка трафика — использует snapshot и дельту."""
    try:
        # Проверяем, включен ли мониторинг трафика
        if not traffic_monitoring_scheduler.is_enabled():
            await callback.answer(
                '⚠️ مانیتورینگ ترافیک در تنظیمات غیرفعال است\nلطفاً TRAFFIC_FAST_CHECK_ENABLED=true را در .env فعال کنید',
                show_alert=True,
            )
            return

        await callback.answer('⏳ در حال اجرای بررسی ترافیک (دلتا)...')

        # Используем run_fast_check — он сравнивает с snapshot и отправляет уведомления
        from app.services.traffic_monitoring_service import traffic_monitoring_scheduler_v2

        # Устанавливаем бота, если не установлен
        if not traffic_monitoring_scheduler_v2.bot:
            traffic_monitoring_scheduler_v2.set_bot(callback.bot)

        violations = await traffic_monitoring_scheduler_v2.run_fast_check_now()

        # Получаем информацию о snapshot
        snapshot_age = await traffic_monitoring_scheduler_v2.service.get_snapshot_age_minutes()
        threshold_gb = traffic_monitoring_scheduler_v2.service.get_fast_check_threshold_gb()

        text = f"""
📊 <b>بررسی ترافیک تکمیل شد</b>

🔍 <b>نتایج (دلتا):</b>
• تجاوزها در بازه: {len(violations)}
• آستانه دلتا: {threshold_gb} گیگابایت
• سن snapshot: {snapshot_age:.1f} دقیقه

🕐 <b>زمان بررسی:</b> {datetime.now(UTC).strftime('%H:%M:%S')}
"""

        if violations:
            text += '\n⚠️ <b>تجاوزهای دلتا:</b>\n'
            for v in violations[:10]:
                name = html.escape(v.full_name or '') or v.user_uuid[:8]
                text += f'• {name}: +{v.used_traffic_gb:.1f} گیگابایت\n'
            if len(violations) > 10:
                text += f'... و {len(violations) - 10} مورد دیگر\n'
            text += '\n📨 اعلان‌ها ارسال شدند (با احتساب cooldown)'
        else:
            text += '\n✅ تجاوزی یافت نشد'

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🔄 تکرار', callback_data='admin_mon_traffic_check')],
                [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error checking traffic', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data.startswith('admin_mon_logs'))
@admin_required
async def monitoring_logs_callback(callback: CallbackQuery):
    try:
        page = 1
        if '_page_' in callback.data:
            page = int(callback.data.split('_page_')[1])

        async with AsyncSessionLocal() as db:
            all_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)

            if not all_logs:
                text = '📋 <b>لاگ‌های مانیتورینگ خالی است</b>\n\nسیستم هنوز بررسی‌ای انجام نداده است.'
                keyboard = get_monitoring_logs_back_keyboard()
                await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                return

            per_page = 8
            paginated_logs = paginate_list(all_logs, page=page, per_page=per_page)

            text = f'📋 <b>لاگ‌های مانیتورینگ</b> (صفحه {page}/{paginated_logs.total_pages})\n\n'

            for log in paginated_logs.items:
                icon = '✅' if log['is_success'] else '❌'
                time_str = log['created_at'].strftime('%m-%d %H:%M')
                event_type = log['event_type'].replace('_', ' ').title()

                message = log['message']
                if len(message) > 45:
                    message = message[:45] + '...'

                text += f'{icon} <code>{time_str}</code> {event_type}\n'
                text += f'   📄 {message}\n\n'

            total_success = sum(1 for log in all_logs if log['is_success'])
            total_failed = len(all_logs) - total_success
            success_rate = round(total_success / len(all_logs) * 100, 1) if all_logs else 0

            text += '📊 <b>آمار کلی:</b>\n'
            text += f'• کل رویدادها: {len(all_logs)}\n'
            text += f'• موفق: {total_success}\n'
            text += f'• خطا: {total_failed}\n'
            text += f'• نرخ موفقیت: {success_rate}%'

            keyboard = get_monitoring_logs_keyboard(page, paginated_logs.total_pages)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error getting logs', error=e)
        await callback.answer('❌ خطا در دریافت لاگ‌ها', show_alert=True)


@router.callback_query(F.data == 'admin_mon_clear_logs')
@admin_required
async def clear_logs_callback(callback: CallbackQuery):
    try:
        async with AsyncSessionLocal() as db:
            deleted_count = await monitoring_service.cleanup_old_logs(db, days=0)
            await db.commit()

            if deleted_count > 0:
                await callback.answer(f'🗑️ {deleted_count} رکورد لاگ حذف شد')
            else:
                await callback.answer('ℹ️ لاگ‌ها قبلاً خالی بودند')

            await monitoring_logs_callback(callback)

    except Exception as e:
        logger.error('Error clearing logs', error=e)
        await callback.answer(f'❌ خطای پاکسازی: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_test_notifications')
@admin_required
async def test_notifications_callback(callback: CallbackQuery):
    try:
        test_message = f"""
🧪 <b>پیام تست سیستم مانیتورینگ</b>

این یک پیام آزمایشی برای بررسی عملکرد سیستم اعلان است.

📊 <b>وضعیت سیستم:</b>
• مانیتورینگ: {'🟢 در حال اجرا' if monitoring_service.is_running else '🔴 متوقف'}
• اعلان‌ها: {'🟢 فعال' if settings.ENABLE_NOTIFICATIONS else '🔴 غیرفعال'}
• زمان تست: {datetime.now(UTC).strftime('%H:%M:%S %d.%m.%Y')}

✅ اگر این پیام را دریافت کردید، سیستم اعلان به درستی کار می‌کند!
"""

        await callback.bot.send_message(callback.from_user.id, test_message, parse_mode='HTML')

        await callback.answer('✅ پیام تست ارسال شد!')

    except Exception as e:
        logger.error('Error sending test notification', error=e)
        await callback.answer(f'❌ خطای ارسال: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_statistics')
@admin_required
async def monitoring_statistics_callback(callback: CallbackQuery):
    try:
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)

            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]

            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            text = f"""
📊 <b>آمار مانیتورینگ</b>

📱 <b>اشتراک‌ها:</b>
• کل: {sub_stats['total_subscriptions']}
• فعال: {sub_stats['active_subscriptions']}
• آزمایشی: {sub_stats['trial_subscriptions']}
• پولی: {sub_stats['paid_subscriptions']}

📈 <b>امروز:</b>
• عملیات موفق: {mon_status['stats_24h']['successful']}
• خطاها: {mon_status['stats_24h']['failed']}
• نرخ موفقیت: {mon_status['stats_24h']['success_rate']}%

📊 <b>هفته گذشته:</b>
• کل رویدادها: {len(week_logs)}
• موفق: {week_success}
• خطا: {week_errors}
• نرخ موفقیت: {round(week_success / len(week_logs) * 100, 1) if week_logs else 0}%

🔧 <b>سیستم:</b>
• فاصله زمانی: {settings.MONITORING_INTERVAL} دقیقه
• اعلان‌ها: {'🟢 فعال' if getattr(settings, 'ENABLE_NOTIFICATIONS', True) else '🔴 غیرفعال'}
• پرداخت خودکار: {', '.join(map(str, settings.get_autopay_warning_days()))} روز
"""

            # Добавляем информацию о чеках NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)
                pending_count = nalogo_status.get('pending_verification_count', 0)
                pending_amount = nalogo_status.get('pending_verification_amount', 0)

                nalogo_section = f"""
🧾 <b>فاکتورهای NaloGO:</b>
• سرویس: {'🟢 در حال اجرا' if running else '🔴 متوقف'}
• در صف: {queue_len} فاکتور"""
                if queue_len > 0:
                    nalogo_section += f'\n• مبلغ: {total_amount:,.2f} ₽'
                if pending_count > 0:
                    nalogo_section += f'\n⚠️ <b>نیاز به تأیید: {pending_count} ({pending_amount:,.2f} ₽)</b>'
                text += nalogo_section

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = []
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=f'🧾 ارسال ({nalogo_status["queue_length"]})',
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                pending_count = nalogo_status.get('pending_verification_count', 0)
                if pending_count > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=f'⚠️ تأیید ({pending_count})', callback_data='admin_mon_nalogo_pending'
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(text='📊 تطبیق فاکتورها', callback_data='admin_mon_receipts_missing')
                )
                buttons.append(nalogo_buttons)

            buttons.append([InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error getting statistics', error=e)
        await callback.answer(f'❌ خطا در دریافت آمار: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_nalogo_force_process')
@admin_required
async def nalogo_force_process_callback(callback: CallbackQuery):
    """Принудительная отправка чеков из очереди."""
    try:
        await callback.answer('🔄 در حال پردازش صف فاکتورها...', show_alert=False)

        result = await nalogo_queue_service.force_process()

        if 'error' in result:
            await callback.answer(f'❌ {result["error"]}', show_alert=True)
            return

        result.get('message', 'Готово')
        processed = result.get('processed', 0)
        remaining = result.get('remaining', 0)

        if processed > 0:
            text = f'✅ پردازش شد: {processed} فاکتور'
            if remaining > 0:
                text += f'\n⏳ باقی‌مانده در صف: {remaining}'
        elif remaining > 0:
            text = f'⚠️ سرویس nalog.ru در دسترس نیست\n⏳ در صف: {remaining} فاکتور'
        else:
            text = '📭 صف خالی است'

        await callback.answer(text, show_alert=True)

        # Обновляем страницу статистики
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        # Перезагружаем статистику
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)
            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]
            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            stats_text = f"""
📊 <b>آمار مانیتورینگ</b>

📱 <b>اشتراک‌ها:</b>
• کل: {sub_stats['total_subscriptions']}
• فعال: {sub_stats['active_subscriptions']}
• آزمایشی: {sub_stats['trial_subscriptions']}
• پولی: {sub_stats['paid_subscriptions']}

📈 <b>امروز:</b>
• عملیات موفق: {mon_status['stats_24h']['successful']}
• خطاها: {mon_status['stats_24h']['failed']}
• نرخ موفقیت: {mon_status['stats_24h']['success_rate']}%

📊 <b>هفته گذشته:</b>
• کل رویدادها: {len(week_logs)}
• موفق: {week_success}
• خطا: {week_errors}
• نرخ موفقیت: {round(week_success / len(week_logs) * 100, 1) if week_logs else 0}%

🔧 <b>سیستم:</b>
• فاصله زمانی: {settings.MONITORING_INTERVAL} دقیقه
• اعلان‌ها: {'🟢 فعال' if getattr(settings, 'ENABLE_NOTIFICATIONS', True) else '🔴 غیرفعال'}
• پرداخت خودکار: {', '.join(map(str, settings.get_autopay_warning_days()))} روز
"""

            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)

                nalogo_section = f"""
🧾 <b>فاکتورهای NaloGO:</b>
• سرویس: {'🟢 در حال اجرا' if running else '🔴 متوقف'}
• در صف: {queue_len} فاکتور"""
                if queue_len > 0:
                    nalogo_section += f'\n• مبلغ: {total_amount:,.2f} ₽'
                stats_text += nalogo_section

            buttons = []
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=f'🧾 ارسال ({nalogo_status["queue_length"]})',
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(text='📊 تطبیق فاکتورها', callback_data='admin_mon_receipts_missing')
                )
                buttons.append(nalogo_buttons)

            buttons.append([InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(stats_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error in forced processing of receipts', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_nalogo_pending')
@admin_required
async def nalogo_pending_callback(callback: CallbackQuery):
    """Просмотр чеков ожидающих ручной проверки."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()

        if not receipts:
            await callback.answer('✅ فاکتوری برای بررسی وجود ندارد', show_alert=True)
            return

        text = f'⚠️ <b>فاکتورهای نیازمند بررسی: {len(receipts)}</b>\n\n'
        text += 'لطفاً در lknpd.nalog.ru بررسی کنید که آیا این فاکتورها ایجاد شده‌اند.\n\n'

        buttons = []
        for i, receipt in enumerate(receipts[:10], 1):
            payment_id = receipt.get('payment_id', 'unknown')
            amount = receipt.get('amount', 0)
            created_at = receipt.get('created_at', '')[:16].replace('T', ' ')
            error = receipt.get('error', '')[:50]

            text += f'<b>{i}. {amount:,.2f} ₽</b>\n'
            text += f'   📅 {created_at}\n'
            text += f'   🆔 <code>{payment_id[:20]}...</code>\n'
            if error:
                text += f'   ❌ {error}\n'
            text += '\n'

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f'✅ ایجاد شده ({i})', callback_data=f'admin_nalogo_verified:{payment_id[:30]}'
                    ),
                    InlineKeyboardButton(
                        text=f'🔄 ارسال مجدد ({i})', callback_data=f'admin_nalogo_retry:{payment_id[:30]}'
                    ),
                ]
            )

        if len(receipts) > 10:
            text += f'\n... و {len(receipts) - 10} فاکتور دیگر'

        buttons.append(
            [InlineKeyboardButton(text='🗑 پاکسازی همه (تأیید شده)', callback_data='admin_nalogo_clear_pending')]
        )
        buttons.append([InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error viewing verification queue', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data.startswith('admin_nalogo_verified:'))
@admin_required
async def nalogo_mark_verified_callback(callback: CallbackQuery):
    """Пометить чек как созданный в налоговой."""
    try:
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        # Помечаем как проверенный (чек был создан)
        removed = await nalogo_service.mark_pending_as_verified(payment_id, receipt_uuid=None, was_created=True)

        if removed:
            await callback.answer('✅ فاکتور به عنوان ایجاد شده علامت‌گذاری شد', show_alert=True)
            await nalogo_pending_callback(callback)
        else:
            await callback.answer('❌ فاکتور یافت نشد', show_alert=True)

    except Exception as e:
        logger.error('Error marking receipt as verified', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data.startswith('admin_nalogo_retry:'))
@admin_required
async def nalogo_retry_callback(callback: CallbackQuery):
    """Повторно отправить чек в налоговую."""
    try:
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        await callback.answer('🔄 در حال ارسال فاکتور...', show_alert=False)

        receipt_uuid = await nalogo_service.retry_pending_receipt(payment_id)

        if receipt_uuid:
            await callback.answer(f'✅ فاکتور ایجاد شد: {receipt_uuid}', show_alert=True)
            await nalogo_pending_callback(callback)
        else:
            await callback.answer('❌ ایجاد فاکتور ممکن نشد', show_alert=True)

    except Exception as e:
        logger.error('Error retrying receipt submission', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_nalogo_clear_pending')
@admin_required
async def nalogo_clear_pending_callback(callback: CallbackQuery):
    """Очистить всю очередь проверки."""
    try:
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        count = await nalogo_service.clear_pending_verification()

        await callback.answer(f'✅ پاکسازی شد: {count} فاکتور', show_alert=True)
        await callback.message.edit_text(
            '✅ صف بررسی پاکسازی شد',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')]]
            ),
        )

    except Exception as e:
        logger.error('Error clearing verification queue', error=e)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_receipts_missing')
@admin_required
async def receipts_missing_callback(callback: CallbackQuery):
    """Сверка чеков по логам."""
    # Напрямую вызываем сверку по логам
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_receipts_link_old')
@admin_required
async def receipts_link_old_callback(callback: CallbackQuery):
    """Привязать старые чеки из NaloGO к транзакциям по сумме и дате."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from sqlalchemy import and_, select

        from app.database.models import PaymentMethod, Transaction, TransactionType
        from app.services.nalogo_service import NaloGoService

        await callback.answer('🔄 در حال بارگذاری فاکتورها از NaloGO...', show_alert=False)

        TRACKING_START_DATE = datetime(2024, 12, 29, 0, 0, 0, tzinfo=UTC)

        async with AsyncSessionLocal() as db:
            # Получаем старые транзакции без чеков
            query = (
                select(Transaction)
                .where(
                    and_(
                        Transaction.type == TransactionType.DEPOSIT.value,
                        Transaction.payment_method == PaymentMethod.YOOKASSA.value,
                        Transaction.receipt_uuid.is_(None),
                        Transaction.is_completed == True,
                        Transaction.created_at < TRACKING_START_DATE,
                    )
                )
                .order_by(Transaction.created_at.desc())
            )

            result = await db.execute(query)
            transactions = result.scalars().all()

            if not transactions:
                await callback.answer('✅ تراکنش قدیمی برای پیوند وجود ندارد', show_alert=True)
                return

            # Получаем чеки из NaloGO за последние 60 дней
            nalogo_service = NaloGoService()
            to_date = date.today()
            from_date = to_date - timedelta(days=60)

            incomes = await nalogo_service.get_incomes(
                from_date=from_date,
                to_date=to_date,
                limit=500,
            )

            if not incomes:
                await callback.answer('❌ دریافت فاکتورها از NaloGO ممکن نشد', show_alert=True)
                return

            # Создаём словарь чеков по сумме для быстрого поиска
            # Ключ: сумма в копейках, значение: список чеков
            incomes_by_amount = {}
            for income in incomes:
                amount = float(income.get('totalAmount', income.get('amount', 0)))
                amount_kopeks = int(amount * 100)
                if amount_kopeks not in incomes_by_amount:
                    incomes_by_amount[amount_kopeks] = []
                incomes_by_amount[amount_kopeks].append(income)

            linked = 0
            for t in transactions:
                if t.amount_kopeks in incomes_by_amount:
                    matching_incomes = incomes_by_amount[t.amount_kopeks]
                    if matching_incomes:
                        # Берём первый подходящий чек
                        income = matching_incomes.pop(0)
                        receipt_uuid = income.get('approvedReceiptUuid', income.get('receiptUuid'))
                        if receipt_uuid:
                            t.receipt_uuid = receipt_uuid
                            # Парсим дату чека
                            operation_time = income.get('operationTime')
                            if operation_time:
                                try:
                                    from dateutil.parser import isoparse

                                    parsed_time = isoparse(operation_time)
                                    t.receipt_created_at = (
                                        parsed_time if parsed_time.tzinfo else parsed_time.replace(tzinfo=UTC)
                                    )
                                except Exception:
                                    t.receipt_created_at = datetime.now(UTC)
                            linked += 1

            if linked > 0:
                await db.commit()

            text = '🔗 <b>پیوند کامل شد</b>\n\n'
            text += f'کل تراکنش‌ها: {len(transactions)}\n'
            text += f'فاکتورها در NaloGO: {len(incomes)}\n'
            text += f'پیوند داده شده: <b>{linked}</b>\n'
            text += f'پیوند ناموفق: {len(transactions) - linked}'

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')],
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error linking old receipts', error=e, exc_info=True)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_receipts_reconcile')
@admin_required
async def receipts_reconcile_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Меню выбора периода сверки."""

    # Очищаем состояние на случай если остался ввод даты
    await state.clear()

    # Сразу показываем сверку по логам
    await _do_reconcile_logs(callback)


async def _do_reconcile_logs(callback: CallbackQuery):
    """Внутренняя функция сверки по логам."""
    try:
        import re
        from collections import defaultdict
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer('🔄 در حال تجزیه لاگ‌های پرداخت...', show_alert=False)

        # Путь к файлу логов платежей (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            try:
                await callback.message.edit_text(
                    '❌ <b>فایل لاگ یافت نشد</b>\n\n'
                    f'مسیر: <code>{payments_log}</code>\n\n'
                    '<i>لاگ‌ها پس از اولین پرداخت موفق ظاهر می‌شوند.</i>',
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text='🔄 بروزرسانی', callback_data='admin_mon_reconcile_logs')],
                            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')],
                        ]
                    ),
                )
            except TelegramBadRequest:
                pass  # Сообщение не изменилось
            return

        # Паттерны для парсинга логов
        # Успешный платёж: "Успешно обработан платеж YooKassa 30e3c6fc-000f-5001-9000-1a9c8b242396: пользователь 1046 пополнил баланс на 200.0₽"
        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Успешно обработан платеж YooKassa ([a-f0-9-]+).*на ([\d.]+)₽'
        )
        # Чек создан: "Чек NaloGO создан для платежа 30e3c6fc-000f-5001-9000-1a9c8b242396: 243udsqtik"
        receipt_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Чек NaloGO создан для платежа ([a-f0-9-]+): (\w+)'
        )

        # Читаем и парсим логи
        payments = {}  # payment_id -> {date, amount}
        receipts = {}  # payment_id -> {date, receipt_uuid}

        try:
            with open(payments_log, encoding='utf-8') as f:
                for line in f:
                    # Проверяем платежи
                    match = payment_pattern.search(line)
                    if match:
                        date_str, payment_id, amount = match.groups()
                        payments[payment_id] = {'date': date_str, 'amount': float(amount)}
                        continue

                    # Проверяем чеки
                    match = receipt_pattern.search(line)
                    if match:
                        date_str, payment_id, receipt_uuid = match.groups()
                        receipts[payment_id] = {'date': date_str, 'receipt_uuid': receipt_uuid}
        except Exception as e:
            logger.error('Error reading logs', error=e)
            await callback.message.edit_text(
                f'❌ <b>خطا در خواندن لاگ‌ها</b>\n\n{e!s}',
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')]]
                ),
            )
            return

        # Находим платежи без чеков
        payments_without_receipts = []
        for payment_id, payment_data in payments.items():
            if payment_id not in receipts:
                payments_without_receipts.append(
                    {'payment_id': payment_id, 'date': payment_data['date'], 'amount': payment_data['amount']}
                )

        # Группируем по датам
        by_date = defaultdict(list)
        for p in payments_without_receipts:
            by_date[p['date']].append(p)

        # Формируем отчёт
        total_payments = len(payments)
        total_receipts = len(receipts)
        missing_count = len(payments_without_receipts)
        missing_amount = sum(p['amount'] for p in payments_without_receipts)

        text = '📋 <b>تطبیق بر اساس لاگ‌ها</b>\n\n'
        text += f'📦 <b>کل پرداخت‌ها:</b> {total_payments}\n'
        text += f'🧾 <b>فاکتورهای ایجاد شده:</b> {total_receipts}\n\n'

        if missing_count == 0:
            text += '✅ <b>همه پرداخت‌ها دارای فاکتور هستند!</b>'
        else:
            text += f'⚠️ <b>بدون فاکتور:</b> {missing_count} پرداخت به مبلغ {missing_amount:,.2f} ₽\n\n'

            sorted_dates = sorted(by_date.keys(), reverse=True)
            for date_str in sorted_dates[:7]:
                date_payments = by_date[date_str]
                date_amount = sum(p['amount'] for p in date_payments)
                text += f'• <b>{date_str}:</b> {len(date_payments)} مورد به مبلغ {date_amount:,.2f} ₽\n'

            if len(sorted_dates) > 7:
                text += f'\n<i>...و {len(sorted_dates) - 7} روز دیگر</i>'

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🔄 بروزرسانی', callback_data='admin_mon_reconcile_logs')],
                [InlineKeyboardButton(text='📄 جزئیات', callback_data='admin_mon_reconcile_logs_details')],
                [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_statistics')],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass  # Сообщение не изменилось

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Error reconciling by logs', error=e, exc_info=True)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


@router.callback_query(F.data == 'admin_mon_reconcile_logs')
@admin_required
async def receipts_reconcile_logs_refresh_callback(callback: CallbackQuery):
    """Обновить сверку по логам."""
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_reconcile_logs_details')
@admin_required
async def receipts_reconcile_logs_details_callback(callback: CallbackQuery):
    """Детальный список платежей без чеков."""
    try:
        import re
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer('🔄 در حال بارگذاری جزئیات...', show_alert=False)

        # Путь к логам (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            await callback.answer('❌ فایل لاگ یافت نشد', show_alert=True)
            return

        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*Успешно обработан платеж YooKassa ([a-f0-9-]+).*пользователь (\d+).*на ([\d.]+)₽'
        )
        receipt_pattern = re.compile(r'Чек NaloGO создан для платежа ([a-f0-9-]+)')

        payments = {}
        receipts = set()

        with open(payments_log, encoding='utf-8') as f:
            for line in f:
                match = payment_pattern.search(line)
                if match:
                    date_str, time_str, payment_id, user_id, amount = match.groups()
                    payments[payment_id] = {
                        'date': date_str,
                        'time': time_str,
                        'user_id': user_id,
                        'amount': float(amount),
                    }
                    continue

                match = receipt_pattern.search(line)
                if match:
                    receipts.add(match.group(1))

        # Платежи без чеков
        missing = []
        for payment_id, data in payments.items():
            if payment_id not in receipts:
                missing.append({'payment_id': payment_id, **data})

        # Сортируем по дате (новые сверху)
        missing.sort(key=lambda x: (x['date'], x['time']), reverse=True)

        if not missing:
            text = '✅ <b>همه پرداخت‌ها دارای فاکتور هستند!</b>'
        else:
            text = f'📄 <b>پرداخت‌های بدون فاکتور ({len(missing)} مورد)</b>\n\n'

            for p in missing[:20]:
                text += (
                    f'• <b>{p["date"]} {p["time"]}</b>\n'
                    f'  User: {p["user_id"]} | {p["amount"]:.0f}₽\n'
                    f'  <code>{p["payment_id"][:18]}...</code>\n\n'
                )

            if len(missing) > 20:
                text += f'<i>...و {len(missing) - 20} پرداخت دیگر</i>'

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_mon_reconcile_logs')],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Error getting details', error=e, exc_info=True)
        await callback.answer(f'❌ خطا: {e!s}', show_alert=True)


def get_monitoring_logs_keyboard(current_page: int, total_pages: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = []

    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_mon_logs_page_{current_page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_mon_logs_page_{current_page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(text='🔄 بروزرسانی', callback_data='admin_mon_logs'),
                InlineKeyboardButton(text='🗑️ پاکسازی', callback_data='admin_mon_clear_logs'),
            ],
            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_logs_back_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='🔄 بروزرسانی', callback_data='admin_mon_logs'),
                InlineKeyboardButton(text='🔍 فیلترها', callback_data='admin_mon_logs_filters'),
            ],
            [InlineKeyboardButton(text='🗑️ پاکسازی لاگ‌ها', callback_data='admin_mon_clear_logs')],
            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')],
        ]
    )


@router.message(Command('monitoring'))
@admin_required
async def monitoring_command(message: Message):
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            running_status = '🟢 در حال اجرا' if status['is_running'] else '🔴 متوقف'

            text = f"""
🔍 <b>وضعیت سریع مانیتورینگ</b>

📊 <b>وضعیت:</b> {running_status}
📈 <b>رویدادها در ۲۴ ساعت:</b> {status['stats_24h']['total_events']}
✅ <b>نرخ موفقیت:</b> {status['stats_24h']['success_rate']}%

برای مدیریت تفصیلی از پنل ادمین استفاده کنید.
"""

            await message.answer(text, parse_mode='HTML')

    except Exception as e:
        logger.error('Error in /monitoring command', error=e)
        await message.answer(f'❌ خطا: {e!s}')


@router.message(AdminStates.editing_notification_value)
async def process_notification_value_input(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data:
        await state.clear()
        await message.answer('ℹ️ اطلاعات از دست رفت، لطفاً دوباره از منوی تنظیمات امتحان کنید.')
        return

    raw_value = (message.text or '').strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ لطفاً یک عدد صحیح وارد کنید.'))
        return

    key = data.get('notification_setting_key')
    field = data.get('notification_setting_field')
    language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    if (key == 'expired_second_wave' and field == 'percent') or (key == 'expired_third_wave' and field == 'percent'):
        if value < 0 or value > 100:
            await message.answer('❌ درصد تخفیف باید بین ۰ تا ۱۰۰ باشد.')
            return
    elif (key == 'expired_second_wave' and field == 'hours') or (key == 'expired_third_wave' and field == 'hours'):
        if value < 1 or value > 168:
            await message.answer('❌ تعداد ساعت‌ها باید بین ۱ تا ۱۶۸ باشد.')
            return
    elif key == 'expired_third_wave' and field == 'trigger':
        if value < 2:
            await message.answer('❌ تعداد روزها باید حداقل ۲ باشد.')
            return

    success = False
    if key == 'expired_second_wave' and field == 'percent':
        success = NotificationSettingsService.set_second_wave_discount_percent(value)
    elif key == 'expired_second_wave' and field == 'hours':
        success = NotificationSettingsService.set_second_wave_valid_hours(value)
    elif key == 'expired_third_wave' and field == 'percent':
        success = NotificationSettingsService.set_third_wave_discount_percent(value)
    elif key == 'expired_third_wave' and field == 'hours':
        success = NotificationSettingsService.set_third_wave_valid_hours(value)
    elif key == 'expired_third_wave' and field == 'trigger':
        success = NotificationSettingsService.set_third_wave_trigger_days(value)

    if not success:
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ مقدار نامعتبر است، لطفاً دوباره امتحان کنید.'))
        return

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.get('BACK', '⬅️ بازگشت'),
                    callback_data='admin_mon_notify_settings',
                )
            ]
        ]
    )

    await message.answer(
        texts.get('NOTIFICATION_VALUE_UPDATED', '✅ تنظیمات به‌روز شد.'),
        reply_markup=back_keyboard,
    )

    chat_id = data.get('settings_message_chat')
    message_id = data.get('settings_message_id')
    business_connection_id = data.get('settings_business_connection_id')
    if chat_id and message_id:
        await _render_notification_settings_for_state(
            message.bot,
            chat_id,
            message_id,
            language,
            business_connection_id=business_connection_id,
        )

    await state.clear()


# ============== Настройки мониторинга трафика ==============


def _format_traffic_toggle(enabled: bool) -> str:
    return '🟢 فعال' if enabled else '🔴 غیرفعال'


def _build_traffic_settings_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру настроек мониторинга трафика."""
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_interval = settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES
    fast_threshold = settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB
    daily_time = settings.TRAFFIC_DAILY_CHECK_TIME
    daily_threshold = settings.TRAFFIC_DAILY_THRESHOLD_GB
    cooldown = settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f'{_format_traffic_toggle(fast_enabled)} بررسی سریع',
                    callback_data='admin_traffic_toggle_fast',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'⏱ فاصله زمانی: {fast_interval} دقیقه', callback_data='admin_traffic_edit_fast_interval'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'📊 آستانه دلتا: {fast_threshold} گیگابایت', callback_data='admin_traffic_edit_fast_threshold'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'{_format_traffic_toggle(daily_enabled)} بررسی روزانه',
                    callback_data='admin_traffic_toggle_daily',
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'🕐 زمان بررسی: {daily_time}', callback_data='admin_traffic_edit_daily_time'
                )
            ],
            [
                InlineKeyboardButton(
                    text=f'📈 آستانه روزانه: {daily_threshold} گیگابایت', callback_data='admin_traffic_edit_daily_threshold'
                )
            ],
            [InlineKeyboardButton(text=f'⏳ Cooldown: {cooldown} دقیقه', callback_data='admin_traffic_edit_cooldown')],
            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_monitoring')],
        ]
    )


def _build_traffic_settings_text() -> str:
    """Строит текст настроек мониторинга трафика."""
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_status = _format_traffic_toggle(fast_enabled)
    daily_status = _format_traffic_toggle(daily_enabled)

    text = (
        '⚙️ <b>تنظیمات مانیتورینگ ترافیک</b>\n\n'
        f'<b>بررسی سریع:</b> {fast_status}\n'
        f'• فاصله زمانی: {settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES} دقیقه\n'
        f'• آستانه دلتا: {settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB} گیگابایت\n\n'
        f'<b>بررسی روزانه:</b> {daily_status}\n'
        f'• زمان: {settings.TRAFFIC_DAILY_CHECK_TIME} UTC\n'
        f'• آستانه: {settings.TRAFFIC_DAILY_THRESHOLD_GB} گیگابایت\n\n'
        f'<b>عمومی:</b>\n'
        f'• Cooldown اعلان: {settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES} دقیقه\n'
    )

    monitored_nodes = settings.get_traffic_monitored_nodes()
    ignored_nodes = settings.get_traffic_ignored_nodes()
    excluded_uuids = settings.get_traffic_excluded_user_uuids()

    if monitored_nodes:
        text += f'• فقط نودها: {len(monitored_nodes)} نود\n'
    if ignored_nodes:
        text += f'• نادیده گرفته: {len(ignored_nodes)} نود\n'
    if excluded_uuids:
        text += f'• کاربران استثنا: {len(excluded_uuids)}\n'

    return text


@router.callback_query(F.data == 'admin_mon_traffic_settings')
@admin_required
async def admin_traffic_settings(callback: CallbackQuery):
    """Показывает настройки мониторинга трафика."""
    try:
        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error('Error displaying traffic settings', error=e)
        await callback.answer('❌ خطا در بارگذاری تنظیمات', show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_fast')
@admin_required
async def toggle_fast_check(callback: CallbackQuery):
    """Переключает быструю проверку трафика."""
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_FAST_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_FAST_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer('✅ فعال شد' if new_value else '⏸️ غیرفعال شد')

        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error toggling fast check', error=e)
        await callback.answer('❌ خطا', show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_daily')
@admin_required
async def toggle_daily_check(callback: CallbackQuery):
    """Переключает суточную проверку трафика."""
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_DAILY_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_DAILY_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer('✅ فعال شد' if new_value else '⏸️ غیرفعال شد')

        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Error toggling daily check', error=e)
        await callback.answer('❌ خطا', show_alert=True)


@router.callback_query(F.data == 'admin_traffic_edit_fast_interval')
@admin_required
async def edit_fast_interval(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование интервала быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_INTERVAL_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer('⏱ فاصله زمانی بررسی سریع را به دقیقه وارد کنید (حداقل ۱):')


@router.callback_query(F.data == 'admin_traffic_edit_fast_threshold')
@admin_required
async def edit_fast_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование порога быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer('📊 آستانه دلتای ترافیک را به گیگابایت وارد کنید (مثال: 5.0):')


@router.callback_query(F.data == 'admin_traffic_edit_daily_time')
@admin_required
async def edit_daily_time(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование времени суточной проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_CHECK_TIME',
        traffic_setting_type='time',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        '🕐 زمان بررسی روزانه را به فرمت HH:MM (UTC) وارد کنید:\nمثال: 00:00, 03:00, 12:30'
    )


@router.callback_query(F.data == 'admin_traffic_edit_daily_threshold')
@admin_required
async def edit_daily_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование суточного порога."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer('📈 آستانه روزانه ترافیک را به گیگابایت وارد کنید (مثال: 50.0):')


@router.callback_query(F.data == 'admin_traffic_edit_cooldown')
@admin_required
async def edit_cooldown(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование кулдауна уведомлений."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer('⏳ مدت زمان cooldown اعلان را به دقیقه وارد کنید (حداقل ۱):')


@router.message(AdminStates.editing_traffic_setting)
async def process_traffic_setting_input(message: Message, state: FSMContext):
    """Обрабатывает ввод настройки мониторинга трафика."""
    from app.services.system_settings_service import BotConfigurationService

    data = await state.get_data()
    if not data:
        await state.clear()
        await message.answer('ℹ️ اطلاعات از دست رفت، لطفاً دوباره از منوی تنظیمات امتحان کنید.')
        return

    raw_value = (message.text or '').strip()
    setting_key = data.get('traffic_setting_key')
    setting_type = data.get('traffic_setting_type')

    try:
        if setting_type == 'int':
            value = int(raw_value)
            if value < 1:
                raise ValueError('مقدار باید >= ۱ باشد')
        elif setting_type == 'float':
            value = float(raw_value.replace(',', '.'))
            if value <= 0:
                raise ValueError('مقدار باید > ۰ باشد')
        elif setting_type == 'time':
            import re

            if not re.match(r'^\d{1,2}:\d{2}$', raw_value):
                raise ValueError('فرمت زمان نادرست است. از HH:MM استفاده کنید')
            parts = raw_value.split(':')
            hours, minutes = int(parts[0]), int(parts[1])
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                raise ValueError('زمان نامعتبر است')
            value = f'{hours:02d}:{minutes:02d}'
        else:
            value = raw_value
    except ValueError as e:
        await message.answer(f'❌ {e!s}')
        return

    try:
        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, setting_key, value)
            await db.commit()

        back_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='⬅️ بازگشت به تنظیمات ترافیک', callback_data='admin_mon_traffic_settings')]
            ]
        )
        await message.answer('✅ تنظیمات ذخیره شد!', reply_markup=back_keyboard)

        chat_id = data.get('settings_message_chat')
        message_id = data.get('settings_message_id')
        if chat_id and message_id:
            try:
                text = _build_traffic_settings_text()
                keyboard = _build_traffic_settings_keyboard()
                await message.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', reply_markup=keyboard
                )
            except Exception:
                pass

    except Exception as e:
        logger.error('Error saving traffic setting', error=e)
        await message.answer(f'❌ خطای ذخیره: {e!s}')

    await state.clear()


def register_handlers(dp):
    dp.include_router(router)
