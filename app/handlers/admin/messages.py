import asyncio
import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_expiring_subscriptions
from app.database.crud.tariff import get_all_tariffs
from app.database.crud.user import get_users_list
from app.database.database import AsyncSessionLocal
from app.database.models import (
    BroadcastHistory,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)
from app.keyboards.admin import (
    BROADCAST_BUTTON_ROWS,
    DEFAULT_BROADCAST_BUTTONS,
    get_admin_messages_keyboard,
    get_broadcast_button_config,
    get_broadcast_button_labels,
    get_broadcast_history_keyboard,
    get_broadcast_media_keyboard,
    get_broadcast_target_keyboard,
    get_custom_criteria_keyboard,
    get_media_confirm_keyboard,
    get_pinned_message_keyboard,
    get_updated_message_buttons_selector_keyboard_with_media,
)
from app.localization.texts import get_texts
from app.services.pinned_message_service import (
    broadcast_pinned_message,
    get_active_pinned_message,
    set_active_pinned_message,
    unpin_active_pinned_message,
)
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.miniapp_buttons import BUTTON_KEY_TO_CABINET_PATH, build_miniapp_or_callback_button


logger = structlog.get_logger(__name__)


async def safe_edit_or_send_text(callback: types.CallbackQuery, text: str, reply_markup=None, parse_mode: str = 'HTML'):
    """
    Safely edits a message or deletes and sends a new one.
    Needed for cases where the current message is media (photo/video)
    that cannot be edited via edit_text.
    """
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if 'there is no text in the message to edit' in str(e):
            # Message is media without text — delete and send a new one
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.bot.send_message(
                chat_id=callback.message.chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        else:
            raise


BUTTON_ROWS = BROADCAST_BUTTON_ROWS
DEFAULT_SELECTED_BUTTONS = DEFAULT_BROADCAST_BUTTONS

CABINET_MINIAPP_BUTTON_KEYS = {
    'balance',
    'referrals',
    'promocode',
    'connect',
    'subscription',
    'support',
}


def get_message_buttons_selector_keyboard(language: str = 'ru') -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard(list(DEFAULT_SELECTED_BUTTONS), language)


def get_updated_message_buttons_selector_keyboard(
    selected_buttons: list, language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    return get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, False, language)


def create_broadcast_keyboard(
    selected_buttons: list,
    language: str = 'ru',
    custom_buttons: list[dict] | None = None,
) -> types.InlineKeyboardMarkup | None:
    selected_buttons = selected_buttons or []
    keyboard: list[list[types.InlineKeyboardButton]] = []
    button_config_map = get_broadcast_button_config(language)

    for row in BUTTON_ROWS:
        row_buttons: list[types.InlineKeyboardButton] = []
        for button_key in row:
            if button_key not in selected_buttons:
                continue
            button_config = button_config_map[button_key]
            if settings.is_cabinet_mode() and button_key in CABINET_MINIAPP_BUTTON_KEYS:
                row_buttons.append(
                    build_miniapp_or_callback_button(
                        text=button_config['text'],
                        callback_data=button_config['callback'],
                        cabinet_path=BUTTON_KEY_TO_CABINET_PATH.get(button_key, ''),
                    )
                )
            else:
                row_buttons.append(
                    types.InlineKeyboardButton(text=button_config['text'], callback_data=button_config['callback'])
                )
        if row_buttons:
            keyboard.append(row_buttons)

    # Append custom buttons (each on its own row)
    if custom_buttons:
        for btn in custom_buttons:
            label = btn.get('label', '')
            action_type = btn.get('action_type', 'callback')
            action_value = btn.get('action_value', '')
            if not label or not action_value:
                continue
            if action_type == 'url':
                keyboard.append([types.InlineKeyboardButton(text=label, url=action_value)])
            else:
                # callback type
                keyboard.append([types.InlineKeyboardButton(text=label, callback_data=action_value)])

    if not keyboard:
        return None

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _persist_broadcast_result(
    broadcast_id: int,
    sent_count: int,
    failed_count: int,
    status: str,
    blocked_count: int = 0,
) -> None:
    """
    Persists broadcast results in a NEW session.

    IMPORTANT: We use a fresh session instead of the passed one, because over the
    course of a long broadcast (minutes/hours) the original connection is guaranteed
    to close due to PostgreSQL's idle_in_transaction_session_timeout.

    Args:
        broadcast_id: ID of the BroadcastHistory record (not an ORM object!)
        sent_count: Number of successfully sent messages
        failed_count: Number of failed sends
        status: Final broadcast status ('completed', 'partial', 'failed')
        blocked_count: Number of users who blocked the bot
    """
    completed_at = datetime.now(UTC)
    max_retries = 3
    retry_delay = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            async with AsyncSessionLocal() as session:
                broadcast_history = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast_history:
                    logger.critical(
                        'Failed to find BroadcastHistory record # to save results', broadcast_id=broadcast_id
                    )
                    return

                broadcast_history.sent_count = sent_count
                broadcast_history.failed_count = failed_count
                broadcast_history.blocked_count = blocked_count
                broadcast_history.status = status
                broadcast_history.completed_at = completed_at
                await session.commit()

                logger.info(
                    'Broadcast results saved (id sent failed blocked status=)',
                    broadcast_id=broadcast_id,
                    sent_count=sent_count,
                    failed_count=failed_count,
                    blocked_count=blocked_count,
                    status=status,
                )
                return

        except InterfaceError as error:
            logger.warning(
                'Connection error while saving broadcast results (attempt /)',
                attempt=attempt,
                max_retries=max_retries,
                error=error,
            )
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.critical(
                    'Failed to save broadcast results after retries (id=)',
                    max_retries=max_retries,
                    broadcast_id=broadcast_id,
                )

        except Exception as error:
            logger.critical(
                'Unexpected error while saving broadcast results (id=)',
                broadcast_id=broadcast_id,
                exc_info=error,
            )
            return


@admin_required
@error_handler
async def show_messages_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
📨 <b>مدیریت پیام‌های گروهی</b>

نوع ارسال را انتخاب کنید:

- <b>به همه کاربران</b> - ارسال به همه کاربران فعال
- <b>بر اساس اشتراک</b> - فیلتر بر اساس نوع اشتراک
- <b>بر اساس معیار</b> - فیلترهای سفارشی
- <b>تاریخچه</b> - مشاهده ارسال‌های قبلی

⚠️ در ارسال‌های گروهی احتیاط کنید!
"""

    await safe_edit_or_send_text(
        callback, text, reply_markup=get_admin_messages_keyboard(db_user.language), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def show_pinned_message_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    pinned_message = await get_active_pinned_message(db)

    if pinned_message:
        content_preview = html.escape(pinned_message.content or '')
        last_updated = pinned_message.updated_at or pinned_message.created_at
        timestamp_text = last_updated.strftime('%d.%m.%Y %H:%M') if last_updated else '—'
        media_line = ''
        if pinned_message.media_type:
            media_label = 'عکس' if pinned_message.media_type == 'photo' else 'ویدیو'
            media_line = f'📎 رسانه: {media_label}\n'
        position_line = '⬆️ ارسال قبل از منو' if pinned_message.send_before_menu else '⬇️ ارسال بعد از منو'
        start_mode_line = (
            '🔁 در هر /start' if pinned_message.send_on_every_start else '🚫 فقط یک بار و هنگام به‌روزرسانی'
        )
        body = (
            '📌 <b>پیام سنجاق‌شده</b>\n\n'
            '📝 متن فعلی:\n'
            f'<code>{content_preview}</code>\n\n'
            f'{media_line}'
            f'{position_line}\n'
            f'{start_mode_line}\n'
            f'🕒 به‌روزرسانی: {timestamp_text}'
        )
    else:
        body = (
            '📌 <b>پیام سنجاق‌شده</b>\n\n'
            'پیامی تنظیم نشده است. متن جدیدی ارسال کنید تا برای کاربران ارسال و سنجاق شود.'
        )

    await callback.message.edit_text(
        body,
        reply_markup=get_pinned_message_keyboard(
            db_user.language,
            send_before_menu=getattr(pinned_message, 'send_before_menu', True),
            send_on_every_start=getattr(pinned_message, 'send_on_every_start', True),
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def prompt_pinned_message_update(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    await state.set_state(AdminStates.editing_pinned_message)
    await callback.message.edit_text(
        '✏️ <b>پیام سنجاق‌شده جدید</b>\n\n'
        'متن، عکس یا ویدیویی که باید سنجاق شود را ارسال کنید.\n'
        'ربات آن را برای همه کاربران فعال ارسال می‌کند، پیام قبلی را از سنجاق خارج و پیام جدید را بدون اعلان سنجاق می‌کند.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_pinned_message')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_pinned_message_position(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer('ابتدا پیام سنجاق‌شده را تنظیم کنید', show_alert=True)
        return

    pinned_message.send_before_menu = not pinned_message.send_before_menu
    pinned_message.updated_at = datetime.now(UTC)
    await db.commit()

    await show_pinned_message_menu(callback, db_user, db, state)


@admin_required
@error_handler
async def toggle_pinned_message_start_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer('ابتدا پیام سنجاق‌شده را تنظیم کنید', show_alert=True)
        return

    pinned_message.send_on_every_start = not pinned_message.send_on_every_start
    pinned_message.updated_at = datetime.now(UTC)
    await db.commit()

    await show_pinned_message_menu(callback, db_user, db, state)


@admin_required
@error_handler
async def delete_pinned_message(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        await callback.answer('پیام سنجاق‌شده از قبل موجود نیست', show_alert=True)
        return

    await callback.message.edit_text(
        '🗑️ <b>حذف پیام سنجاق‌شده</b>\n\nصبر کنید تا ربات پیام را از کاربران خارج کند...',
        parse_mode='HTML',
    )

    unpinned_count, failed_count, deleted = await unpin_active_pinned_message(
        callback.bot,
        db,
    )

    if not deleted:
        await callback.message.edit_text(
            '❌ پیام سنجاق‌شده فعالی برای حذف پیدا نشد',
            reply_markup=get_admin_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await state.clear()
        return

    total = unpinned_count + failed_count
    await callback.message.edit_text(
        '✅ <b>پیام سنجاق‌شده حذف شد</b>\n\n'
        f'👥 چت‌های پردازش‌شده: {total}\n'
        f'✅ از سنجاق خارج‌شده: {unpinned_count}\n'
        f'⚠️ خطاها: {failed_count}\n\n'
        'پیام جدید را می‌توانید با دکمه «به‌روزرسانی» تنظیم کنید.',
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def process_pinned_message_update(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    media_type: str | None = None
    media_file_id: str | None = None

    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = 'video'
        media_file_id = message.video.file_id

    pinned_text = message.html_text or message.caption_html or message.text or message.caption or ''

    if not pinned_text and not media_file_id:
        await message.answer(
            texts.t('ADMIN_PINNED_NO_CONTENT', '❌ خواندن متن یا رسانه پیام ممکن نشد، دوباره تلاش کنید.')
        )
        return

    try:
        pinned_message = await set_active_pinned_message(
            db,
            pinned_text,
            db_user.id,
            media_type=media_type,
            media_file_id=media_file_id,
        )
    except ValueError as validation_error:
        await message.answer(f'❌ {validation_error}')
        return

    # Message saved, ask about broadcast
    from app.keyboards.admin import get_pinned_broadcast_confirm_keyboard
    from app.states import AdminStates

    await message.answer(
        texts.t(
            'ADMIN_PINNED_SAVED_ASK_BROADCAST',
            '📌 <b>پیام ذخیره شد!</b>\n\n'
            'نحوه تحویل پیام به کاربران را انتخاب کنید:\n\n'
            '• <b>همین الان ارسال</b> — برای همه کاربران فعال ارسال و سنجاق می‌شود\n'
            '• <b>فقط در /start</b> — کاربران در اجرای بعدی ربات آن را خواهند دید',
        ),
        reply_markup=get_pinned_broadcast_confirm_keyboard(db_user.language, pinned_message.id),
        parse_mode='HTML',
    )
    await state.set_state(AdminStates.confirming_pinned_broadcast)


@admin_required
@error_handler
async def handle_pinned_broadcast_now(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Broadcast the pinned message now to all users."""
    texts = get_texts(db_user.language)

    # Get the message ID from callback_data
    pinned_message_id = int(callback.data.split(':')[1])

    # Get the message from the DB
    from sqlalchemy import select

    from app.database.models import PinnedMessage

    result = await db.execute(select(PinnedMessage).where(PinnedMessage.id == pinned_message_id))
    pinned_message = result.scalar_one_or_none()

    if not pinned_message:
        await callback.answer('❌ پیام پیدا نشد', show_alert=True)
        await state.clear()
        return

    await callback.message.edit_text(
        texts.t('ADMIN_PINNED_SAVING', '📌 پیام ذخیره شد. شروع به ارسال و سنجاق‌کردن برای کاربران...'),
        parse_mode='HTML',
    )

    sent_count, failed_count = await broadcast_pinned_message(
        callback.bot,
        db,
        pinned_message,
    )

    total = sent_count + failed_count
    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_UPDATED',
            '✅ <b>پیام سنجاق‌شده به‌روزرسانی شد</b>\n\n'
            '👥 دریافت‌کنندگان: {total}\n'
            '✅ ارسال‌شده: {sent}\n'
            '⚠️ خطاها: {failed}',
        ).format(total=total, sent=sent_count, failed=failed_count),
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def handle_pinned_broadcast_skip(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Skip the broadcast — users will see it on /start."""
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_PINNED_SAVED_NO_BROADCAST',
            '✅ <b>پیام سنجاق‌شده ذخیره شد</b>\n\n'
            'ارسال انجام نشد. کاربران پیام را در اجرای بعدی /start خواهند دید.',
        ),
        reply_markup=get_admin_messages_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.clear()


@admin_required
@error_handler
async def show_broadcast_targets(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await callback.message.edit_text(
        '🎯 <b>انتخاب مخاطبان هدف</b>\n\nدسته کاربران برای ارسال را انتخاب کنید:',
        reply_markup=get_broadcast_target_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariff_filter(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Shows the list of tariffs for broadcast filtering."""
    tariffs = await get_all_tariffs(db, include_inactive=False)

    if not tariffs:
        await callback.message.edit_text(
            '❌ <b>تعرفه‌ای موجود نیست</b>\n\nدر بخش مدیریت تعرفه‌ها، تعرفه بسازید.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_msg_by_sub')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Retrieve the number of subscribers on each tariff
    tariff_counts = {}
    for tariff in tariffs:
        count_query = select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff.id,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
        result = await db.execute(count_query)
        tariff_counts[tariff.id] = result.scalar() or 0

    buttons = []
    for tariff in tariffs:
        count = tariff_counts.get(tariff.id, 0)
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'{tariff.name} ({count} نفر)', callback_data=f'broadcast_tariff_{tariff.id}'
                )
            ]
        )

    buttons.append([types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_msg_by_sub')])

    await callback.message.edit_text(
        '📦 <b>ارسال بر اساس تعرفه</b>\n\nتعرفه‌ای برای ارسال به کاربران دارای اشتراک فعال در آن تعرفه انتخاب کنید:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_messages_history(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = 1
    if '_page_' in callback.data:
        page = int(callback.data.split('_page_')[1])

    limit = 10
    offset = (page - 1) * limit

    stmt = select(BroadcastHistory).order_by(BroadcastHistory.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    broadcasts = result.scalars().all()

    count_stmt = select(func.count(BroadcastHistory.id))
    count_result = await db.execute(count_stmt)
    total_count = count_result.scalar() or 0
    total_pages = (total_count + limit - 1) // limit

    if not broadcasts:
        text = """
📋 <b>تاریخچه ارسال‌ها</b>

❌ تاریخچه ارسال خالی است.
اولین ارسال را انجام دهید تا اینجا نمایش داده شود.
"""
        keyboard = [[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_messages')]]
    else:
        text = f'📋 <b>تاریخچه ارسال‌ها</b> (صفحه {page}/{total_pages})\n\n'

        for broadcast in broadcasts:
            status_emoji = '✅' if broadcast.status == 'completed' else '❌' if broadcast.status == 'failed' else '⏳'
            success_rate = (
                round((broadcast.sent_count / broadcast.total_count * 100), 1) if broadcast.total_count > 0 else 0
            )

            message_preview = (
                broadcast.message_text[:100] + '...'
                if broadcast.message_text and len(broadcast.message_text) > 100
                else (broadcast.message_text or '📊 نظرسنجی')
            )

            import html

            message_preview = html.escape(message_preview)

            text += f"""
{status_emoji} <b>{broadcast.created_at.strftime('%d.%m.%Y %H:%M')}</b>
📊 ارسال‌شده: {broadcast.sent_count}/{broadcast.total_count} ({success_rate}%)
🎯 مخاطبان: {get_target_name(broadcast.target_type)}
👤 ادمین: {html.escape(broadcast.admin_name or '')}
📝 پیام: {message_preview}
━━━━━━━━━━━━━━━━━━━━━━━
"""

        keyboard = get_broadcast_history_keyboard(page, total_pages, db_user.language).inline_keyboard

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def show_custom_broadcast(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    stats = await get_users_statistics(db)

    text = f"""
📝 <b>ارسال بر اساس معیار</b>

📊 <b>فیلترهای موجود:</b>

👥 <b>بر اساس ثبت‌نام:</b>
• امروز: {stats['today']} نفر
• هفته گذشته: {stats['week']} نفر
• ماه گذشته: {stats['month']} نفر

💼 <b>بر اساس فعالیت:</b>
• فعال امروز: {stats['active_today']} نفر
• غیرفعال ۷+ روز: {stats['inactive_week']} نفر
• غیرفعال ۳۰+ روز: {stats['inactive_month']} نفر

🔗 <b>بر اساس منبع:</b>
• از طریق ارجاع: {stats['referrals']} نفر
• ثبت‌نام مستقیم: {stats['direct']} نفر

معیار فیلتر را انتخاب کنید:
"""

    await callback.message.edit_text(
        text, reply_markup=get_custom_criteria_keyboard(db_user.language), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def select_custom_criteria(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    criteria = callback.data.replace('criteria_', '')

    criteria_names = {
        'today': 'ثبت‌نام‌شده‌های امروز',
        'week': 'ثبت‌نام‌شده‌های هفته گذشته',
        'month': 'ثبت‌نام‌شده‌های ماه گذشته',
        'active_today': 'فعال امروز',
        'inactive_week': 'غیرفعال ۷+ روز',
        'inactive_month': 'غیرفعال ۳۰+ روز',
        'referrals': 'آمده از طریق ارجاع',
        'direct': 'ثبت‌نام مستقیم',
    }

    user_count = await get_custom_users_count(db, criteria)

    await state.update_data(broadcast_target=f'custom_{criteria}')

    await callback.message.edit_text(
        f'📨 <b>ایجاد ارسال</b>\n\n'
        f'🎯 <b>معیار:</b> {criteria_names.get(criteria, criteria)}\n'
        f'👥 <b>دریافت‌کنندگان:</b> {user_count}\n\n'
        f'متن پیام ارسال را وارد کنید:\n\n'
        f'<i>نشانه‌گذاری HTML پشتیبانی می‌شود</i>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_messages')]]
        ),
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def select_broadcast_target(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    raw_target = callback.data[len('broadcast_') :]
    target_aliases = {
        'no_sub': 'no',
    }
    target = target_aliases.get(raw_target, raw_target)

    target_names = {
        'all': 'به همه کاربران',
        'active': 'دارای اشتراک فعال',
        'trial': 'دارای اشتراک آزمایشی',
        'no': 'بدون اشتراک',
        'expiring': 'دارای اشتراک رو به انقضا',
        'expired': 'دارای اشتراک منقضی‌شده',
        'active_zero': 'اشتراک فعال، ترافیک ۰ گیگابایت',
        'trial_zero': 'اشتراک آزمایشی، ترافیک ۰ گیگابایت',
    }

    # Handle tariff filter
    target_name = target_names.get(target, target)
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, tariff_id)
        if tariff:
            target_name = f'تعرفه «{tariff.name}»'
        else:
            target_name = f'تعرفه #{tariff_id}'

    user_count = await get_target_users_count(db, target)

    await state.update_data(broadcast_target=target)

    await callback.message.edit_text(
        f'📨 <b>ایجاد ارسال</b>\n\n'
        f'🎯 <b>مخاطبان:</b> {target_name}\n'
        f'👥 <b>دریافت‌کنندگان:</b> {user_count}\n\n'
        f'متن پیام ارسال را وارد کنید:\n\n'
        f'<i>نشانه‌گذاری HTML پشتیبانی می‌شود</i>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_messages')]]
        ),
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()


@admin_required
@error_handler
async def process_broadcast_message(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    broadcast_text = message.text

    if len(broadcast_text) > 4000:
        await message.answer('❌ پیام خیلی طولانی است (حداکثر ۴۰۰۰ کاراکتر)')
        return

    await state.update_data(broadcast_message=broadcast_text)

    await message.answer(
        '🖼️ <b>افزودن رسانه</b>\n\n'
        'می‌توانید عکس، ویدیو یا سندی به پیام اضافه کنید.\n'
        'یا این مرحله را رد کنید.\n\n'
        'نوع رسانه را انتخاب کنید:',
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def handle_media_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    if callback.data == 'skip_media':
        await state.update_data(has_media=False)
        await show_button_selector_callback(callback, db_user, state)
        return

    media_type = callback.data.replace('add_media_', '')

    media_instructions = {
        'photo': '📷 عکس مورد نظر برای ارسال را بفرستید:',
        'video': '🎥 ویدیوی مورد نظر برای ارسال را بفرستید:',
        'document': '📄 سند مورد نظر برای ارسال را بفرستید:',
    }

    await state.update_data(media_type=media_type, waiting_for_media=True)

    instruction_text = (
        f'{media_instructions.get(media_type, "فایل رسانه را بفرستید:")}\n\n<i>حجم فایل نباید از ۵۰ مگابایت بیشتر باشد</i>'
    )
    instruction_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_messages')]]
    )

    # Check whether the current message is a media message
    is_media_message = (
        callback.message.photo
        or callback.message.video
        or callback.message.document
        or callback.message.animation
        or callback.message.audio
        or callback.message.voice
    )

    if is_media_message:
        # Delete the media message and send a new text one
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(instruction_text, reply_markup=instruction_keyboard, parse_mode='HTML')
    else:
        await callback.message.edit_text(instruction_text, reply_markup=instruction_keyboard, parse_mode='HTML')

    await state.set_state(AdminStates.waiting_for_broadcast_media)
    await callback.answer()


@admin_required
@error_handler
async def process_broadcast_media(message: types.Message, db_user: User, state: FSMContext):
    data = await state.get_data()
    expected_type = data.get('media_type')

    media_file_id = None
    media_type = None

    if message.photo and expected_type == 'photo':
        media_file_id = message.photo[-1].file_id
        media_type = 'photo'
    elif message.video and expected_type == 'video':
        media_file_id = message.video.file_id
        media_type = 'video'
    elif message.document and expected_type == 'document':
        media_file_id = message.document.file_id
        media_type = 'document'
    else:
        await message.answer(f'❌ لطفاً {expected_type} را مطابق دستورالعمل ارسال کنید.')
        return

    await state.update_data(
        has_media=True, media_file_id=media_file_id, media_type=media_type, media_caption=message.caption
    )

    await show_media_preview(message, db_user, state)


async def show_media_preview(message: types.Message, db_user: User, state: FSMContext):
    data = await state.get_data()
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')

    preview_text = (
        f'🖼️ <b>رسانه اضافه شد</b>\n\n'
        f'📎 <b>نوع:</b> {media_type}\n'
        f'✅ فایل ذخیره شد و آماده ارسال است\n\n'
        f'مرحله بعد چیست؟'
    )

    # For broadcast preview we use the original method without logo-patching
    # so we show exactly the uploaded photo
    from app.utils.message_patch import _original_answer

    if media_type == 'photo' and media_file_id:
        # Show preview with the uploaded photo
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=media_file_id,
            caption=preview_text,
            reply_markup=get_media_confirm_keyboard(db_user.language),
            parse_mode='HTML',
        )
    else:
        # For other media types or if there is no photo, use a regular message
        await _original_answer(
            message, preview_text, reply_markup=get_media_confirm_keyboard(db_user.language), parse_mode='HTML'
        )


@admin_required
@error_handler
async def handle_media_confirmation(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    action = callback.data

    if action == 'confirm_media':
        await show_button_selector_callback(callback, db_user, state)
    elif action == 'replace_media':
        data = await state.get_data()
        data.get('media_type', 'photo')
        await handle_media_selection(callback, db_user, state)
    elif action == 'skip_media':
        await state.update_data(has_media=False, media_file_id=None, media_type=None, media_caption=None)
        await show_button_selector_callback(callback, db_user, state)


@admin_required
@error_handler
async def handle_change_media(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await safe_edit_or_send_text(
        callback,
        '🖼️ <b>تغییر رسانه</b>\n\nنوع رسانه جدید را انتخاب کنید:',
        reply_markup=get_broadcast_media_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_button_selector_callback(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    data = await state.get_data()
    has_media = data.get('has_media', False)
    selected_buttons = data.get('selected_buttons')

    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)

    media_info = ''
    if has_media:
        media_type = data.get('media_type', 'فایل')
        media_info = f'\n🖼️ <b>رسانه:</b> {media_type} اضافه شد'

    text = f"""
📘 <b>انتخاب دکمه‌های اضافی</b>

دکمه‌هایی را که به پیام ارسال اضافه می‌شوند انتخاب کنید:

💰 <b>افزایش موجودی</b> — روش‌های افزایش موجودی را باز می‌کند
🤝 <b>شراکت</b> — برنامه ارجاع را باز می‌کند
🎫 <b>کد تخفیف</b> — فرم وارد کردن کد تخفیف را باز می‌کند
🔗 <b>اتصال</b> — به اتصال برنامه کمک می‌کند
📱 <b>اشتراک</b> — وضعیت اشتراک را نشان می‌دهد
🛠️ <b>پشتیبانی</b> — با پشتیبانی ارتباط برقرار می‌کند

🏠 <b>دکمه "صفحه اصلی"</b> به‌طور پیش‌فرض فعال است، اما در صورت نیاز می‌توانید آن را غیرفعال کنید.{media_info}

دکمه‌های مورد نظر را انتخاب کنید و «ادامه» را بزنید:
"""

    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    # Check whether the current message is a media message
    # (photo, video, document, etc.) — edit_text cannot be used for those
    is_media_message = (
        callback.message.photo
        or callback.message.video
        or callback.message.document
        or callback.message.animation
        or callback.message.audio
        or callback.message.voice
    )

    if is_media_message:
        # Delete the media message and send a new text one
        try:
            await callback.message.delete()
        except Exception:
            pass  # Ignore deletion errors
        await callback.message.answer(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_button_selector(message: types.Message, db_user: User, state: FSMContext):
    data = await state.get_data()
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)

    has_media = data.get('has_media', False)

    text = """
📘 <b>انتخاب دکمه‌های اضافی</b>

دکمه‌هایی را که به پیام ارسال اضافه می‌شوند انتخاب کنید:

💰 <b>افزایش موجودی</b> — روش‌های افزایش موجودی را باز می‌کند
🤝 <b>شراکت</b> — برنامه ارجاع را باز می‌کند
🎫 <b>کد تخفیف</b> — فرم وارد کردن کد تخفیف را باز می‌کند
🔗 <b>اتصال</b> — به اتصال برنامه کمک می‌کند
📱 <b>اشتراک</b> — وضعیت اشتراک را نشان می‌دهد
🛠️ <b>پشتیبانی</b> — با پشتیبانی ارتباط برقرار می‌کند

🏠 <b>دکمه "صفحه اصلی"</b> به‌طور پیش‌فرض فعال است، اما در صورت نیاز می‌توانید آن را غیرفعال کنید.

دکمه‌های مورد نظر را انتخاب کنید و «ادامه» را بزنید:
"""

    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    await message.answer(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_button_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    button_type = callback.data.replace('btn_', '')
    data = await state.get_data()
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
    else:
        selected_buttons = list(selected_buttons)

    if button_type in selected_buttons:
        selected_buttons.remove(button_type)
    else:
        selected_buttons.append(button_type)

    await state.update_data(selected_buttons=selected_buttons)

    has_media = data.get('has_media', False)
    keyboard = get_updated_message_buttons_selector_keyboard_with_media(selected_buttons, has_media, db_user.language)

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def confirm_button_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    target = data.get('broadcast_target')
    message_text = data.get('broadcast_message')
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
        await state.update_data(selected_buttons=selected_buttons)
    has_media = data.get('has_media', False)
    media_type = data.get('media_type')

    user_count = (
        await get_target_users_count(db, target)
        if not target.startswith('custom_')
        else await get_custom_users_count(db, target.replace('custom_', ''))
    )
    target_display = get_target_display_name(target)

    media_info = ''
    if has_media:
        media_type_names = {'photo': 'عکس', 'video': 'ویدیو', 'document': 'سند'}
        media_info = f'\n🖼️ <b>رسانه:</b> {media_type_names.get(media_type, media_type)}'

    ordered_keys = [button_key for row in BUTTON_ROWS for button_key in row]
    button_labels = get_broadcast_button_labels(db_user.language)
    selected_names = [button_labels[key] for key in ordered_keys if key in selected_buttons]
    if selected_names:
        buttons_info = f'\n📘 <b>دکمه‌ها:</b> {", ".join(selected_names)}'
    else:
        buttons_info = '\n📘 <b>دکمه‌ها:</b> موجود نیست'

    preview_text = f"""
📨 <b>پیش‌نمایش ارسال</b>

🎯 <b>مخاطبان:</b> {target_display}
👥 <b>دریافت‌کنندگان:</b> {user_count}

📝 <b>پیام:</b>
{message_text}{media_info}

{buttons_info}

تأیید ارسال؟
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='✅ ارسال', callback_data='admin_confirm_broadcast'),
            types.InlineKeyboardButton(text='📘 ویرایش دکمه‌ها', callback_data='edit_buttons'),
        ]
    ]

    if has_media:
        keyboard.append([types.InlineKeyboardButton(text='🖼️ تغییر رسانه', callback_data='change_media')])

    keyboard.append([types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_messages')])

    # If there is media, show it with the uploaded photo; otherwise show a plain text message
    if has_media and media_type == 'photo':
        media_file_id = data.get('media_file_id')
        if media_file_id:
            # Delete the current message and send a new one with the photo
            try:
                await callback.message.delete()
            except Exception:
                pass
            # Telegram limits caption to 1024 characters
            if len(preview_text) <= 1024:
                await callback.bot.send_photo(
                    chat_id=callback.message.chat.id,
                    photo=media_file_id,
                    caption=preview_text,
                    reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                    parse_mode='HTML',
                )
            else:
                # Photo without caption + text as a separate message
                await callback.bot.send_photo(
                    chat_id=callback.message.chat.id,
                    photo=media_file_id,
                )
                await callback.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=preview_text,
                    reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                    parse_mode='HTML',
                )
        else:
            # If there is no file_id, use safe editing
            await safe_edit_or_send_text(
                callback,
                preview_text,
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
                parse_mode='HTML',
            )
    else:
        # For text messages or other media types use safe editing
        await safe_edit_or_send_text(
            callback, preview_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
        )

    await callback.answer()


@admin_required
@error_handler
async def confirm_broadcast(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    target = data.get('broadcast_target')
    message_text = data.get('broadcast_message')
    selected_buttons = data.get('selected_buttons')
    if selected_buttons is None:
        selected_buttons = list(DEFAULT_SELECTED_BUTTONS)
    has_media = data.get('has_media', False)
    media_type = data.get('media_type')
    media_file_id = data.get('media_file_id')
    media_caption = data.get('media_caption')

    # =========================================================================
    # CRITICAL: Extract ALL scalar values from ORM objects NOW,
    # while the session is still active. After the broadcast starts, the DB
    # connection may close due to a timeout, and any access to ORM attributes will cause:
    # - MissingGreenlet (lazy loading outside async context)
    # - InterfaceError (connection closed)
    # =========================================================================
    admin_id: int = db_user.id
    admin_name: str = db_user.full_name  # property, reads first_name/last_name
    admin_telegram_id: int | None = db_user.telegram_id
    admin_language: str = db_user.language

    await safe_edit_or_send_text(
        callback,
        '📨 <b>آماده‌سازی ارسال...</b>\n\n⏳ در حال بارگذاری لیست دریافت‌کنندگان...',
        reply_markup=None,
        parse_mode='HTML',
    )

    # Load users and immediately extract telegram_id into a list
    # so we don't access ORM objects during the long broadcast
    if target.startswith('custom_'):
        users_orm = await get_custom_users(db, target.replace('custom_', ''))
    else:
        users_orm = await get_target_users(db, target)

    # Extract only telegram_id — that's all we need for sending
    # Filter out None (email-only users)
    recipient_telegram_ids: list[int] = [user.telegram_id for user in users_orm if user.telegram_id is not None]
    total_users_count = len(users_orm)

    # Create a broadcast history record
    broadcast_history = BroadcastHistory(
        target_type=target,
        message_text=message_text,
        has_media=has_media,
        media_type=media_type,
        media_file_id=media_file_id,
        media_caption=media_caption,
        total_count=total_users_count,
        sent_count=0,
        failed_count=0,
        admin_id=admin_id,
        admin_name=admin_name,
        status='in_progress',
    )
    db.add(broadcast_history)
    await db.commit()
    await db.refresh(broadcast_history)

    # Save the ID — it's the only thing we need after the commit
    broadcast_id: int = broadcast_history.id

    # =========================================================================
    # From this point on we do NOT use the db session or ORM objects!
    # We work only with scalar values.
    # =========================================================================

    sent_count = 0
    failed_count = 0

    broadcast_keyboard = create_broadcast_keyboard(selected_buttons, admin_language)

    # =========================================================================
    # Rate limiting: Telegram allows ~30 msg/sec for a bot.
    # We use batch_size=25 + 1 sec delay between batches = ~25 msg/sec
    # with headroom to avoid FloodWait.
    # Semaphore=25 — all messages in the batch are sent in parallel.
    # =========================================================================
    _BATCH_SIZE = 25
    _BATCH_DELAY = 1.0  # seconds between batches
    _MAX_SEND_RETRIES = 3
    # Update progress every N batches (not every message — otherwise FloodWait on edit_text)
    _PROGRESS_UPDATE_INTERVAL = max(1, 500 // _BATCH_SIZE)  # ~every 500 messages
    # Minimum interval between progress updates (seconds)
    _PROGRESS_MIN_INTERVAL = 5.0

    # Global pause on FloodWait — stalls ALL sends, not just one semaphore slot
    flood_wait_until: float = 0.0

    async def send_single_broadcast(telegram_id: int) -> str:
        """Sends a single message. Returns 'sent', 'blocked', or 'failed'."""
        nonlocal flood_wait_until

        for attempt in range(_MAX_SEND_RETRIES):
            # Global pause on FloodWait
            now = asyncio.get_event_loop().time()
            if flood_wait_until > now:
                await asyncio.sleep(flood_wait_until - now)

            try:
                if has_media and media_file_id:
                    send_method = {
                        'photo': callback.bot.send_photo,
                        'video': callback.bot.send_video,
                        'document': callback.bot.send_document,
                    }.get(media_type)
                    if send_method:
                        media_kwarg = {
                            'photo': 'photo',
                            'video': 'video',
                            'document': 'document',
                        }[media_type]
                        # Telegram limits caption to 1024 characters
                        if len(message_text) <= 1024:
                            await send_method(
                                chat_id=telegram_id,
                                **{media_kwarg: media_file_id},
                                caption=message_text,
                                parse_mode='HTML',
                                reply_markup=broadcast_keyboard,
                            )
                        else:
                            # Media without caption + text as a separate message
                            await send_method(
                                chat_id=telegram_id,
                                **{media_kwarg: media_file_id},
                            )
                            await callback.bot.send_message(
                                chat_id=telegram_id,
                                text=message_text,
                                parse_mode='HTML',
                                reply_markup=broadcast_keyboard,
                            )
                    else:
                        # Unknown media_type — send as text
                        await callback.bot.send_message(
                            chat_id=telegram_id,
                            text=message_text,
                            parse_mode='HTML',
                            reply_markup=broadcast_keyboard,
                        )
                else:
                    await callback.bot.send_message(
                        chat_id=telegram_id,
                        text=message_text,
                        parse_mode='HTML',
                        reply_markup=broadcast_keyboard,
                    )
                return 'sent'

            except TelegramRetryAfter as e:
                # Global pause — stall all coroutines
                wait_seconds = e.retry_after + 1
                flood_wait_until = asyncio.get_event_loop().time() + wait_seconds
                logger.warning(
                    'FloodWait: Telegram requests to wait sec (user, attempt /)',
                    retry_after=e.retry_after,
                    telegram_id=telegram_id,
                    attempt=attempt + 1,
                    MAX_SEND_RETRIES=_MAX_SEND_RETRIES,
                )
                await asyncio.sleep(wait_seconds)

            except TelegramForbiddenError:
                return 'blocked'

            except TelegramBadRequest as e:
                err = str(e).lower()
                if 'bot was blocked' in err or 'user is deactivated' in err or 'chat not found' in err:
                    return 'blocked'
                logger.debug('BadRequest while broadcasting to user', telegram_id=telegram_id, e=e)
                return 'failed'

            except Exception as e:
                logger.error(
                    'Error sending to user (attempt /)',
                    telegram_id=telegram_id,
                    attempt=attempt + 1,
                    MAX_SEND_RETRIES=_MAX_SEND_RETRIES,
                    e=e,
                )
                if attempt < _MAX_SEND_RETRIES - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))

        return 'failed'

    # =========================================================================
    # Real-time progress bar (like in the blocked-users scanner)
    # =========================================================================
    total_recipients = len(recipient_telegram_ids)
    last_progress_update: float = 0.0
    # ID of the message being updated (may be replaced on error)
    progress_message = callback.message

    def _build_progress_text(
        current_sent: int,
        current_failed: int,
        total: int,
        phase: str = 'sending',
        current_blocked: int = 0,
    ) -> str:
        processed = current_sent + current_failed + current_blocked
        percent = round(processed / total * 100, 1) if total > 0 else 0
        bar_length = 20
        filled = int(bar_length * processed / total) if total > 0 else 0
        bar = '█' * filled + '░' * (bar_length - filled)

        if phase == 'sending':
            blocked_line = f'• ربات را بلاک کرده‌اند: {current_blocked}\n' if current_blocked else ''
            return (
                f'📨 <b>ارسال در حال انجام...</b>\n\n'
                f'[{bar}] {percent}%\n\n'
                f'📊 <b>پیشرفت:</b>\n'
                f'• ارسال‌شده: {current_sent}\n'
                f'{blocked_line}'
                f'• خطاها: {current_failed}\n'
                f'• پردازش‌شده: {processed}/{total}\n\n'
                f'⏳ گفتگو را نبندید — ارسال ادامه دارد...'
            )
        return ''

    async def _update_progress_message(current_sent: int, current_failed: int, current_blocked: int = 0) -> None:
        """Safely updates the progress message."""
        nonlocal last_progress_update, progress_message
        now = asyncio.get_event_loop().time()
        if now - last_progress_update < _PROGRESS_MIN_INTERVAL:
            return
        last_progress_update = now

        text = _build_progress_text(current_sent, current_failed, total_recipients, current_blocked=current_blocked)
        try:
            await progress_message.edit_text(text, parse_mode='HTML')
        except TelegramRetryAfter as e:
            # Don't panic — skip the progress update
            logger.debug('FloodWait while updating progress, skipping: sec', retry_after=e.retry_after)
        except TelegramBadRequest:
            # Message deleted or content unchanged — send a new one
            try:
                progress_message = await callback.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=text,
                    parse_mode='HTML',
                )
            except Exception:
                pass
        except Exception:
            pass  # Don't break the broadcast due to progress update errors

    # First progress update
    await _update_progress_message(0, 0)

    blocked_count = 0
    blocked_telegram_ids: list[int] = []

    # =========================================================================
    # Main broadcast loop — batches of _BATCH_SIZE
    # =========================================================================
    for batch_idx, i in enumerate(range(0, total_recipients, _BATCH_SIZE)):
        batch = recipient_telegram_ids[i : i + _BATCH_SIZE]

        # Send the batch in parallel
        results = await asyncio.gather(
            *[send_single_broadcast(tid) for tid in batch],
            return_exceptions=True,
        )

        for idx, result in enumerate(results):
            if isinstance(result, str):
                if result == 'sent':
                    sent_count += 1
                elif result == 'blocked':
                    blocked_count += 1
                    blocked_telegram_ids.append(batch[idx])
                else:
                    failed_count += 1
            elif isinstance(result, Exception):
                failed_count += 1
                logger.error('Unhandled exception in broadcast', result=result)

        # Update progress every _PROGRESS_UPDATE_INTERVAL batches
        if batch_idx % _PROGRESS_UPDATE_INTERVAL == 0:
            await _update_progress_message(sent_count, failed_count, blocked_count)

        # Delay between batches to respect rate limits
        await asyncio.sleep(_BATCH_DELAY)

    # Account for skipped email-only users
    skipped_email_users = total_users_count - total_recipients
    if skipped_email_users > 0:
        logger.info('Skipped email-only users during broadcast', skipped_email_users=skipped_email_users)

    status = 'completed' if failed_count == 0 and blocked_count == 0 else 'partial'

    # Save the result in a NEW session (the old one is already dead)
    await _persist_broadcast_result(
        broadcast_id=broadcast_id,
        sent_count=sent_count,
        failed_count=failed_count,
        status=status,
        blocked_count=blocked_count,
    )

    success_rate = round(sent_count / total_users_count * 100, 1) if total_users_count else 0
    media_info = f'\n🖼️ <b>رسانه:</b> {media_type}' if has_media else ''
    blocked_line = f'• ربات را بلاک کرده‌اند: {blocked_count}\n' if blocked_count else ''

    result_text = (
        f'✅ <b>ارسال تمام شد!</b>\n\n'
        f'📊 <b>نتیجه:</b>\n'
        f'• ارسال‌شده: {sent_count}\n'
        f'{blocked_line}'
        f'• تحویل‌نشده: {failed_count}\n'
        f'• مجموع کاربران: {total_users_count}\n'
        f'• موفقیت: {success_rate}%{media_info}\n\n'
        f'<b>ادمین:</b> {html.escape(admin_name)}'
    )

    back_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='📨 بازگشت به ارسال‌ها', callback_data='admin_messages')]]
    )

    try:
        await progress_message.edit_text(result_text, reply_markup=back_keyboard, parse_mode='HTML')
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if (
            'message to edit not found' in error_msg
            or 'there is no text' in error_msg
            or "message can't be edited" in error_msg
        ):
            await callback.bot.send_message(
                chat_id=callback.message.chat.id,
                text=result_text,
                reply_markup=back_keyboard,
                parse_mode='HTML',
            )
        else:
            raise

    await state.clear()
    logger.info(
        'Broadcast completed by admin: sent failed total= (media:)',
        admin_telegram_id=admin_telegram_id,
        sent_count=sent_count,
        failed_count=failed_count,
        total_users_count=total_users_count,
        has_media=has_media,
    )


async def get_target_users_count(db: AsyncSession, target: str) -> int:
    """Fast user count via SQL COUNT instead of loading all users into memory."""
    from sqlalchemy import distinct, func as sql_func

    base_filter = User.status == UserStatus.ACTIVE.value

    if target == 'all':
        query = select(sql_func.count(User.id)).where(base_filter)
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'active':
        # Active paid subscriptions (not trial)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.is_trial == False,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'trial':
        # Trial subscriptions (without is_active check, as in the original)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.is_trial == True,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'no':
        # No active subscription — use NOT EXISTS for correctness
        subquery = (
            select(Subscription.id)
            .where(
                Subscription.user_id == User.id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
            .correlate(User)
            .exists()
        )
        query = select(sql_func.count(User.id)).where(base_filter, ~subquery)
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expiring':
        # Expiring in the next 3 days
        now = datetime.now(UTC)
        expiry_threshold = now + timedelta(days=3)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date <= expiry_threshold,
                Subscription.end_date > now,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'expiring_subscribers':
        # Expiring in the next 7 days
        now = datetime.now(UTC)
        expiry_threshold = now + timedelta(days=7)
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date <= expiry_threshold,
                Subscription.end_date > now,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target in ('expired', 'expired_subscribers'):
        # Expired subscriptions — exclude users with at least one active subscription
        now = datetime.now(UTC)
        expired_statuses = [
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.DISABLED.value,
            SubscriptionStatus.LIMITED.value,
        ]
        has_active_sub = (
            select(Subscription.id)
            .where(
                Subscription.user_id == User.id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
            .correlate(User)
            .exists()
        )
        query = (
            select(sql_func.count(distinct(User.id)))
            .outerjoin(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                ~has_active_sub,
                or_(
                    Subscription.status.in_(expired_statuses),
                    and_(Subscription.end_date <= now, Subscription.status != SubscriptionStatus.ACTIVE.value),
                    and_(Subscription.id == None, User.has_had_paid_subscription == True),
                ),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'active_zero':
        # Active paid subscriptions with zero traffic
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.is_trial == False,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'trial_zero':
        # Trial subscriptions with zero traffic
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.is_trial == True,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    if target == 'zero':
        # All active subscriptions with zero traffic
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                or_(Subscription.traffic_used_gb == None, Subscription.traffic_used_gb <= 0),
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    # Tariff filter
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        query = (
            select(sql_func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                base_filter,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.tariff_id == tariff_id,
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0

    # Custom filters — fast COUNT instead of loading all users
    if target.startswith('custom_'):
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        criteria = target[len('custom_') :]

        if criteria == 'today':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= today)
        elif criteria == 'week':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= now - timedelta(days=7))
        elif criteria == 'month':
            query = select(sql_func.count(User.id)).where(base_filter, User.created_at >= now - timedelta(days=30))
        elif criteria == 'active_today':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity >= today)
        elif criteria == 'inactive_week':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity < now - timedelta(days=7))
        elif criteria == 'inactive_month':
            query = select(sql_func.count(User.id)).where(base_filter, User.last_activity < now - timedelta(days=30))
        elif criteria == 'referrals':
            query = select(sql_func.count(User.id)).where(base_filter, User.referred_by_id.isnot(None))
        elif criteria == 'direct':
            query = select(sql_func.count(User.id)).where(base_filter, User.referred_by_id.is_(None))
        else:
            return 0

        result = await db.execute(query)
        return result.scalar() or 0

    return 0


async def get_target_users(db: AsyncSession, target: str) -> list:
    # Load all active users in batches to avoid the 10k limit
    users: list[User] = []
    offset = 0
    batch_size = 5000

    while True:
        batch = await get_users_list(
            db,
            offset=offset,
            limit=batch_size,
            status=UserStatus.ACTIVE,
        )

        if not batch:
            break

        users.extend(batch)
        offset += batch_size

    if target == 'all':
        return users

    if target == 'active':
        return [
            user
            for user in users
            if any(s.is_active and not s.is_trial for s in (getattr(user, 'subscriptions', None) or []))
        ]

    if target == 'trial':
        return [user for user in users if any(s.is_trial for s in (getattr(user, 'subscriptions', None) or []))]

    if target == 'no':
        return [user for user in users if not any(s.is_active for s in (getattr(user, 'subscriptions', None) or []))]

    if target == 'expiring':
        expiring_subs = await get_expiring_subscriptions(db, 3)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == 'expired':
        now = datetime.now(UTC)
        expired_statuses = {
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.DISABLED.value,
        }
        expired_users = []
        for user in users:
            subs = getattr(user, 'subscriptions', None) or []
            if subs:
                has_active = any(s.is_active for s in subs)
                if has_active:
                    continue  # Skip users who have at least one active subscription
                has_expired = any(s.status in expired_statuses or (s.end_date <= now and not s.is_active) for s in subs)
                if has_expired:
                    expired_users.append(user)
            elif user.has_had_paid_subscription:
                expired_users.append(user)
        return expired_users

    if target == 'active_zero':
        return [
            user
            for user in users
            if any(
                not s.is_trial and s.is_active and (s.traffic_used_gb or 0) <= 0
                for s in (getattr(user, 'subscriptions', None) or [])
            )
        ]

    if target == 'trial_zero':
        return [
            user
            for user in users
            if any(
                s.is_trial and s.is_active and (s.traffic_used_gb or 0) <= 0
                for s in (getattr(user, 'subscriptions', None) or [])
            )
        ]

    if target == 'zero':
        return [
            user
            for user in users
            if any(s.is_active and (s.traffic_used_gb or 0) <= 0 for s in (getattr(user, 'subscriptions', None) or []))
        ]

    if target == 'expiring_subscribers':
        expiring_subs = await get_expiring_subscriptions(db, 7)
        return [sub.user for sub in expiring_subs if sub.user]

    if target == 'expired_subscribers':
        now = datetime.now(UTC)
        expired_statuses = {
            SubscriptionStatus.EXPIRED.value,
            SubscriptionStatus.DISABLED.value,
        }
        expired_users = []
        for user in users:
            subs = getattr(user, 'subscriptions', None) or []
            if subs:
                has_active = any(s.is_active for s in subs)
                if has_active:
                    continue  # Skip users who have at least one active subscription
                has_expired = any(s.status in expired_statuses or (s.end_date <= now and not s.is_active) for s in subs)
                if has_expired:
                    expired_users.append(user)
            elif user.has_had_paid_subscription:
                expired_users.append(user)
        return expired_users

    if target == 'canceled_subscribers':
        return [
            user
            for user in users
            if any(s.status == SubscriptionStatus.DISABLED.value for s in (getattr(user, 'subscriptions', None) or []))
        ]

    if target == 'trial_ending':
        now = datetime.now(UTC)
        in_3_days = now + timedelta(days=3)
        return [
            user
            for user in users
            if any(
                s.is_trial and s.is_active and s.end_date <= in_3_days
                for s in (getattr(user, 'subscriptions', None) or [])
            )
        ]

    if target == 'trial_expired':
        now = datetime.now(UTC)
        return [
            user
            for user in users
            if any(s.is_trial and s.end_date <= now for s in (getattr(user, 'subscriptions', None) or []))
        ]

    if target == 'autopay_failed':
        from app.database.models import SubscriptionEvent

        week_ago = datetime.now(UTC) - timedelta(days=7)
        stmt = (
            select(SubscriptionEvent.user_id)
            .where(
                and_(
                    SubscriptionEvent.event_type == 'autopay_failed',
                    SubscriptionEvent.occurred_at >= week_ago,
                )
            )
            .distinct()
        )
        result = await db.execute(stmt)
        failed_user_ids = set(result.scalars().all())
        return [user for user in users if user.id in failed_user_ids]

    if target == 'low_balance':
        threshold_kopeks = 10000  # 100 rubles
        return [
            user for user in users if (user.balance_kopeks or 0) < threshold_kopeks and (user.balance_kopeks or 0) > 0
        ]

    if target == 'inactive_30d':
        threshold = datetime.now(UTC) - timedelta(days=30)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    if target == 'inactive_60d':
        threshold = datetime.now(UTC) - timedelta(days=60)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    if target == 'inactive_90d':
        threshold = datetime.now(UTC) - timedelta(days=90)
        return [user for user in users if user.last_activity and user.last_activity < threshold]

    # Tariff filter
    if target.startswith('tariff_'):
        tariff_id = int(target.split('_')[1])
        return [
            user
            for user in users
            if any(s.is_active and s.tariff_id == tariff_id for s in (getattr(user, 'subscriptions', None) or []))
        ]

    return []


async def get_custom_users_count(db: AsyncSession, criteria: str) -> int:
    users = await get_custom_users(db, criteria)
    return len(users)


async def get_custom_users(db: AsyncSession, criteria: str) -> list:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    if criteria == 'today':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= today))
    elif criteria == 'week':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= week_ago))
    elif criteria == 'month':
        stmt = select(User).where(and_(User.status == 'active', User.created_at >= month_ago))
    elif criteria == 'active_today':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity >= today))
    elif criteria == 'inactive_week':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity < week_ago))
    elif criteria == 'inactive_month':
        stmt = select(User).where(and_(User.status == 'active', User.last_activity < month_ago))
    elif criteria == 'referrals':
        stmt = select(User).where(and_(User.status == 'active', User.referred_by_id.isnot(None)))
    elif criteria == 'direct':
        stmt = select(User).where(and_(User.status == 'active', User.referred_by_id.is_(None)))
    else:
        return []

    result = await db.execute(stmt)
    return result.scalars().all()


async def get_users_statistics(db: AsyncSession) -> dict:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    stats = {}

    stats['today'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= today))) or 0
    )

    stats['week'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= week_ago)))
        or 0
    )

    stats['month'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.created_at >= month_ago)))
        or 0
    )

    stats['active_today'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity >= today)))
        or 0
    )

    stats['inactive_week'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity < week_ago)))
        or 0
    )

    stats['inactive_month'] = (
        await db.scalar(
            select(func.count(User.id)).where(and_(User.status == 'active', User.last_activity < month_ago))
        )
        or 0
    )

    stats['referrals'] = (
        await db.scalar(
            select(func.count(User.id)).where(and_(User.status == 'active', User.referred_by_id.isnot(None)))
        )
        or 0
    )

    stats['direct'] = (
        await db.scalar(select(func.count(User.id)).where(and_(User.status == 'active', User.referred_by_id.is_(None))))
        or 0
    )

    return stats


def get_target_name(target_type: str) -> str:
    names = {
        'all': 'به همه کاربران',
        'active': 'دارای اشتراک فعال',
        'trial': 'دارای اشتراک آزمایشی',
        'no': 'بدون اشتراک',
        'sub': 'بدون اشتراک',
        'expiring': 'دارای اشتراک رو به انقضا',
        'expired': 'دارای اشتراک منقضی‌شده',
        'active_zero': 'اشتراک فعال، ترافیک ۰ گیگابایت',
        'trial_zero': 'اشتراک آزمایشی، ترافیک ۰ گیگابایت',
        'zero': 'اشتراک، ترافیک ۰ گیگابایت',
        'custom_today': 'ثبت‌نام‌شده‌های امروز',
        'custom_week': 'ثبت‌نام‌شده‌های هفته گذشته',
        'custom_month': 'ثبت‌نام‌شده‌های ماه گذشته',
        'custom_active_today': 'فعال امروز',
        'custom_inactive_week': 'غیرفعال ۷+ روز',
        'custom_inactive_month': 'غیرفعال ۳۰+ روز',
        'custom_referrals': 'از طریق ارجاع',
        'custom_direct': 'ثبت‌نام مستقیم',
    }
    # Handle tariff filter
    if target_type.startswith('tariff_'):
        tariff_id = target_type.split('_')[1]
        return f'تعرفه #{tariff_id}'
    return names.get(target_type, target_type)


def get_target_display_name(target: str) -> str:
    return get_target_name(target)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_messages_menu, F.data == 'admin_messages')
    dp.callback_query.register(show_pinned_message_menu, F.data == 'admin_pinned_message')
    dp.callback_query.register(toggle_pinned_message_position, F.data == 'admin_pinned_message_position')
    dp.callback_query.register(toggle_pinned_message_start_mode, F.data == 'admin_pinned_message_start_mode')
    dp.callback_query.register(delete_pinned_message, F.data == 'admin_pinned_message_delete')
    dp.callback_query.register(prompt_pinned_message_update, F.data == 'admin_pinned_message_edit')
    dp.callback_query.register(handle_pinned_broadcast_now, F.data.startswith('admin_pinned_broadcast_now:'))
    dp.callback_query.register(handle_pinned_broadcast_skip, F.data.startswith('admin_pinned_broadcast_skip:'))
    dp.callback_query.register(show_broadcast_targets, F.data.in_(['admin_msg_all', 'admin_msg_by_sub']))
    dp.callback_query.register(show_tariff_filter, F.data == 'broadcast_by_tariff')
    dp.callback_query.register(select_broadcast_target, F.data.startswith('broadcast_'))
    dp.callback_query.register(confirm_broadcast, F.data == 'admin_confirm_broadcast')

    dp.callback_query.register(show_messages_history, F.data.startswith('admin_msg_history'))
    dp.callback_query.register(show_custom_broadcast, F.data == 'admin_msg_custom')
    dp.callback_query.register(select_custom_criteria, F.data.startswith('criteria_'))

    dp.callback_query.register(toggle_button_selection, F.data.startswith('btn_'))
    dp.callback_query.register(confirm_button_selection, F.data == 'buttons_confirm')
    dp.callback_query.register(show_button_selector_callback, F.data == 'edit_buttons')
    dp.callback_query.register(handle_media_selection, F.data.startswith('add_media_'))
    dp.callback_query.register(handle_media_selection, F.data == 'skip_media')
    dp.callback_query.register(handle_media_confirmation, F.data.in_(['confirm_media', 'replace_media']))
    dp.callback_query.register(handle_change_media, F.data == 'change_media')
    dp.message.register(process_broadcast_message, AdminStates.waiting_for_broadcast_message)
    dp.message.register(process_broadcast_media, AdminStates.waiting_for_broadcast_media)
    dp.message.register(process_pinned_message_update, AdminStates.editing_pinned_message)
