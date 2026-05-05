import html
import time
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.models import Ticket, TicketStatus, User
from app.keyboards.inline import (
    get_admin_ticket_reply_cancel_keyboard,
    get_admin_ticket_view_keyboard,
    get_admin_tickets_keyboard,
)
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.states import AdminTicketStates
from app.utils.cache import RateLimitCache


logger = structlog.get_logger(__name__)

# Maximum Telegram message length (with margin)
MAX_MESSAGE_LEN = 3500


def _split_long_block(block: str, max_len: int) -> list[str]:
    """Splits a too-long block into parts."""
    if len(block) <= max_len:
        return [block]

    parts = []
    remaining = block
    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break
        cut_at = max_len
        newline_pos = remaining.rfind('\n', 0, max_len)
        space_pos = remaining.rfind(' ', 0, max_len)

        if newline_pos > max_len // 2:
            cut_at = newline_pos + 1
        elif space_pos > max_len // 2:
            cut_at = space_pos + 1

        parts.append(remaining[:cut_at])
        remaining = remaining[cut_at:]

    return parts


def _split_text_into_pages(header: str, message_blocks: list[str], max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    """Splits text into pages respecting the Telegram limit."""
    pages: list[str] = []
    current = header
    header_len = len(header)
    block_max_len = max_len - header_len - 50

    for block in message_blocks:
        if len(block) > block_max_len:
            block_parts = _split_long_block(block, block_max_len)
            for part in block_parts:
                if len(current) + len(part) > max_len:
                    if current.strip() and current != header:
                        pages.append(current)
                    current = header + part
                else:
                    current += part
        elif len(current) + len(block) > max_len:
            if current.strip() and current != header:
                pages.append(current)
            current = header + block
        else:
            current += block

    if current.strip():
        pages.append(current)

    return pages or [header]


async def show_admin_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show all tickets for admins."""
    # permission gate: admin or active moderator only
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    texts = get_texts(db_user.language)

    # Determine current page and scope
    current_page = 1
    scope = 'open'
    data_str = callback.data
    if data_str == 'admin_tickets_scope_open':
        scope = 'open'
    elif data_str == 'admin_tickets_scope_closed':
        scope = 'closed'
    elif data_str.startswith('admin_tickets_page_'):
        try:
            parts = data_str.split('_')
            # format: admin_tickets_page_{scope}_{page}
            if len(parts) >= 5:
                scope = parts[3]
                current_page = int(parts[4])
            else:
                current_page = int(data_str.replace('admin_tickets_page_', ''))
        except ValueError:
            current_page = 1
    statuses = (
        [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value] if scope == 'open' else [TicketStatus.CLOSED.value]
    )
    page_size = 10
    # total count for proper pagination
    total_count = await TicketCRUD.count_tickets_by_statuses(db, statuses)
    total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count > 0 else 1
    current_page = max(current_page, 1)
    current_page = min(current_page, total_pages)
    offset = (current_page - 1) * page_size
    tickets = await TicketCRUD.get_tickets_by_statuses(db, statuses=statuses, limit=page_size, offset=offset)

    # Show section toggles even when there are no tickets

    # Build keyboard data
    ticket_data = []
    for ticket in tickets:
        user_name = ticket.user.full_name if ticket.user else 'Unknown'
        username = ticket.user.username if ticket.user else None
        telegram_id = ticket.user.telegram_id if ticket.user else None
        ticket_data.append(
            {
                'id': ticket.id,
                'title': ticket.title,
                'status_emoji': ticket.status_emoji,
                'priority_emoji': ticket.priority_emoji,
                'user_name': user_name,
                'username': username,
                'telegram_id': telegram_id,
                'is_closed': ticket.is_closed,
                'locked_emoji': ('🔒' if ticket.is_user_reply_blocked else ''),
            }
        )

    # Total pages already calculated above
    header_text = (
        texts.t('ADMIN_TICKETS_TITLE_OPEN', '�� تیکت‌های باز پشتیبانی:')
        if scope == 'open'
        else texts.t('ADMIN_TICKETS_TITLE_CLOSED', '🎫 تیکت‌های بسته پشتیبانی:')
    )
    # Determine proper back target for moderators
    back_cb = 'admin_submenu_support'
    try:
        if not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(callback.from_user.id):
            back_cb = 'moderator_panel'
    except Exception:
        pass

    keyboard = get_admin_tickets_keyboard(
        ticket_data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        scope=scope,
        back_callback=back_cb,
    )
    from app.utils.photo_message import edit_or_answer_photo

    await edit_or_answer_photo(
        callback=callback,
        caption=header_text,
        keyboard=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def view_admin_ticket(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
    ticket_id: int | None = None,
):
    """Show admin ticket details with pagination."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return

    # Parse ticket_id and page from callback_data
    page = 1
    data_str = callback.data or ''

    if data_str.startswith('admin_ticket_page_'):
        # format: admin_ticket_page_{ticket_id}_{page}
        try:
            parts = data_str.split('_')
            ticket_id = int(parts[3])
            page = max(1, int(parts[4]))
        except (ValueError, IndexError):
            pass
    elif ticket_id is None:
        try:
            ticket_id = int(data_str.split('_')[-1])
        except (ValueError, AttributeError):
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'), show_alert=True)
            return

    if state is None:
        state = FSMContext(callback.bot, callback.from_user.id)

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True, load_user=True)

    if not ticket:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Build ticket header
    status_text = {
        TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'باز'),
        TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'پاسخ داده شده'),
        TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'بسته'),
        TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'در انتظار'),
    }.get(ticket.status, ticket.status)

    user_name = html.escape(ticket.user.full_name) if ticket.user else 'Unknown'
    telegram_id_display = (
        html.escape(str(ticket.user.telegram_id or ticket.user.email or f'#{ticket.user.id}')) if ticket.user else '—'
    )
    username_value = ticket.user.username if ticket.user else None
    id_label = 'Telegram ID' if (ticket.user and ticket.user.telegram_id) else 'ID'

    header = f'🎫 تیکت #{ticket.id}\n\n'
    header += f'👤 کاربر: {user_name}\n'
    header += f'🆔 {id_label}: <code>{telegram_id_display}</code>\n'
    if username_value:
        safe_username = html.escape(username_value)
        header += f'📱 Username: @{safe_username}\n'
    else:
        header += '📱 نام کاربری: موجود نیست\n'
    header += f'📝 موضوع: {html.escape(ticket.title)}\n'
    header += f'📊 وضعیت: {ticket.status_emoji} {status_text}\n'
    header += f'📅 ایجاد شده: {ticket.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    if ticket.is_user_reply_blocked:
        if ticket.user_reply_block_permanent:
            header += '🚫 کاربر به صورت دائمی مسدود شده است\n\n'
        elif ticket.user_reply_block_until:
            header += f'⏳ مسدود تا: {ticket.user_reply_block_until.strftime("%d.%m.%Y %H:%M")}\n\n'

    # Build message blocks
    message_blocks: list[str] = []
    if ticket.messages:
        message_blocks.append(f'💬 پیام‌ها ({len(ticket.messages)}):\n\n')
        for msg in ticket.messages:
            sender = '👤 کاربر' if msg.is_user_message else '🛠️ پشتیبانی'
            block = f'{sender} ({msg.created_at.strftime("%d.%m %H:%M")}):\n{html.escape(msg.message_text)}\n\n'
            if getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) == 'photo':
                block += '📎 پیوست: عکس\n\n'
            message_blocks.append(block)

    # Split into pages
    pages = _split_text_into_pages(header, message_blocks, max_len=MAX_MESSAGE_LEN)
    total_pages = len(pages)
    page = min(page, total_pages)

    # Build keyboard
    has_photos = any(
        getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo' for m in ticket.messages or []
    )
    keyboard = get_admin_ticket_view_keyboard(
        ticket_id, ticket.is_closed, db_user.language, is_user_blocked=ticket.is_user_reply_blocked
    )

    # User profile button
    try:
        if ticket.user:
            admin_profile_btn = types.InlineKeyboardButton(
                text='👤 به کاربر', callback_data=f'admin_user_manage_{ticket.user.id}_from_ticket_{ticket.id}'
            )
            keyboard.inline_keyboard.insert(0, [admin_profile_btn])
    except Exception:
        pass

    # DM and profile buttons
    try:
        if ticket.user and ticket.user.telegram_id and ticket.user.username:
            safe_username = html.escape(ticket.user.username)
            buttons_row = []
            pm_url = f'tg://resolve?domain={safe_username}'
            buttons_row.append(types.InlineKeyboardButton(text='✉ پیام مستقیم', url=pm_url))
            profile_url = f'tg://user?id={ticket.user.telegram_id}'
            buttons_row.append(types.InlineKeyboardButton(text='�� پروفایل', url=profile_url))
            if buttons_row:
                keyboard.inline_keyboard.insert(0, buttons_row)
    except Exception:
        pass

    # Attachments button
    if has_photos:
        try:
            keyboard.inline_keyboard.insert(
                0,
                [
                    types.InlineKeyboardButton(
                        text=texts.t('TICKET_ATTACHMENTS', '📎 پیوست‌ها'),
                        callback_data=f'admin_ticket_attachments_{ticket_id}',
                    )
                ],
            )
        except Exception:
            pass

    # Pagination
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_ticket_page_{ticket_id}_{page - 1}')
            )
        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(text='➡️', callback_data=f'admin_ticket_page_{ticket_id}_{page + 1}')
            )
        try:
            keyboard.inline_keyboard.insert(0, nav_row)
        except Exception:
            pass

    page_text = pages[page - 1]

    # Send message
    try:
        await callback.message.edit_text(page_text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(page_text, reply_markup=keyboard, parse_mode='HTML')

    # Save id for further actions
    if state is not None:
        try:
            await state.update_data(ticket_id=ticket_id)
        except Exception:
            pass
    await callback.answer()


async def reply_to_admin_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Start an admin reply to a ticket."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_reply_ticket_', ''))

    await state.update_data(ticket_id=ticket_id, reply_mode=True)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_TICKET_REPLY_INPUT', 'پاسخ پشتیبانی را وارد کنید:'),
        reply_markup=get_admin_ticket_reply_cancel_keyboard(db_user.language),
    )

    await state.set_state(AdminTicketStates.waiting_for_reply)
    await callback.answer()


async def handle_admin_ticket_reply(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    if not (settings.is_admin(message.from_user.id) or SupportSettingsService.is_moderator(message.from_user.id)):
        texts = get_texts(db_user.language)
        await message.answer(texts.ACCESS_DENIED)
        await state.clear()
        return
    # Check that the user is in the correct state
    current_state = await state.get_state()
    if current_state != AdminTicketStates.waiting_for_reply:
        return

    # Anti-spam: one message per short window per specific ticket
    try:
        data_rl = await state.get_data()
        rl_ticket_id = data_rl.get('ticket_id') or 'admin_reply'
        limited = await RateLimitCache.is_rate_limited(
            db_user.id, f'admin_ticket_reply_{rl_ticket_id}', limit=1, window=2
        )
        if limited:
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('admin_rl_ts_reply')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            return
        await state.update_data(admin_rl_ts_reply=now_ts)
    except Exception:
        pass

    """Process the admin reply to a ticket."""
    # Support photo attachments in admin reply
    reply_text = (message.text or message.caption or '').strip()
    if len(reply_text) > 400:
        reply_text = reply_text[:400]
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption

    if len(reply_text) < 1 and not media_file_id:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_TOO_SHORT', 'پاسخ باید حداقل ۵ کاراکتر داشته باشد. دوباره امتحان کنید:')
        )
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')
    try:
        ticket_id = int(ticket_id) if ticket_id is not None else None
    except (TypeError, ValueError):
        ticket_id = None

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'خطا: شناسه تیکت یافت نشد.'))
        await state.clear()
        return

    try:
        # If this is block duration input mode
        if not data.get('reply_mode'):
            try:
                minutes = int(reply_text)
                minutes = max(1, min(60 * 24 * 365, minutes))
            except ValueError:
                await message.answer('❌ یک عدد صحیح برای دقیقه وارد کنید')
                return
            until = datetime.now(UTC) + timedelta(minutes=minutes)
            ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=until)
            if ok:
                await message.answer(f'✅ کاربر برای {minutes} دقیقه مسدود شد')
            else:
                await message.answer('❌ خطا در مسدودسازی')
            await state.clear()
            return

        # Normal admin reply mode
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
        if not ticket:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'))
            await state.clear()
            return

        # Add message from admin (inside add_message status becomes ANSWERED)
        await TicketMessageCRUD.add_message(
            db,
            ticket_id,
            db_user.id,
            reply_text,
            is_from_admin=True,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )

        texts = get_texts(db_user.language)

        await message.answer(
            texts.t('ADMIN_TICKET_REPLY_SENT', '✅ پاسخ ارسال شد!'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_TICKET', '👁️ مشاهده تیکت'),
                            callback_data=f'admin_view_ticket_{ticket_id}',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_TICKETS', '⬅️ به تیکت‌ها'), callback_data='admin_tickets'
                        )
                    ],
                ]
            ),
        )

        await state.clear()

        # Notify user about new reply
        await notify_user_about_ticket_reply(message.bot, ticket, reply_text, db)
        # Admin notifications about ticket replies are disabled by requirement

    except Exception as e:
        logger.error('Error adding admin ticket reply', error=e)
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_ERROR', '❌ خطایی در ارسال پاسخ رخ داد. بعداً دوباره امتحان کنید.')
        )


async def mark_ticket_as_answered(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Mark ticket as answered."""
    ticket_id = int(callback.data.replace('admin_mark_answered_', ''))

    try:
        success = await TicketCRUD.update_ticket_status(db, ticket_id, TicketStatus.ANSWERED.value)

        if success:
            texts = get_texts(db_user.language)
            await callback.answer(
                texts.t('TICKET_MARKED_ANSWERED', '✅ تیکت به عنوان پاسخ داده شده علامت‌گذاری شد.'), show_alert=True
            )

            # Update message
            await view_admin_ticket(callback, db_user, db, state)
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ خطا در به‌روزرسانی تیکت.'), show_alert=True)

    except Exception as e:
        logger.error('Error marking ticket as answered', error=e)
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ خطا در به‌روزرسانی تیکت.'), show_alert=True)


async def close_all_open_admin_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Close all open tickets."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return

    texts = get_texts(db_user.language)

    try:
        closed_ticket_ids = await TicketCRUD.close_all_open_tickets(db)
    except Exception as error:
        logger.error('Error closing all open tickets', error=error)
        await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ خطا در به‌روزرسانی تیکت.'), show_alert=True)
        return

    closed_count = len(closed_ticket_ids)

    if closed_count == 0:
        await callback.answer(
            texts.t('ADMIN_CLOSE_ALL_OPEN_TICKETS_EMPTY', 'ℹ️ تیکت باز برای بستن وجود ندارد.'), show_alert=True
        )
        return

    try:
        is_moderator = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
            callback.from_user.id
        )
        await TicketCRUD.add_support_audit(
            db,
            actor_user_id=db_user.id if db_user else None,
            actor_telegram_id=callback.from_user.id,
            is_moderator=is_moderator,
            action='close_all_tickets',
            ticket_id=None,
            target_user_id=None,
            details={
                'count': closed_count,
                'ticket_ids': closed_ticket_ids,
            },
        )
    except Exception as audit_error:
        logger.warning('Failed to add support audit for bulk close', audit_error=audit_error)

    # Update ticket list
    await show_admin_tickets(callback, db_user, db)

    success_text = texts.t('ADMIN_CLOSE_ALL_OPEN_TICKETS_SUCCESS', '✅ تیکت‌های باز بسته شده: {count}').format(
        count=closed_count
    )

    notification_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 حذف', callback_data='admin_support_delete_msg')]]
    )

    try:
        await callback.message.answer(success_text, reply_markup=notification_keyboard)
    except Exception:
        # If unable to send a separate message, try to respond with an alert
        try:
            await callback.answer(success_text, show_alert=True)
        except Exception:
            pass


async def close_admin_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Close ticket by admin."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_close_ticket_', ''))

    try:
        success = await TicketCRUD.close_ticket(db, ticket_id)

        if success:
            # audit
            try:
                is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                    callback.from_user.id
                )
                # enrich details with ticket user contacts
                details = {}
                try:
                    t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                    if t and t.user:
                        details.update(
                            {
                                'target_telegram_id': t.user.telegram_id,
                                'target_username': t.user.username,
                            }
                        )
                except Exception:
                    pass
                await TicketCRUD.add_support_audit(
                    db,
                    actor_user_id=db_user.id if db_user else None,
                    actor_telegram_id=callback.from_user.id,
                    is_moderator=is_mod,
                    action='close_ticket',
                    ticket_id=ticket_id,
                    target_user_id=None,
                    details=details,
                )
            except Exception:
                pass
            texts = get_texts(db_user.language)
            # Notify with deletable inline message
            try:
                await callback.message.answer(
                    texts.t('TICKET_CLOSED', '✅ تیکت بسته شد.'),
                    reply_markup=types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [types.InlineKeyboardButton(text='🗑 حذف', callback_data='admin_support_delete_msg')]
                        ]
                    ),
                )
            except Exception:
                await callback.answer(texts.t('TICKET_CLOSED', '✅ تیکت بسته شد.'), show_alert=True)

            # Update inline keyboard in current message without action buttons
            await callback.message.edit_reply_markup(
                reply_markup=get_admin_ticket_view_keyboard(ticket_id, True, db_user.language)
            )
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ خطا در بستن تیکت.'), show_alert=True)

    except Exception as e:
        logger.error('Error closing admin ticket', error=e)
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ خطا در بستن تیکت.'), show_alert=True)


async def cancel_admin_ticket_reply(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Cancel admin reply to ticket."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_REPLY_CANCELLED', 'پاسخ لغو شد.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_TICKETS', '⬅️ به تیکت‌ها'), callback_data='admin_tickets'
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def block_user_in_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_block_user_ticket_', ''))
    texts = get_texts(db_user.language)
    # Save original ticket message ids to update it after blocking without reopening
    try:
        await state.update_data(origin_chat_id=callback.message.chat.id, origin_message_id=callback.message.message_id)
    except Exception:
        pass
    await callback.message.edit_text(
        texts.t('ENTER_BLOCK_MINUTES', 'تعداد دقیقه‌های مسدودسازی کاربر را وارد کنید (مثلاً 15):'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CANCEL_REPLY', '❌ لغو ورودی'), callback_data='cancel_admin_ticket_reply'
                    )
                ]
            ]
        ),
    )
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(AdminTicketStates.waiting_for_block_duration)
    await callback.answer()


async def handle_admin_block_duration_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # permission gate for message flow
    if not (settings.is_admin(message.from_user.id) or SupportSettingsService.is_moderator(message.from_user.id)):
        texts = get_texts(db_user.language)
        await message.answer(texts.ACCESS_DENIED)
        await state.clear()
        return
    # Check state
    current_state = await state.get_state()
    if current_state != AdminTicketStates.waiting_for_block_duration:
        return

    reply_text = message.text.strip()
    if len(reply_text) < 1:
        await message.answer('❌ یک عدد صحیح برای دقیقه وارد کنید')
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')
    origin_chat_id = data.get('origin_chat_id')
    origin_message_id = data.get('origin_message_id')
    try:
        minutes = int(reply_text)
        minutes = max(1, min(60 * 24 * 365, minutes))  # maximum 1 year
    except ValueError:
        await message.answer('❌ یک عدد صحیح برای دقیقه وارد کنید')
        return

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'خطا: شناسه تیکت یافت نشد.'))
        await state.clear()
        return

    try:
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'))
            await state.clear()
            return

        until = datetime.now(UTC) + timedelta(minutes=minutes)
        ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=until)
        if not ok:
            await message.answer('❌ خطا در مسدودسازی')
            return
        # audit
        try:
            is_mod = not settings.is_admin(message.from_user.id) and SupportSettingsService.is_moderator(
                message.from_user.id
            )
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=message.from_user.id,
                is_moderator=is_mod,
                action='block_user_timed',
                ticket_id=ticket_id,
                target_user_id=ticket.user_id if ticket else None,
                details={'minutes': minutes},
            )
        except Exception:
            pass
        # Refresh original ticket card (caption/text and buttons) in place
        try:
            updated = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True, load_user=True)
            texts = get_texts(db_user.language)
            status_text = {
                TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'باز'),
                TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'پاسخ داده شده'),
                TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'بسته'),
                TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'در انتظار'),
            }.get(updated.status, updated.status)
            user_name = html.escape(updated.user.full_name) if updated.user else 'Unknown'
            ticket_text = f'🎫 تیکت #{updated.id}\n\n'
            ticket_text += f'👤 کاربر: {user_name}\n'
            ticket_text += f'📝 موضوع: {html.escape(updated.title)}\n'
            ticket_text += f'📊 وضعیت: {updated.status_emoji} {status_text}\n'
            ticket_text += f'📅 ایجاد شده: {updated.created_at.strftime("%d.%m.%Y %H:%M")}\n'
            ticket_text += f'🔄 به‌روزشده: {updated.updated_at.strftime("%d.%m.%Y %H:%M")}\n'
            if updated.user and updated.user.telegram_id:
                ticket_text += f'🆔 Telegram ID: <code>{updated.user.telegram_id}</code>\n'
                if updated.user.username:
                    safe_username = html.escape(updated.user.username)
                    ticket_text += f'📱 Username: @{safe_username}\n'
                    ticket_text += (
                        f'🔗 پیام مستقیم: <a href="tg://resolve?domain={safe_username}">'
                        f'tg://resolve?domain={safe_username}</a>\n'
                    )
                else:
                    ticket_text += '📱 نام کاربری: موجود نیست\n'
                    chat_link = f'tg://user?id={int(updated.user.telegram_id)}'
                    ticket_text += f'🔗 چت با شناسه: <a href="{chat_link}">{chat_link}</a>\n'
            elif updated.user:
                # Email-only user
                user_id_display = html.escape(str(updated.user.email or f'#{updated.user.id}'))
                ticket_text += f'🆔 ID: <code>{user_id_display}</code>\n'
                ticket_text += '📧 نوع: کاربر ایمیل\n'
            ticket_text += '\n'
            if updated.is_user_reply_blocked:
                if updated.user_reply_block_permanent:
                    ticket_text += '🚫 کاربر برای ارسال پاسخ در این تیکت به صورت دائمی مسدود شده است\n'
                elif updated.user_reply_block_until:
                    ticket_text += f'⏳ مسدود تا: {updated.user_reply_block_until.strftime("%d.%m.%Y %H:%M")}\n'
            if updated.messages:
                ticket_text += f'💬 پیام‌ها ({len(updated.messages)}):\n\n'
                for msg in updated.messages:
                    sender = '👤 کاربر' if msg.is_user_message else '🛠️ پشتیبانی'
                    ticket_text += f'{sender} ({msg.created_at.strftime("%d.%m %H:%M")}):\n'
                    ticket_text += f'{html.escape(msg.message_text)}\n\n'
                    if getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) == 'photo':
                        ticket_text += '📎 پیوست: عکس\n\n'

            kb = get_admin_ticket_view_keyboard(
                updated.id, updated.is_closed, db_user.language, is_user_blocked=updated.is_user_reply_blocked
            )
            # Button to open user profile in admin panel
            try:
                if updated.user:
                    admin_profile_btn = types.InlineKeyboardButton(
                        text='👤 به کاربر',
                        callback_data=f'admin_user_manage_{updated.user.id}_from_ticket_{updated.id}',
                    )
                    kb.inline_keyboard.insert(0, [admin_profile_btn])
            except Exception:
                pass
            # DM and profile buttons when updating card
            try:
                if updated.user and updated.user.telegram_id and updated.user.username:
                    safe_username = html.escape(updated.user.username)
                    buttons_row = []
                    pm_url = f'tg://resolve?domain={safe_username}'
                    buttons_row.append(types.InlineKeyboardButton(text='✉ پیام مستقیم', url=pm_url))
                    profile_url = f'tg://user?id={updated.user.telegram_id}'
                    buttons_row.append(types.InlineKeyboardButton(text='�� پروفایل', url=profile_url))
                    if buttons_row:
                        kb.inline_keyboard.insert(0, buttons_row)
            except Exception:
                pass
            has_photos = any(
                getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo'
                for m in updated.messages or []
            )
            if has_photos:
                try:
                    kb.inline_keyboard.insert(
                        0,
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('TICKET_ATTACHMENTS', '📎 پیوست‌ها'),
                                callback_data=f'admin_ticket_attachments_{updated.id}',
                            )
                        ],
                    )
                except Exception:
                    pass
            if origin_chat_id and origin_message_id:
                try:
                    await message.bot.edit_message_caption(
                        chat_id=origin_chat_id,
                        message_id=origin_message_id,
                        caption=ticket_text,
                        reply_markup=kb,
                        parse_mode='HTML',
                    )
                except Exception:
                    try:
                        await message.bot.edit_message_text(
                            chat_id=origin_chat_id,
                            message_id=origin_message_id,
                            text=ticket_text,
                            reply_markup=kb,
                            parse_mode='HTML',
                        )
                    except Exception:
                        await message.answer(f'✅ کاربر برای {minutes} دقیقه مسدود شد')
            else:
                await message.answer(f'✅ کاربر برای {minutes} دقیقه مسدود شد')
        except Exception:
            await message.answer(f'✅ کاربر برای {minutes} دقیقه مسدود شد')
        finally:
            await state.clear()
    except Exception as e:
        logger.error('Error setting block duration', error=e)
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', '❌ خطایی رخ داد. بعداً دوباره امتحان کنید.'))


async def unblock_user_in_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_unblock_user_ticket_', ''))
    ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=None)
    if ok:
        try:
            await callback.message.answer(
                '✅ مسدودیت برداشته شد',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🗑 حذف', callback_data='admin_support_delete_msg')]
                    ]
                ),
            )
        except Exception:
            await callback.answer('✅ مسدودیت برداشته شد')
        # audit
        try:
            is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                callback.from_user.id
            )
            ticket_id = int(callback.data.replace('admin_unblock_user_ticket_', ''))
            details = {}
            try:
                t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                if t and t.user:
                    details.update(
                        {
                            'target_telegram_id': t.user.telegram_id,
                            'target_username': t.user.username,
                        }
                    )
            except Exception:
                pass
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=callback.from_user.id,
                is_moderator=is_mod,
                action='unblock_user',
                ticket_id=ticket_id,
                target_user_id=None,
                details=details,
            )
        except Exception:
            pass
        await view_admin_ticket(callback, db_user, db, state)
    else:
        await callback.answer('❌ خطا', show_alert=True)


async def block_user_permanently(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_block_user_perm_ticket_', ''))
    ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=True, until=None)
    if ok:
        try:
            await callback.message.answer(
                '✅ کاربر به صورت دائمی مسدود شد',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🗑 حذف', callback_data='admin_support_delete_msg')]
                    ]
                ),
            )
        except Exception:
            await callback.answer('✅ کاربر مسدود شد')
        # audit
        try:
            is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                callback.from_user.id
            )
            details = {}
            try:
                t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                if t and t.user:
                    details.update(
                        {
                            'target_telegram_id': t.user.telegram_id,
                            'target_username': t.user.username,
                        }
                    )
            except Exception:
                pass
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=callback.from_user.id,
                is_moderator=is_mod,
                action='block_user_perm',
                ticket_id=ticket_id,
                target_user_id=None,
                details=details,
            )
        except Exception:
            pass
        await view_admin_ticket(callback, db_user, db, state)
    else:
        await callback.answer('❌ خطا', show_alert=True)


async def notify_user_about_ticket_reply(bot: Bot, ticket: Ticket, reply_text: str, db: AsyncSession):
    """Notify user about a new reply in the ticket."""
    try:
        # Respect runtime toggle for user ticket notifications
        try:
            if not SupportSettingsService.get_user_ticket_notifications_enabled():
                return
        except Exception:
            pass
        from app.localization.texts import get_texts

        # Ensure user data is present in the ticket object
        ticket_with_user = ticket
        if not getattr(ticket_with_user, 'user', None):
            ticket_with_user = await TicketCRUD.get_ticket_by_id(db, ticket.id, load_user=True)

        user = getattr(ticket_with_user, 'user', None)
        if not user:
            logger.error('User not found for ticket #', ticket_id=ticket.id)
            return

        if not getattr(user, 'telegram_id', None):
            logger.warning(
                'Cannot notify ticket # user without telegram_id (username auth_type=)',
                ticket_id=ticket.id,
                getattr=getattr(user, 'username', None),
                getattr_2=getattr(user, 'auth_type', None),
            )
            return

        chat_id = int(user.telegram_id)
        texts = get_texts(user.language)

        # Build notification
        base_text = texts.t(
            'TICKET_REPLY_NOTIFICATION',
            '🎫 پاسخ جدید برای تیکت #{ticket_id}\n\n{reply_preview}\n\nدکمه زیر را برای مشاهده تیکت بفشارید:',
        ).format(ticket_id=ticket.id, reply_preview=reply_text[:100] + '...' if len(reply_text) > 100 else reply_text)
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('VIEW_TICKET', '👁️ مشاهده تیکت'), callback_data=f'view_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CLOSE_NOTIFICATION', '❌ بستن اعلان'),
                        callback_data=f'close_ticket_notification_{ticket.id}',
                    )
                ],
            ]
        )

        # If the last admin reply had a photo — send as photo
        last_message = await TicketMessageCRUD.get_last_message(db, ticket.id)
        if (
            last_message
            and last_message.has_media
            and last_message.media_type == 'photo'
            and last_message.is_from_admin
        ):
            caption = base_text
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=last_message.media_file_id,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except TelegramBadRequest as photo_error:
                logger.error(
                    'Failed to send photo notification to user for ticket',
                    chat_id=chat_id,
                    ticket_id=ticket.id,
                    photo_error=photo_error,
                )
            except Exception as e:
                logger.error('Failed to send photo notification', error=e)
        # Fallback: text notification
        await bot.send_message(
            chat_id=chat_id,
            text=base_text,
            reply_markup=keyboard,
        )

        logger.info('Ticket # reply notification sent to user', ticket_id=ticket.id, chat_id=chat_id)

    except Exception as e:
        logger.error('Error notifying user about ticket reply', error=e)


def register_handlers(dp: Dispatcher):
    """Register admin ticket handlers."""

    # View tickets
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets')
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets_scope_open')
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets_scope_closed')
    dp.callback_query.register(close_all_open_admin_tickets, F.data == 'admin_tickets_close_all_open')

    dp.callback_query.register(view_admin_ticket, F.data.startswith('admin_view_ticket_'))
    dp.callback_query.register(view_admin_ticket, F.data.startswith('admin_ticket_page_'))

    # Ticket replies
    dp.callback_query.register(reply_to_admin_ticket, F.data.startswith('admin_reply_ticket_'))

    dp.message.register(handle_admin_ticket_reply, AdminTicketStates.waiting_for_reply)
    dp.message.register(handle_admin_block_duration_input, AdminTicketStates.waiting_for_block_duration)

    # Status management: explicit button no longer used (status changes automatically)

    dp.callback_query.register(close_admin_ticket, F.data.startswith('admin_close_ticket_'))
    dp.callback_query.register(block_user_in_ticket, F.data.startswith('admin_block_user_ticket_'))
    dp.callback_query.register(unblock_user_in_ticket, F.data.startswith('admin_unblock_user_ticket_'))
    dp.callback_query.register(block_user_permanently, F.data.startswith('admin_block_user_perm_ticket_'))

    # Cancel operations
    dp.callback_query.register(cancel_admin_ticket_reply, F.data == 'cancel_admin_ticket_reply')

    # Admin ticket pagination
    dp.callback_query.register(show_admin_tickets, F.data.startswith('admin_tickets_page_'))

    # Reply layout management — (disabled)

    # Ticket attachments (admin)
    async def send_admin_ticket_attachments(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
        # permission gate for attachments view
        if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
            texts = get_texts(db_user.language)
            await callback.answer(texts.ACCESS_DENIED, show_alert=True)
            return
        texts = get_texts(db_user.language)
        try:
            ticket_id = int(callback.data.replace('admin_ticket_attachments_', ''))
        except ValueError:
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'), show_alert=True)
            return
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)
        if not ticket:
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'تیکت یافت نشد.'), show_alert=True)
            return
        photos = [
            m.media_file_id
            for m in ticket.messages
            if getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo' and m.media_file_id
        ]
        if not photos:
            await callback.answer(texts.t('NO_ATTACHMENTS', 'پیوستی وجود ندارد.'), show_alert=True)
            return
        from aiogram.types import InputMediaPhoto

        chunks = [photos[i : i + 10] for i in range(0, len(photos), 10)]
        last_group_message = None
        for chunk in chunks:
            media = [InputMediaPhoto(media=pid) for pid in chunk]
            try:
                messages = await callback.message.bot.send_media_group(chat_id=callback.from_user.id, media=media)
                if messages:
                    last_group_message = messages[-1]
            except Exception:
                pass
        # After sending, add a delete button below the last message of the group
        if last_group_message:
            try:
                kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('DELETE_MESSAGE', '🗑 حذف'),
                                callback_data=f'admin_delete_message_{last_group_message.message_id}',
                            )
                        ]
                    ]
                )
                await callback.message.bot.send_message(
                    chat_id=callback.from_user.id,
                    text=texts.t('ATTACHMENTS_SENT', 'پیوست‌ها ارسال شدند.'),
                    reply_markup=kb,
                )
            except Exception:
                await callback.answer(texts.t('ATTACHMENTS_SENT', 'پیوست‌ها ارسال شدند.'))
        else:
            await callback.answer(texts.t('ATTACHMENTS_SENT', 'پیوست‌ها ارسال شدند.'))

    dp.callback_query.register(send_admin_ticket_attachments, F.data.startswith('admin_ticket_attachments_'))

    async def admin_delete_message(callback: types.CallbackQuery):
        try:
            msg_id = int(callback.data.replace('admin_delete_message_', ''))
        except ValueError:
            await callback.answer('❌')
            return
        try:
            await callback.message.bot.delete_message(chat_id=callback.from_user.id, message_id=msg_id)
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer('✅')

    dp.callback_query.register(admin_delete_message, F.data.startswith('admin_delete_message_'))
