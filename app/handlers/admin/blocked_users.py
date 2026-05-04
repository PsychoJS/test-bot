"""
Admin panel handlers for managing blocked users.

Allows scanning users, identifying those who blocked the bot,
and performing cleanup of the DB and Remnawave panel.
"""

import html
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.blocked_users_service import (
    BlockCheckResult,
    BlockedUserAction,
    BlockedUsersService,
)
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


# =============================================================================
# Enums for texts and callback_data
# =============================================================================


class BlockedUsersText(Enum):
    """Text messages for the blocked users module."""

    MENU_TITLE = '🔒 <b>بررسی کاربران مسدودشده</b>'
    MENU_DESCRIPTION = (
        '\n\nاینجا می‌توانید بررسی کنید کدام کاربران ربات را مسدود کرده‌اند، '
        'و آن‌ها را از پایگاه داده و پنل Remnawave حذف کنید.\n\n'
        '<b>نحوه عملکرد:</b>\n'
        '1. اسکن یک درخواست آزمایشی برای هر کاربر ارسال می‌کند\n'
        '2. اگر کاربر ربات را مسدود کرده باشد - خطا دریافت می‌کنیم\n'
        '3. می‌توان چنین کاربرانی را از دیتابیس و/یا Remnawave حذف کرد'
    )

    SCAN_STARTED = '🔄 <b>اسکن شروع شد...</b>\n\nاین ممکن است چند دقیقه طول بکشد.'
    SCAN_PROGRESS = '🔄 <b>اسکن:</b> {checked}/{total} ({percent}%)'
    SCAN_COMPLETE = (
        '✅ <b>اسکن کامل شد</b>\n\n'
        '📊 <b>نتایج:</b>\n'
        '• بررسی‌شده: {total_checked}\n'
        '• ربات را مسدود کرده‌اند: {blocked_count}\n'
        '• فعال: {active_users}\n'
        '• خطاها: {errors}\n'
        '• بدون شناسه تلگرام: {skipped}\n\n'
        '⏱ زمان اسکن: {duration:.1f}s'
    )
    SCAN_NO_BLOCKED = '✅ <b>عالی!</b>\n\nهیچ کاربری که ربات را مسدود کرده باشد پیدا نشد.'

    BLOCKED_LIST_TITLE = '🔒 <b>کاربران مسدودشده</b> ({count})\n\n'
    BLOCKED_USER_ROW = '• {name} (ID: <code>{telegram_id}</code>)\n'

    CLEANUP_CONFIRM_TITLE = '⚠️ <b>تأیید عملیات</b>\n\n'
    CLEANUP_CONFIRM_DELETE_DB = (
        'می‌خواهید <b>از دیتابیس حذف کنید</b> {count} کاربر را.\n'
        'این عملیات غیرقابل بازگشت است!\n\n'
        'موارد حذف‌شده:\n'
        '• پروفایل کاربران\n'
        '• اشتراک‌ها\n'
        '• تراکنش‌ها\n'
        '• داده‌های معرف'
    )
    CLEANUP_CONFIRM_DELETE_REMNAWAVE = (
        'می‌خواهید <b>از Remnawave حذف کنید</b> {count} کاربر را.\nدسترسی VPN آن‌ها کاملاً غیرفعال خواهد شد.'
    )
    CLEANUP_CONFIRM_DELETE_BOTH = (
        'می‌خواهید <b>به‌طور کامل حذف کنید</b> {count} کاربر را:\n'
        '• از پایگاه داده ربات\n'
        '• از پنل Remnawave\n\n'
        'این عملیات غیرقابل بازگشت است!'
    )
    CLEANUP_CONFIRM_MARK = (
        'می‌خواهید <b>به‌عنوان مسدودشده علامت‌گذاری کنید</b> {count} کاربر را.\n'
        'آن‌ها در دیتابیس باقی می‌مانند، اما با وضعیت "blocked" علامت‌گذاری می‌شوند.'
    )

    CLEANUP_PROGRESS = '🗑 <b>پاکسازی:</b> {processed}/{total}'
    CLEANUP_COMPLETE = (
        '✅ <b>پاکسازی کامل شد</b>\n\n'
        '📊 <b>نتایج:</b>\n'
        '• حذف‌شده از دیتابیس: {deleted_db}\n'
        '• حذف‌شده از Remnawave: {deleted_remnawave}\n'
        '• علامت‌گذاری‌شده به‌عنوان مسدود: {marked}\n'
        '• خطاها: {errors}'
    )

    BUTTON_START_SCAN = '🔍 شروع اسکن'
    BUTTON_VIEW_BLOCKED = '👥 لیست مسدودشدگان ({count})'
    BUTTON_DELETE_DB = '🗑 حذف از دیتابیس'
    BUTTON_DELETE_REMNAWAVE = '🌐 حذف از Remnawave'
    BUTTON_DELETE_BOTH = '💀 حذف از همه‌جا'
    BUTTON_MARK_BLOCKED = '🚫 علامت‌گذاری به‌عنوان مسدود'
    BUTTON_CONFIRM = '✅ تأیید'
    BUTTON_CANCEL = '❌ لغو'
    BUTTON_BACK = '⬅️ بازگشت'
    BUTTON_BACK_TO_USERS = '⬅️ بازگشت به کاربران'


class BlockedUsersCallback(Enum):
    """Callback data for module buttons."""

    MENU = 'admin_blocked_users'
    START_SCAN = 'admin_blocked_scan'
    VIEW_LIST = 'admin_blocked_list'
    VIEW_LIST_PAGE = 'admin_blocked_list_page_'
    ACTION_DELETE_DB = 'admin_blocked_action_db'
    ACTION_DELETE_REMNAWAVE = 'admin_blocked_action_rw'
    ACTION_DELETE_BOTH = 'admin_blocked_action_both'
    ACTION_MARK = 'admin_blocked_action_mark'
    CONFIRM_PREFIX = 'admin_blocked_confirm_'
    CANCEL = 'admin_blocked_cancel'


# =============================================================================
# FSM States
# =============================================================================


class BlockedUsersStates(StatesGroup):
    """FSM states for the blocked users module."""

    scanning = State()
    viewing_results = State()
    confirming_action = State()
    processing_cleanup = State()


# =============================================================================
# Keyboards
# =============================================================================


def get_blocked_users_menu_keyboard(
    scan_result: dict[str, Any] | None = None,
) -> InlineKeyboardMarkup:
    """Main menu keyboard for the module."""
    buttons = [
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_START_SCAN.value,
                callback_data=BlockedUsersCallback.START_SCAN.value,
            )
        ]
    ]

    blocked_count = scan_result.get('blocked_count', 0) if scan_result else 0
    if blocked_count > 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_VIEW_BLOCKED.value.format(count=blocked_count),
                    callback_data=BlockedUsersCallback.VIEW_LIST.value,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_BACK_TO_USERS.value,
                callback_data='admin_users',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_blocked_list_keyboard(
    page: int = 1,
    total_pages: int = 1,
    has_blocked: bool = True,
) -> InlineKeyboardMarkup:
    """Keyboard for the blocked users list."""
    buttons = []

    # Pagination
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text='⬅️',
                    callback_data=f'{BlockedUsersCallback.VIEW_LIST_PAGE.value}{page - 1}',
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f'{page}/{total_pages}',
                callback_data='noop',
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text='➡️',
                    callback_data=f'{BlockedUsersCallback.VIEW_LIST_PAGE.value}{page + 1}',
                )
            )
        buttons.append(nav_row)

    # Actions
    if has_blocked:
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_DB.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_DB.value,
                    ),
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_REMNAWAVE.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_REMNAWAVE.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_DELETE_BOTH.value,
                        callback_data=BlockedUsersCallback.ACTION_DELETE_BOTH.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=BlockedUsersText.BUTTON_MARK_BLOCKED.value,
                        callback_data=BlockedUsersCallback.ACTION_MARK.value,
                    ),
                ],
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=BlockedUsersText.BUTTON_BACK.value,
                callback_data=BlockedUsersCallback.MENU.value,
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_keyboard(action: BlockedUserAction) -> InlineKeyboardMarkup:
    """Confirmation keyboard for an action."""
    action_map = {
        BlockedUserAction.DELETE_FROM_DB: 'db',
        BlockedUserAction.DELETE_FROM_REMNAWAVE: 'rw',
        BlockedUserAction.DELETE_BOTH: 'both',
        BlockedUserAction.MARK_AS_BLOCKED: 'mark',
    }

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_CONFIRM.value,
                    callback_data=f'{BlockedUsersCallback.CONFIRM_PREFIX.value}{action_map[action]}',
                ),
                InlineKeyboardButton(
                    text=BlockedUsersText.BUTTON_CANCEL.value,
                    callback_data=BlockedUsersCallback.CANCEL.value,
                ),
            ]
        ]
    )


# =============================================================================
# Handlers
# =============================================================================


@admin_required
@error_handler
async def show_blocked_users_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Shows the main menu of the blocked users module."""
    data = await state.get_data()
    scan_result = data.get('blocked_users_scan_result')

    text = BlockedUsersText.MENU_TITLE.value + BlockedUsersText.MENU_DESCRIPTION.value

    if scan_result:
        text += (
            f'\n\n📊 <b>آخرین اسکن:</b>\n'
            f'• مسدودشده: {scan_result.get("blocked_count", 0)}\n'
            f'• فعال: {scan_result.get("active_users", 0)}'
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(scan_result),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_scan(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Starts scanning users."""
    await state.set_state(BlockedUsersStates.scanning)

    # Send the initial message
    await callback.message.edit_text(
        BlockedUsersText.SCAN_STARTED.value,
        parse_mode=ParseMode.HTML,
    )

    service = BlockedUsersService(bot)
    last_update_time = datetime.now(tz=UTC)

    async def progress_callback(checked: int, total: int) -> None:
        nonlocal last_update_time
        now = datetime.now(tz=UTC)
        # Update the message no more than once every 3 seconds
        if (now - last_update_time).total_seconds() >= 3:
            last_update_time = now
            percent = int(checked / total * 100) if total > 0 else 0
            try:
                await callback.message.edit_text(
                    BlockedUsersText.SCAN_PROGRESS.value.format(
                        checked=checked,
                        total=total,
                        percent=percent,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # Ignore message update errors

    # Perform the scan
    result = await service.scan_all_users(
        db,
        only_active=True,
        progress_callback=progress_callback,
    )

    # Serialize the result to a dict for Redis and the keyboard
    scan_result_dict = {
        'total_checked': result.total_checked,
        'blocked_count': result.blocked_count,
        'active_users': result.active_users,
        'errors': result.errors,
        'skipped_no_telegram': result.skipped_no_telegram,
        'scan_duration_seconds': result.scan_duration_seconds,
    }

    # Save the result to state
    await state.update_data(
        blocked_users_scan_result=scan_result_dict,
        blocked_users_list=[
            {
                'user_id': u.user_id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'full_name': u.full_name,
                'remnawave_uuid': u.remnawave_uuid,
            }
            for u in result.blocked_users
        ],
    )

    await state.set_state(BlockedUsersStates.viewing_results)

    # Build the final message
    if result.blocked_count == 0:
        text = BlockedUsersText.SCAN_NO_BLOCKED.value
    else:
        text = BlockedUsersText.SCAN_COMPLETE.value.format(
            total_checked=result.total_checked,
            blocked_count=result.blocked_count,
            active_users=result.active_users,
            errors=result.errors,
            skipped=result.skipped_no_telegram,
            duration=result.scan_duration_seconds,
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(scan_result_dict),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_blocked_list(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    page: int = 1,
) -> None:
    """Shows the list of blocked users."""
    data = await state.get_data()
    blocked_list: list[dict[str, Any]] = data.get('blocked_users_list', [])

    if not blocked_list:
        await callback.answer('کاربر مسدودشده‌ای وجود ندارد', show_alert=True)
        return

    # Pagination
    per_page = 15
    total_pages = (len(blocked_list) + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_users = blocked_list[start_idx:end_idx]

    text = BlockedUsersText.BLOCKED_LIST_TITLE.value.format(count=len(blocked_list))

    for user_data in page_users:
        name = user_data.get('full_name') or user_data.get('username') or 'بدون نام'
        telegram_id = user_data.get('telegram_id', '?')
        text += BlockedUsersText.BLOCKED_USER_ROW.value.format(
            name=html.escape(name),
            telegram_id=telegram_id,
        )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_list_keyboard(page, total_pages, bool(blocked_list)),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_blocked_list_pagination(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Handles pagination of the blocked list."""
    try:
        page = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        page = 1

    await show_blocked_list(callback, db_user, state, page)


@admin_required
@error_handler
async def show_action_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    action: BlockedUserAction,
) -> None:
    """Shows action confirmation."""
    data = await state.get_data()
    blocked_list = data.get('blocked_users_list', [])
    count = len(blocked_list)

    if count == 0:
        await callback.answer('کاربری برای پردازش وجود ندارد', show_alert=True)
        return

    await state.set_state(BlockedUsersStates.confirming_action)
    await state.update_data(pending_action=action.value)

    text = BlockedUsersText.CLEANUP_CONFIRM_TITLE.value

    if action == BlockedUserAction.DELETE_FROM_DB:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_DB.value.format(count=count)
    elif action == BlockedUserAction.DELETE_FROM_REMNAWAVE:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_REMNAWAVE.value.format(count=count)
    elif action == BlockedUserAction.DELETE_BOTH:
        text += BlockedUsersText.CLEANUP_CONFIRM_DELETE_BOTH.value.format(count=count)
    elif action == BlockedUserAction.MARK_AS_BLOCKED:
        text += BlockedUsersText.CLEANUP_CONFIRM_MARK.value.format(count=count)

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_confirm_keyboard(action),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_action_delete_db(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Handles the selection to delete from DB."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_FROM_DB)


@admin_required
@error_handler
async def handle_action_delete_remnawave(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Handles the selection to delete from Remnawave."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_FROM_REMNAWAVE)


@admin_required
@error_handler
async def handle_action_delete_both(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Handles the selection to delete from all sources."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.DELETE_BOTH)


@admin_required
@error_handler
async def handle_action_mark(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Handles the selection to mark as blocked."""
    await show_action_confirm(callback, db_user, state, BlockedUserAction.MARK_AS_BLOCKED)


@admin_required
@error_handler
async def handle_confirm_action(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Executes the confirmed action."""
    data = await state.get_data()
    blocked_list = data.get('blocked_users_list', [])

    # Determine the action from callback_data
    action_code = callback.data.replace(BlockedUsersCallback.CONFIRM_PREFIX.value, '')
    action_map = {
        'db': BlockedUserAction.DELETE_FROM_DB,
        'rw': BlockedUserAction.DELETE_FROM_REMNAWAVE,
        'both': BlockedUserAction.DELETE_BOTH,
        'mark': BlockedUserAction.MARK_AS_BLOCKED,
    }
    action = action_map.get(action_code)

    if not action:
        await callback.answer('عملیات ناشناخته', show_alert=True)
        return

    if not blocked_list:
        await callback.answer('کاربری برای پردازش وجود ندارد', show_alert=True)
        return

    await state.set_state(BlockedUsersStates.processing_cleanup)

    # Convert back to BlockCheckResult
    blocked_results = [
        BlockCheckResult(
            user_id=u['user_id'],
            telegram_id=u['telegram_id'],
            username=u['username'],
            full_name=u['full_name'],
            status=None,  # type: ignore
            remnawave_uuid=u['remnawave_uuid'],
        )
        for u in blocked_list
    ]

    service = BlockedUsersService(bot)
    last_update_time = datetime.now(tz=UTC)

    async def progress_callback(processed: int, total_count: int) -> None:
        nonlocal last_update_time
        now = datetime.now(tz=UTC)
        if (now - last_update_time).total_seconds() >= 2:
            last_update_time = now
            try:
                await callback.message.edit_text(
                    BlockedUsersText.CLEANUP_PROGRESS.value.format(
                        processed=processed,
                        total=total_count,
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    # Perform the cleanup
    result = await service.cleanup_blocked_users(
        db,
        blocked_results,
        action,
        progress_callback=progress_callback,
    )

    # Clear the saved data
    await state.update_data(
        blocked_users_scan_result=None,
        blocked_users_list=[],
        pending_action=None,
    )
    await state.set_state(None)

    # Show the result
    text = BlockedUsersText.CLEANUP_COMPLETE.value.format(
        deleted_db=result.deleted_from_db,
        deleted_remnawave=result.deleted_from_remnawave,
        marked=result.marked_as_blocked,
        errors=len(result.errors),
    )

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_users_menu_keyboard(),
    )

    logger.info(
        'Blocked users cleanup completed: DB=, RW=, marked=, errors',
        deleted_from_db=result.deleted_from_db,
        deleted_from_remnawave=result.deleted_from_remnawave,
        marked_as_blocked=result.marked_as_blocked,
        errors_count=len(result.errors),
    )

    await callback.answer()


@admin_required
@error_handler
async def handle_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    """Cancels the current action and returns to the menu."""
    await state.update_data(pending_action=None)
    await state.set_state(BlockedUsersStates.viewing_results)
    await show_blocked_users_menu(callback, db_user, state)


# =============================================================================
# Registration
# =============================================================================


def register_handlers(dp: Dispatcher) -> None:
    """Registers handlers for the blocked users module."""

    # Main menu
    dp.callback_query.register(
        show_blocked_users_menu,
        F.data == BlockedUsersCallback.MENU.value,
    )

    # Scanning
    dp.callback_query.register(
        start_scan,
        F.data == BlockedUsersCallback.START_SCAN.value,
    )

    # Blocked list
    dp.callback_query.register(
        show_blocked_list,
        F.data == BlockedUsersCallback.VIEW_LIST.value,
    )

    # List pagination
    dp.callback_query.register(
        handle_blocked_list_pagination,
        F.data.startswith(BlockedUsersCallback.VIEW_LIST_PAGE.value),
    )

    # Action selection
    dp.callback_query.register(
        handle_action_delete_db,
        F.data == BlockedUsersCallback.ACTION_DELETE_DB.value,
    )
    dp.callback_query.register(
        handle_action_delete_remnawave,
        F.data == BlockedUsersCallback.ACTION_DELETE_REMNAWAVE.value,
    )
    dp.callback_query.register(
        handle_action_delete_both,
        F.data == BlockedUsersCallback.ACTION_DELETE_BOTH.value,
    )
    dp.callback_query.register(
        handle_action_mark,
        F.data == BlockedUsersCallback.ACTION_MARK.value,
    )

    # Action confirmation
    dp.callback_query.register(
        handle_confirm_action,
        F.data.startswith(BlockedUsersCallback.CONFIRM_PREFIX.value),
    )

    # Cancellation
    dp.callback_query.register(
        handle_cancel,
        F.data == BlockedUsersCallback.CANCEL.value,
    )
