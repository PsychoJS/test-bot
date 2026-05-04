import html
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import (
    count_active_users_for_squad,
    get_all_server_squads,
    get_server_squad_by_uuid,
)
from app.database.models import User
from app.keyboards.admin import (
    get_admin_remnawave_keyboard,
    get_node_management_keyboard,
    get_squad_edit_keyboard,
    get_squad_management_keyboard,
)
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveConfigurationError, RemnaWaveService
from app.services.remnawave_sync_service import (
    RemnaWaveAutoSyncStatus,
    remnawave_sync_service,
)
from app.services.system_settings_service import bot_configuration_service
from app.states import (
    RemnaWaveSyncStates,
    SquadCreateStates,
    SquadMigrationStates,
    SquadRenameStates,
)
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_bytes, format_datetime


logger = structlog.get_logger(__name__)

squad_inbound_selections = {}
squad_create_data = {}

MIGRATION_PAGE_SIZE = 8


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return 'کمتر از ۱ ثانیه'

    minutes, sec = divmod(int(seconds), 60)
    if minutes:
        if sec:
            return f'{minutes} دقیقه {sec} ثانیه'
        return f'{minutes} دقیقه'
    return f'{sec} ثانیه'


def _format_user_stats(stats: dict[str, Any] | None) -> str:
    if not stats:
        return '—'

    created = stats.get('created', 0)
    updated = stats.get('updated', 0)
    deleted = stats.get('deleted', stats.get('deactivated', 0))
    errors = stats.get('errors', 0)

    return f'• ایجاد شده: {created}\n• به‌روز شده: {updated}\n• غیرفعال شده: {deleted}\n• خطاها: {errors}'


def _format_server_stats(stats: dict[str, Any] | None) -> str:
    if not stats:
        return '—'

    created = stats.get('created', 0)
    updated = stats.get('updated', 0)
    removed = stats.get('removed', 0)
    total = stats.get('total', 0)

    return f'• ایجاد شده: {created}\n• به‌روز شده: {updated}\n• حذف شده: {removed}\n• مجموع در پنل: {total}'


def _build_auto_sync_view(status: RemnaWaveAutoSyncStatus) -> tuple[str, types.InlineKeyboardMarkup]:
    times_text = ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else '—'
    next_run_text = format_datetime(status.next_run) if status.next_run else '—'

    if status.last_run_finished_at:
        finished_text = format_datetime(status.last_run_finished_at)
        started_text = format_datetime(status.last_run_started_at) if status.last_run_started_at else '—'
        duration = status.last_run_finished_at - status.last_run_started_at if status.last_run_started_at else None
        duration_text = f' ({_format_duration(duration.total_seconds())})' if duration else ''
        reason_map = {
            'manual': 'دستی',
            'auto': 'طبق برنامه',
            'immediate': 'هنگام فعال‌سازی',
        }
        reason_text = reason_map.get(status.last_run_reason or '', '—')
        result_icon = '✅' if status.last_run_success else '❌'
        result_label = 'موفق' if status.last_run_success else 'با خطا'
        error_block = f'\n⚠️ خطا: {status.last_run_error}' if status.last_run_error else ''
        last_run_text = (
            f'{result_icon} {result_label}\n'
            f'• شروع: {started_text}\n'
            f'• پایان: {finished_text}{duration_text}\n'
            f'• دلیل اجرا: {reason_text}{error_block}'
        )
    elif status.last_run_started_at:
        last_run_text = (
            '⏳ همگام‌سازی شروع شده اما هنوز تمام نشده'
            if status.is_running
            else f'ℹ️ آخرین اجرا: {format_datetime(status.last_run_started_at)}'
        )
    else:
        last_run_text = '—'

    running_text = '⏳ در حال اجرا' if status.is_running else 'در انتظار'
    toggle_text = '❌ غیرفعال کردن' if status.enabled else '✅ فعال کردن'

    text = f"""🔄 <b>همگام‌سازی خودکار RemnaWave</b>

⚙️ <b>وضعیت:</b> {'✅ فعال' if status.enabled else '❌ غیرفعال'}
🕒 <b>برنامه:</b> {times_text}
📅 <b>اجرای بعدی:</b> {next_run_text if status.enabled else '—'}
⏱️ <b>حالت:</b> {running_text}

📊 <b>آخرین اجرا:</b>
{last_run_text}

👥 <b>کاربران:</b>
{_format_user_stats(status.last_user_stats)}

🌐 <b>سرورها:</b>
{_format_server_stats(status.last_server_stats)}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='🔁 اجرا کنید',
                    callback_data='remnawave_auto_sync_run',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=toggle_text,
                    callback_data='remnawave_auto_sync_toggle',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text='🕒 تغییر برنامه',
                    callback_data='remnawave_auto_sync_times',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text='⬅️ بازگشت',
                    callback_data='admin_rw_sync',
                )
            ],
        ]
    )

    return text, keyboard


def _format_migration_server_label(texts, server) -> str:
    status = (
        texts.t('ADMIN_SQUAD_MIGRATION_STATUS_AVAILABLE', '✅ در دسترس')
        if getattr(server, 'is_available', True)
        else texts.t('ADMIN_SQUAD_MIGRATION_STATUS_UNAVAILABLE', '🚫 غیر قابل دسترس')
    )
    return texts.t(
        'ADMIN_SQUAD_MIGRATION_SERVER_LABEL',
        '{name} — 👥 {users} ({status})',
    ).format(name=html.escape(server.display_name), users=server.current_users, status=status)


def _build_migration_keyboard(
    texts,
    squads,
    page: int,
    total_pages: int,
    stage: str,
    *,
    exclude_uuid: str = None,
):
    prefix = 'admin_migration_source' if stage == 'source' else 'admin_migration_target'
    rows = []
    has_items = False

    button_template = texts.t(
        'ADMIN_SQUAD_MIGRATION_SQUAD_BUTTON',
        '🌍 {name} — 👥 {users} ({status})',
    )

    for squad in squads:
        if exclude_uuid and squad.squad_uuid == exclude_uuid:
            continue

        has_items = True
        status = (
            texts.t('ADMIN_SQUAD_MIGRATION_STATUS_AVAILABLE_SHORT', '✅')
            if getattr(squad, 'is_available', True)
            else texts.t('ADMIN_SQUAD_MIGRATION_STATUS_UNAVAILABLE_SHORT', '🚫')
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=button_template.format(
                        name=squad.display_name,
                        users=squad.current_users,
                        status=status,
                    ),
                    callback_data=f'{prefix}_{squad.squad_uuid}',
                )
            ]
        )

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(
                types.InlineKeyboardButton(
                    text='⬅️',
                    callback_data=f'{prefix}_page_{page - 1}',
                )
            )
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_SQUAD_MIGRATION_PAGE',
                    'صفحه {page}/{pages}',
                ).format(page=page, pages=total_pages),
                callback_data='admin_migration_page_info',
            )
        )
        if page < total_pages:
            nav_buttons.append(
                types.InlineKeyboardButton(
                    text='➡️',
                    callback_data=f'{prefix}_page_{page + 1}',
                )
            )
        rows.append(nav_buttons)

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.CANCEL,
                callback_data='admin_migration_cancel',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows), has_items


async def _fetch_migration_page(
    db: AsyncSession,
    page: int,
):
    squads, total = await get_all_server_squads(
        db,
        page=max(1, page),
        limit=MIGRATION_PAGE_SIZE,
    )
    total_pages = max(1, math.ceil(total / MIGRATION_PAGE_SIZE))

    page = max(page, 1)
    if page > total_pages:
        page = total_pages
        squads, total = await get_all_server_squads(
            db,
            page=page,
            limit=MIGRATION_PAGE_SIZE,
        )
        total_pages = max(1, math.ceil(total / MIGRATION_PAGE_SIZE))

    return squads, page, total_pages


@admin_required
@error_handler
async def show_squad_migration_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    await state.clear()

    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'source',
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_SOURCE',
            'اسکواد مبدأ را انتخاب کنید:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_NO_OPTIONS',
            'اسکوادی در دسترس نیست. یک اسکواد جدید اضافه کنید یا عملیات را لغو کنید.',
        )

    await state.set_state(SquadMigrationStates.selecting_source)

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def paginate_migration_source(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_source:
        await callback.answer()
        return

    try:
        page = int(callback.data.split('_page_')[-1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    squads, page, total_pages = await _fetch_migration_page(db, page=page)
    texts = get_texts(db_user.language)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'source',
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_SOURCE',
            'اسکواد مبدأ را انتخاب کنید:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_NO_OPTIONS',
            'اسکوادی در دسترس نیست. یک اسکواد جدید اضافه کنید یا عملیات را لغو کنید.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_source_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_source:
        await callback.answer()
        return

    if '_page_' in callback.data:
        await callback.answer()
        return

    source_uuid = callback.data.replace('admin_migration_source_', '', 1)

    texts = get_texts(db_user.language)
    server = await get_server_squad_by_uuid(db, source_uuid)

    if not server:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SQUAD_NOT_FOUND',
                'اسکواد یافت نشد یا در دسترس نیست.',
            ),
            show_alert=True,
        )
        return

    await state.update_data(
        source_uuid=server.squad_uuid,
        source_display=_format_migration_server_label(texts, server),
    )

    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=server.squad_uuid,
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'مبدأ: {source}',
        ).format(source=_format_migration_server_label(texts, server))
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'اسکواد مقصد را انتخاب کنید:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'اسکواد دیگری برای مهاجرت وجود ندارد. عملیات را لغو کنید یا اسکوادهای جدید بسازید.',
        )

    await state.set_state(SquadMigrationStates.selecting_target)

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def paginate_migration_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_target:
        await callback.answer()
        return

    try:
        page = int(callback.data.split('_page_')[-1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')
    if not source_uuid:
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    squads, page, total_pages = await _fetch_migration_page(db, page=page)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=source_uuid,
    )

    source_display = data.get('source_display') or source_uuid

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'مبدأ: {source}',
        ).format(source=source_display)
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'اسکواد مقصد را انتخاب کنید:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'اسکواد دیگری برای مهاجرت وجود ندارد. عملیات را لغو کنید یا اسکوادهای جدید بسازید.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_target_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    current_state = await state.get_state()
    if current_state != SquadMigrationStates.selecting_target:
        await callback.answer()
        return

    if '_page_' in callback.data:
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')

    if not source_uuid:
        await callback.answer()
        return

    target_uuid = callback.data.replace('admin_migration_target_', '', 1)

    texts = get_texts(db_user.language)

    if target_uuid == source_uuid:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SAME_SQUAD',
                'نمی‌توان همان اسکواد را انتخاب کرد.',
            ),
            show_alert=True,
        )
        return

    target_server = await get_server_squad_by_uuid(db, target_uuid)
    if not target_server:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SQUAD_NOT_FOUND',
                'اسکواد یافت نشد یا در دسترس نیست.',
            ),
            show_alert=True,
        )
        return

    source_display = data.get('source_display') or source_uuid

    users_to_move = await count_active_users_for_squad(db, source_uuid)

    await state.update_data(
        target_uuid=target_server.squad_uuid,
        target_display=_format_migration_server_label(texts, target_server),
        migration_count=users_to_move,
    )

    await state.set_state(SquadMigrationStates.confirming)

    message_lines = [
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>'),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_DETAILS',
            'پارامترهای مهاجرت را بررسی کنید:',
        ),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_SOURCE',
            '• از: {source}',
        ).format(source=source_display),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_TARGET',
            '• به: {target}',
        ).format(target=_format_migration_server_label(texts, target_server)),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_COUNT',
            '• کاربران برای انتقال: {count}',
        ).format(count=users_to_move),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_PROMPT',
            'عملیات را تأیید کنید.',
        ),
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_CONFIRM_BUTTON',
                        '✅ تأیید',
                    ),
                    callback_data='admin_migration_confirm',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_CHANGE_TARGET',
                        '🔄 تغییر سرور مقصد',
                    ),
                    callback_data='admin_migration_change_target',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.CANCEL,
                    callback_data='admin_migration_cancel',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def change_migration_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    data = await state.get_data()
    source_uuid = data.get('source_uuid')

    if not source_uuid:
        await callback.answer()
        return

    await state.set_state(SquadMigrationStates.selecting_target)

    texts = get_texts(db_user.language)
    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=source_uuid,
    )

    source_display = data.get('source_display') or source_uuid

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>مهاجرت اسکواد</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'مبدأ: {source}',
        ).format(source=source_display)
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'اسکواد مقصد را انتخاب کنید:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'اسکواد دیگری برای مهاجرت وجود ندارد. عملیات را لغو کنید یا اسکوادهای جدید بسازید.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_squad_migration(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    current_state = await state.get_state()
    if current_state != SquadMigrationStates.confirming:
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')
    target_uuid = data.get('target_uuid')

    if not source_uuid or not target_uuid:
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()

    await callback.answer(texts.t('ADMIN_SQUAD_MIGRATION_IN_PROGRESS', 'در حال راه‌اندازی مهاجرت...'))

    try:
        result = await remnawave_service.migrate_squad_users(
            db,
            source_uuid=source_uuid,
            target_uuid=target_uuid,
        )
    except RemnaWaveConfigurationError as error:
        message = texts.t(
            'ADMIN_SQUAD_MIGRATION_API_ERROR',
            '❌ RemnaWave API پیکربندی نشده: {error}',
        ).format(error=str(error))
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                            '⬅️ به Remnawave',
                        ),
                        callback_data='admin_remnawave',
                    )
                ]
            ]
        )
        await callback.message.edit_text(message, reply_markup=reply_markup)
        await state.clear()
        return

    source_display = data.get('source_display') or source_uuid
    target_display = data.get('target_display') or target_uuid

    if not result.get('success'):
        error_message = result.get('message') or ''
        error_code = result.get('error') or 'unexpected'
        message = texts.t(
            'ADMIN_SQUAD_MIGRATION_ERROR',
            '❌ مهاجرت انجام نشد (کد: {code}). {details}',
        ).format(code=error_code, details=error_message)
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                            '⬅️ به Remnawave',
                        ),
                        callback_data='admin_remnawave',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_NEW_BUTTON',
                            '🔁 مهاجرت جدید',
                        ),
                        callback_data='admin_rw_migration',
                    )
                ],
            ]
        )
        await callback.message.edit_text(message, reply_markup=reply_markup)
        await state.clear()
        return

    message_lines = [
        texts.t('ADMIN_SQUAD_MIGRATION_SUCCESS_TITLE', '✅ مهاجرت تکمیل شد'),
        '',
        texts.t('ADMIN_SQUAD_MIGRATION_CONFIRM_SOURCE', '• از: {source}').format(source=source_display),
        texts.t('ADMIN_SQUAD_MIGRATION_CONFIRM_TARGET', '• به: {target}').format(target=target_display),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_RESULT_TOTAL',
            'اشتراک‌ها یافت شد: {count}',
        ).format(count=result.get('total', 0)),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_RESULT_UPDATED',
            'منتقل شد: {count}',
        ).format(count=result.get('updated', 0)),
    ]

    panel_updated = result.get('panel_updated', 0)
    panel_failed = result.get('panel_failed', 0)

    if panel_updated:
        message_lines.append(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_RESULT_PANEL_UPDATED',
                'در پنل به‌روز شد: {count}',
            ).format(count=panel_updated)
        )
    if panel_failed:
        message_lines.append(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_RESULT_PANEL_FAILED',
                'به‌روزرسانی در پنل ناموفق بود: {count}',
            ).format(count=panel_failed)
        )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_NEW_BUTTON',
                        '🔁 مهاجرت جدید',
                    ),
                    callback_data='admin_rw_migration',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                        '⬅️ به Remnawave',
                    ),
                    callback_data='admin_remnawave',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    await state.clear()


@admin_required
@error_handler
async def cancel_squad_migration(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await state.clear()

    message = texts.t(
        'ADMIN_SQUAD_MIGRATION_CANCELLED',
        '❌ مهاجرت لغو شد.',
    )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                        '⬅️ به Remnawave',
                    ),
                    callback_data='admin_remnawave',
                )
            ]
        ]
    )

    await callback.message.edit_text(message, reply_markup=reply_markup)
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_page_info(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await callback.answer(
        texts.t('ADMIN_SQUAD_MIGRATION_PAGE_HINT', 'این صفحه فعلی است.'),
        show_alert=False,
    )


@admin_required
@error_handler
async def show_remnawave_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()
    connection_test = await remnawave_service.test_api_connection()

    status = connection_test.get('status')
    if status == 'connected':
        status_emoji = '✅'
    elif status == 'not_configured':
        status_emoji = 'ℹ️'
    else:
        status_emoji = '❌'

    api_url_display = settings.REMNAWAVE_API_URL or '—'

    text = f"""
🖥️ <b>مدیریت Remnawave</b>

📡 <b>اتصال:</b> {status_emoji} {connection_test.get('message', 'داده‌ای موجود نیست')}
🌐 <b>URL:</b> <code>{api_url_display}</code>

یک عملیات انتخاب کنید:
"""

    await callback.message.edit_text(text, reply_markup=get_admin_remnawave_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_system_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.get_system_statistics()

    if 'error' in stats:
        await callback.message.edit_text(
            f'❌ خطا در دریافت آمار: {stats["error"]}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    system = stats.get('system', {})
    users_by_status = stats.get('users_by_status', {})
    server_info = stats.get('server_info', {})
    bandwidth = stats.get('bandwidth', {})
    traffic_periods = stats.get('traffic_periods', {})
    nodes_realtime = stats.get('nodes_realtime', [])
    nodes_weekly = stats.get('nodes_weekly', [])

    memory_total = server_info.get('memory_total', 1)
    memory_used_percent = (server_info.get('memory_used', 0) / memory_total * 100) if memory_total > 0 else 0

    uptime_seconds = server_info.get('uptime_seconds', 0)
    uptime_days = int(uptime_seconds // 86400)
    uptime_hours = int((uptime_seconds % 86400) // 3600)
    uptime_str = f'{uptime_days}ر {uptime_hours}س'

    users_status_text = ''
    for status, count in users_by_status.items():
        status_emoji = {'ACTIVE': '✅', 'DISABLED': '❌', 'LIMITED': '⚠️', 'EXPIRED': '⏰'}.get(status, '❓')
        users_status_text += f'  {status_emoji} {status}: {count}\n'

    top_nodes_text = ''
    for i, node in enumerate(nodes_weekly[:3], 1):
        top_nodes_text += f'  {i}. {node["name"]}: {format_bytes(node["total_bytes"])}\n'

    realtime_nodes_text = ''
    for node in nodes_realtime[:3]:
        node_total = node.get('downloadBytes', 0) + node.get('uploadBytes', 0)
        if node_total > 0:
            realtime_nodes_text += f'  📡 {node.get("nodeName", "Unknown")}: {format_bytes(node_total)}\n'

    def format_traffic_change(difference_str):
        if not difference_str or difference_str == '0':
            return ''
        if difference_str.startswith('-'):
            return f' (🔻 {difference_str[1:]})'
        return f' (🔺 {difference_str})'

    text = f"""
📊 <b>آمار دقیق Remnawave</b>

🖥️ <b>سرور:</b>
- CPU: {server_info.get('cpu_cores', 0)} هسته
- RAM: {format_bytes(server_info.get('memory_used', 0))} / {format_bytes(memory_total)} ({memory_used_percent:.1f}%)
- آزاد: {format_bytes(server_info.get('memory_free', 0))}
- Uptime: {uptime_str}

👥 <b>کاربران ({system.get('total_users', 0)} مجموع):</b>
- 🟢 آنلاین الان: {system.get('users_online', 0)}
- 📅 در ۲۴ ساعت: {system.get('users_last_day', 0)}
- 📊 در ۷ روز: {system.get('users_last_week', 0)}
- 💤 هرگز وارد نشده‌اند: {system.get('users_never_online', 0)}

<b>وضعیت کاربران:</b>
{users_status_text}

🌐 <b>نودها ({system.get('nodes_online', 0)} آنلاین):</b>"""

    if realtime_nodes_text:
        text += f"""
<b>فعالیت لحظه‌ای:</b>
{realtime_nodes_text}"""

    if top_nodes_text:
        text += f"""
<b>برترین نودها در هفته:</b>
{top_nodes_text}"""

    text += f"""

📈 <b>ترافیک کل کاربران:</b> {format_bytes(system.get('total_user_traffic', 0))}

📊 <b>ترافیک بر اساس دوره:</b>
- ۲ روز: {format_bytes(traffic_periods.get('last_2_days', {}).get('current', 0))}{format_traffic_change(traffic_periods.get('last_2_days', {}).get('difference', ''))}
- ۷ روز: {format_bytes(traffic_periods.get('last_7_days', {}).get('current', 0))}{format_traffic_change(traffic_periods.get('last_7_days', {}).get('difference', ''))}
- ۳۰ روز: {format_bytes(traffic_periods.get('last_30_days', {}).get('current', 0))}{format_traffic_change(traffic_periods.get('last_30_days', {}).get('difference', ''))}
- ماه: {format_bytes(traffic_periods.get('current_month', {}).get('current', 0))}{format_traffic_change(traffic_periods.get('current_month', {}).get('difference', ''))}
- سال: {format_bytes(traffic_periods.get('current_year', {}).get('current', 0))}{format_traffic_change(traffic_periods.get('current_year', {}).get('difference', ''))}
"""

    if bandwidth.get('realtime_total', 0) > 0:
        text += f"""
⚡ <b>ترافیک لحظه‌ای:</b>
- دانلود: {format_bytes(bandwidth.get('realtime_download', 0))}
- آپلود: {format_bytes(bandwidth.get('realtime_upload', 0))}
- مجموع: {format_bytes(bandwidth.get('realtime_total', 0))}
"""

    text += f"""
🕒 <b>به‌روز شده:</b> {format_datetime(stats.get('last_updated', datetime.now(UTC)))}
"""

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_rw_system')],
        [
            types.InlineKeyboardButton(text='📈 نودها', callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text='👥 همگام‌سازی', callback_data='admin_rw_sync'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_traffic_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()

    try:
        async with remnawave_service.get_api_client() as api:
            bandwidth_stats = await api.get_bandwidth_stats()

            realtime_usage = await api.get_nodes_realtime_usage()

            nodes_stats = await api.get_nodes_statistics()

    except Exception as e:
        await callback.message.edit_text(
            f'❌ خطا در دریافت آمار ترافیک: {e!s}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    def parse_bandwidth(bandwidth_str):
        return remnawave_service._parse_bandwidth_string(bandwidth_str)

    total_realtime_download = sum(node.get('downloadBytes', 0) for node in realtime_usage)
    total_realtime_upload = sum(node.get('uploadBytes', 0) for node in realtime_usage)
    total_realtime = total_realtime_download + total_realtime_upload

    total_users_online = sum(node.get('usersOnline', 0) for node in realtime_usage)

    periods = {
        'last_2_days': bandwidth_stats.get('bandwidthLastTwoDays', {}),
        'last_7_days': bandwidth_stats.get('bandwidthLastSevenDays', {}),
        'last_30_days': bandwidth_stats.get('bandwidthLast30Days', {}),
        'current_month': bandwidth_stats.get('bandwidthCalendarMonth', {}),
        'current_year': bandwidth_stats.get('bandwidthCurrentYear', {}),
    }

    def format_change(diff_str):
        if not diff_str or diff_str == '0':
            return ''
        if diff_str.startswith('-'):
            return f' 🔻 {diff_str[1:]}'
        return f' 🔺 {diff_str}'

    text = f"""
📊 <b>آمار ترافیک Remnawave</b>

⚡ <b>ترافیک اینباند:</b>
- دانلود: {format_bytes(total_realtime_download)}
- آپلود: {format_bytes(total_realtime_upload)}
- ترافیک کل: {format_bytes(total_realtime)}
- کاربران آنلاین: {total_users_online}

📈 <b>آمار بر اساس دوره:</b>

<b>در ۲ روز:</b>
- جاری: {format_bytes(parse_bandwidth(periods['last_2_days'].get('current', '0')))}
- قبلی: {format_bytes(parse_bandwidth(periods['last_2_days'].get('previous', '0')))}
- تغییر:{format_change(periods['last_2_days'].get('difference', ''))}

<b>در ۷ روز:</b>
- جاری: {format_bytes(parse_bandwidth(periods['last_7_days'].get('current', '0')))}
- قبلی: {format_bytes(parse_bandwidth(periods['last_7_days'].get('previous', '0')))}
- تغییر:{format_change(periods['last_7_days'].get('difference', ''))}

<b>در ۳۰ روز:</b>
- جاری: {format_bytes(parse_bandwidth(periods['last_30_days'].get('current', '0')))}
- قبلی: {format_bytes(parse_bandwidth(periods['last_30_days'].get('previous', '0')))}
- تغییر:{format_change(periods['last_30_days'].get('difference', ''))}

<b>ماه جاری:</b>
- جاری: {format_bytes(parse_bandwidth(periods['current_month'].get('current', '0')))}
- قبلی: {format_bytes(parse_bandwidth(periods['current_month'].get('previous', '0')))}
- تغییر:{format_change(periods['current_month'].get('difference', ''))}

<b>سال جاری:</b>
- جاری: {format_bytes(parse_bandwidth(periods['current_year'].get('current', '0')))}
- قبلی: {format_bytes(parse_bandwidth(periods['current_year'].get('previous', '0')))}
- تغییر:{format_change(periods['current_year'].get('difference', ''))}
"""

    if realtime_usage:
        text += '\n🌐 <b>ترافیک نودها (لحظه‌ای):</b>\n'
        for node in sorted(realtime_usage, key=lambda x: x.get('totalBytes', 0), reverse=True):
            node_total = node.get('totalBytes', 0)
            if node_total > 0:
                text += f'- {node.get("nodeName", "Unknown")}: {format_bytes(node_total)}\n'

    if nodes_stats.get('lastSevenDays'):
        text += '\n📊 <b>برترین نودها در ۷ روز:</b>\n'

        nodes_weekly = {}
        for day_data in nodes_stats['lastSevenDays']:
            node_name = day_data['nodeName']
            if node_name not in nodes_weekly:
                nodes_weekly[node_name] = 0
            nodes_weekly[node_name] += int(day_data['totalBytes'])

        sorted_nodes = sorted(nodes_weekly.items(), key=lambda x: x[1], reverse=True)
        for i, (node_name, total_bytes) in enumerate(sorted_nodes[:5], 1):
            text += f'{i}. {node_name}: {format_bytes(total_bytes)}\n'

    text += f'\n🕒 <b>به‌روز شده:</b> {format_datetime(datetime.now(UTC))}'

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_rw_traffic')],
        [
            types.InlineKeyboardButton(text='📈 نودها', callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text='📊 سیستم', callback_data='admin_rw_system'),
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_nodes_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()
    nodes = await remnawave_service.get_all_nodes()

    if not nodes:
        await callback.message.edit_text(
            '🖥️ نودی یافت نشد یا خطای اتصال',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    text = '🖥️ <b>مدیریت نودها</b>\n\n'
    keyboard = []

    for node in nodes:
        status_emoji = '🟢' if node['is_node_online'] else '🔴'
        connection_emoji = '📡' if node['is_connected'] else '📵'

        text += f'{status_emoji} {connection_emoji} <b>{node["name"]}</b>\n'
        text += f'🌍 {node["country_code"]} • {node["address"]}\n'
        text += f'👥 آنلاین: {node["users_online"] or 0}\n\n'

        keyboard.append(
            [types.InlineKeyboardButton(text=f'⚙️ {node["name"]}', callback_data=f'admin_node_manage_{node["uuid"]}')]
        )

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='🔄 راه‌اندازی مجدد همه', callback_data='admin_restart_all_nodes')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_node_details(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    node_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    node = await remnawave_service.get_node_details(node_uuid)

    if not node:
        await callback.answer('❌ نود یافت نشد', show_alert=True)
        return

    status_emoji = '🟢' if node['is_node_online'] else '🔴'
    xray_emoji = '✅' if node['is_xray_running'] else '❌'

    status_change = format_datetime(node['last_status_change']) if node.get('last_status_change') else '—'
    created_at = format_datetime(node['created_at']) if node.get('created_at') else '—'
    updated_at = format_datetime(node['updated_at']) if node.get('updated_at') else '—'
    notify_percent = f'{node["notify_percent"]}%' if node.get('notify_percent') is not None else '—'
    sys_info = (node.get('system') or {}).get('info', {})
    cpu_model = html.escape(str(sys_info.get('cpuModel') or '—'))
    cpu_count = sys_info.get('cpus', 0)
    cpu_info = f'{cpu_count}x {cpu_model}' if cpu_count else cpu_model
    memory_total = sys_info.get('memoryTotal', 0)
    total_ram = format_bytes(memory_total) if memory_total else '—'
    versions = node.get('versions') or {}
    xray_ver = html.escape(str(versions.get('xray') or '—'))
    node_ver = html.escape(str(versions.get('node') or '—'))
    xray_uptime_sec = node.get('xray_uptime', 0)
    if xray_uptime_sec:
        days, rem = divmod(int(xray_uptime_sec), 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        xray_uptime_str = f'{days}d {hours}h {mins}m' if days else (f'{hours}h {mins}m' if hours else f'{mins}m')
    else:
        xray_uptime_str = '—'

    text = f"""
🖥️ <b>نود: {html.escape(node['name'])}</b>

<b>وضعیت:</b>
- آنلاین: {status_emoji} {'بله' if node['is_node_online'] else 'خیر'}
- Xray: {xray_emoji} {'در حال اجرا' if node['is_xray_running'] else 'متوقف شده'}
- متصل: {'📡 بله' if node['is_connected'] else '📵 خیر'}
- قطع شده: {'❌ بله' if node['is_disabled'] else '✅ خیر'}
- تغییر وضعیت: {status_change}
- پیام: {html.escape(str(node.get('last_status_message') or '—'))}
- آپتایم Xray: {xray_uptime_str}

<b>نسخه‌ها:</b>
- Xray: {xray_ver}
- Node: {node_ver}

<b>اطلاعات:</b>
- آدرس: {html.escape(node['address'])}
- کشور: {html.escape(node['country_code'])}
- کاربران آنلاین: {node['users_online']}
- CPU: {cpu_info}
- RAM: {total_ram}
- ارائه‌دهنده: {html.escape(str(node.get('provider_uuid') or '—'))}

<b>ترافیک:</b>
- مصرف شده: {format_bytes(node['traffic_used_bytes'])}
- محدودیت: {format_bytes(node['traffic_limit_bytes']) if node['traffic_limit_bytes'] else 'بدون محدودیت'}
- ردیابی: {'✅ فعال' if node.get('is_traffic_tracking_active') else '❌ غیرفعال'}
- روز ریست: {node.get('traffic_reset_day') or '—'}
- اعلان‌ها: {notify_percent}
- ضریب: {node.get('consumption_multiplier') or 1}

<b>متادیتا:</b>
- ایجاد شده: {created_at}
- به‌روز شده: {updated_at}
"""

    await callback.message.edit_text(text, reply_markup=get_node_management_keyboard(node_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def manage_node(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    action, node_uuid = callback.data.split('_')[1], callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.manage_node(node_uuid, action)

    if success:
        action_text = {'enable': 'فعال شد', 'disable': 'غیرفعال شد', 'restart': 'راه‌اندازی مجدد شد'}
        await callback.answer(f'✅ نود {action_text.get(action, "پردازش شد")}')
    else:
        await callback.answer('❌ خطا در انجام عملیات', show_alert=True)

    await show_node_details(callback, db_user, db)


@admin_required
@error_handler
async def show_node_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    node_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()

    node = await remnawave_service.get_node_details(node_uuid)

    if not node:
        await callback.answer('❌ نود یافت نشد', show_alert=True)
        return

    status_emoji = '🟢' if node['is_node_online'] else '🔴'
    xray_emoji = '✅' if node['is_xray_running'] else '❌'
    xray_uptime_sec = node.get('xray_uptime', 0)
    if xray_uptime_sec:
        days, rem = divmod(int(xray_uptime_sec), 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        xray_uptime_str = f'{days}d {hours}h {mins}m' if days else (f'{hours}h {mins}m' if hours else f'{mins}m')
    else:
        xray_uptime_str = '—'

    try:
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=7)

        node_usage = await remnawave_service.get_node_user_usage_by_range(node_uuid, start_date, end_date)

        realtime_stats = await remnawave_service.get_nodes_realtime_usage()

        node_realtime = None
        for stats in realtime_stats:
            if stats.get('nodeUuid') == node_uuid:
                node_realtime = stats
                break

        status_change = format_datetime(node['last_status_change']) if node.get('last_status_change') else '—'
        created_at = format_datetime(node['created_at']) if node.get('created_at') else '—'
        updated_at = format_datetime(node['updated_at']) if node.get('updated_at') else '—'
        notify_percent = f'{node["notify_percent"]}%' if node.get('notify_percent') is not None else '—'
        sys_info = (node.get('system') or {}).get('info', {})
        cpu_model = html.escape(str(sys_info.get('cpuModel') or '—'))
        cpu_count = sys_info.get('cpus', 0)
        cpu_info = f'{cpu_count}x {cpu_model}' if cpu_count else cpu_model
        memory_total = sys_info.get('memoryTotal', 0)
        total_ram = format_bytes(memory_total) if memory_total else '—'
        sys_stats = (node.get('system') or {}).get('stats', {})
        load_avg = sys_stats.get('loadAvg', [])
        load_str = ' / '.join(f'{v:.2f}' for v in load_avg[:3]) if load_avg else '—'
        versions = node.get('versions') or {}
        xray_ver = html.escape(str(versions.get('xray') or '—'))
        node_ver = html.escape(str(versions.get('node') or '—'))

        text = f"""
📊 <b>آمار نود: {html.escape(node['name'])}</b>

<b>وضعیت:</b>
- آنلاین: {status_emoji} {'بله' if node['is_node_online'] else 'خیر'}
- Xray: {xray_emoji} {'در حال اجرا' if node['is_xray_running'] else 'متوقف شده'} (v{xray_ver})
- Node: v{node_ver}
- کاربران آنلاین: {node['users_online']}
- تغییر وضعیت: {status_change}
- پیام: {html.escape(str(node.get('last_status_message') or '—'))}
- آپتایم Xray: {xray_uptime_str}

<b>منابع:</b>
- CPU: {cpu_info}
- RAM: {total_ram}
- بار: {load_str}
- ارائه‌دهنده: {html.escape(str(node.get('provider_uuid') or '—'))}

<b>ترافیک:</b>
- مصرف شده: {format_bytes(node['traffic_used_bytes'] or 0)}
- محدودیت: {format_bytes(node['traffic_limit_bytes']) if node['traffic_limit_bytes'] else 'بدون محدودیت'}
- ردیابی: {'✅ فعال' if node.get('is_traffic_tracking_active') else '❌ غیرفعال'}
- روز ریست: {node.get('traffic_reset_day') or '—'}
- اعلان‌ها: {notify_percent}
- ضریب: {node.get('consumption_multiplier') or 1}

<b>متادیتا:</b>
- ایجاد شده: {created_at}
- به‌روز شده: {updated_at}
"""

        if node_realtime:
            text += f"""
<b>ترافیک اینباند:</b>
- دانلود: {format_bytes(node_realtime.get('downloadBytes', 0))}
- آپلود: {format_bytes(node_realtime.get('uploadBytes', 0))}
- ترافیک کل: {format_bytes(node_realtime.get('totalBytes', 0))}
- آنلاین: {node_realtime.get('usersOnline', 0)}
"""

        if node_usage:
            text += '\n<b>آمار ۷ روز:</b>\n'
            total_usage = 0
            for usage in node_usage[-5:]:
                daily_usage = usage.get('total', 0)
                total_usage += daily_usage
                text += f'- {usage.get("date", "N/A")}: {format_bytes(daily_usage)}\n'

            text += f'\n<b>ترافیک کل ۷ روز:</b> {format_bytes(total_usage)}'
        else:
            text += '\n<b>آمار ۷ روز:</b> داده‌ها موجود نیست'

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data=f'node_stats_{node_uuid}')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'admin_node_manage_{node_uuid}')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка получения статистики ноды', node_uuid=node_uuid, error=e)

        text = f"""
📊 <b>آمار نود: {html.escape(node['name'])}</b>

<b>وضعیت:</b>
- آنلاین: {status_emoji} {'بله' if node['is_node_online'] else 'خیر'}
- Xray: {xray_emoji} {'در حال اجرا' if node['is_xray_running'] else 'متوقف شده'}
- کاربران آنلاین: {node['users_online']}
- تغییر وضعیت: {format_datetime(node.get('last_status_change')) if node.get('last_status_change') else '—'}
- پیام: {html.escape(str(node.get('last_status_message') or '—'))}
- آپتایم Xray: {xray_uptime_str}

<b>ترافیک:</b>
- مصرف شده: {format_bytes(node['traffic_used_bytes'] or 0)}
- محدودیت: {format_bytes(node['traffic_limit_bytes']) if node['traffic_limit_bytes'] else 'بدون محدودیت'}
- ردیابی: {'✅ فعال' if node.get('is_traffic_tracking_active') else '❌ غیرفعال'}
- روز ریست: {node.get('traffic_reset_day') or '—'}
- اعلان‌ها: {node.get('notify_percent') or '—'}
- ضریب: {node.get('consumption_multiplier') or 1}

⚠️ <b>آمار دقیق موقتاً در دسترس نیست</b>
دلایل احتمالی:
• مشکل در اتصال به API
• نود اخیراً اضافه شده
• داده کافی برای نمایش وجود ندارد

<b>به‌روز شده:</b> {format_datetime('now')}
"""

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 تلاش مجدد', callback_data=f'node_stats_{node_uuid}')],
                [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'admin_node_manage_{node_uuid}')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()


@admin_required
@error_handler
async def show_squad_details(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    text = f"""
🌐 <b>اسکواد: {squad['name']}</b>

<b>اطلاعات:</b>
- UUID: <code>{squad['uuid']}</code>
- اعضا: {squad['members_count']}
- اینباندها: {squad['inbounds_count']}

<b>اینباندها:</b>
"""

    if squad.get('inbounds'):
        for inbound in squad['inbounds']:
            text += f'- {inbound["tag"]} ({inbound["type"]})\n'
    else:
        text += 'اینباند فعالی وجود ندارد'

    await callback.message.edit_text(text, reply_markup=get_squad_management_keyboard(squad_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def manage_squad_action(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    action = parts[1]
    squad_uuid = parts[-1]

    remnawave_service = RemnaWaveService()

    if action == 'add_users':
        success = await remnawave_service.add_all_users_to_squad(squad_uuid)
        if success:
            await callback.answer('✅ وظیفه افزودن کاربران در صف قرار گرفت')
        else:
            await callback.answer('❌ خطا در افزودن کاربران', show_alert=True)

    elif action == 'remove_users':
        success = await remnawave_service.remove_all_users_from_squad(squad_uuid)
        if success:
            await callback.answer('✅ وظیفه حذف کاربران در صف قرار گرفت')
        else:
            await callback.answer('❌ خطا در حذف کاربران', show_alert=True)

    elif action == 'delete':
        success = await remnawave_service.delete_squad(squad_uuid)
        if success:
            await callback.message.edit_text(
                '✅ اسکواد با موفقیت حذف شد',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ به اسکوادها', callback_data='admin_rw_squads')]]
                ),
            )
        else:
            await callback.answer('❌ خطا در حذف اسکواد', show_alert=True)
        return

    refreshed_callback = callback.model_copy(update={'data': f'admin_squad_manage_{squad_uuid}'}).as_(callback.bot)

    await show_squad_details(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def show_squad_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    text = f"""
✏️ <b>ویرایش اسکواد: {squad['name']}</b>

<b>اینباندهای فعلی:</b>
"""

    if squad.get('inbounds'):
        for inbound in squad['inbounds']:
            text += f'✅ {inbound["tag"]} ({inbound["type"]})\n'
    else:
        text += 'اینباند فعالی وجود ندارد\n'

    text += '\n<b>عملیات موجود:</b>'

    await callback.message.edit_text(text, reply_markup=get_squad_edit_keyboard(squad_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_squad_inbounds_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()

    squad = await remnawave_service.get_squad_details(squad_uuid)
    all_inbounds = await remnawave_service.get_all_inbounds()

    if not squad:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    if not all_inbounds:
        await callback.answer('❌ اینباند در دسترس نیست', show_alert=True)
        return

    if squad_uuid not in squad_inbound_selections:
        squad_inbound_selections[squad_uuid] = {inbound['uuid'] for inbound in squad.get('inbounds', [])}

    text = f"""
🔧 <b>تغییر اینباندها</b>

<b>اسکواد:</b> {squad['name']}
<b>اینباندهای فعلی:</b> {len(squad_inbound_selections[squad_uuid])}

<b>اینباندهای موجود:</b>
"""

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in squad_inbound_selections[squad_uuid]
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})', callback_data=f'sqd_tgl_{i}_{squad_uuid[:8]}'
                )
            ]
        )

    if len(all_inbounds) > 15:
        text += f'\n⚠️ ۱۵ اینباند اول از {len(all_inbounds)} نمایش داده می‌شود'

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='💾 ذخیره تغییرات', callback_data=f'sqd_save_{squad_uuid[:8]}')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'sqd_edit_{squad_uuid[:8]}')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_squad_rename_form(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    await state.update_data(squad_uuid=squad_uuid, squad_name=squad['name'])
    await state.set_state(SquadRenameStates.waiting_for_new_name)

    text = f"""
✏️ <b>تغییر نام اسکواد</b>

<b>نام فعلی:</b> {squad['name']}

📝 <b>نام جدید اسکواد را وارد کنید:</b>

<i>الزامات نام:</i>
• از ۲ تا ۲۰ کاراکتر
• فقط حروف، اعداد، خط تیره و زیرخط
• بدون فاصله و کاراکترهای ویژه

پیامی با نام جدید ارسال کنید یا برای خروج «لغو» را بزنید.
"""

    keyboard = [[types.InlineKeyboardButton(text='❌ لغو', callback_data=f'cancel_rename_{squad_uuid}')]]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def cancel_squad_rename(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    squad_uuid = callback.data.split('_')[-1]

    await state.clear()

    refreshed_callback = callback.model_copy(update={'data': f'squad_edit_{squad_uuid}'}).as_(callback.bot)

    await show_squad_edit_menu(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def process_squad_new_name(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    squad_uuid = data.get('squad_uuid')
    old_name = data.get('squad_name')

    if not squad_uuid:
        await message.answer('❌ خطا: اسکواد یافت نشد')
        await state.clear()
        return

    new_name = message.text.strip()

    if not new_name:
        await message.answer('❌ نام نمی‌تواند خالی باشد. دوباره تلاش کنید:')
        return

    if len(new_name) < 2 or len(new_name) > 20:
        await message.answer('❌ نام باید بین ۲ تا ۲۰ کاراکتر باشد. دوباره تلاش کنید:')
        return

    import re

    if not re.match(r'^[A-Za-z0-9_-]+$', new_name):
        await message.answer(
            '❌ نام فقط می‌تواند شامل حروف، اعداد، خط تیره و زیرخط باشد. دوباره تلاش کنید:'
        )
        return

    if new_name == old_name:
        await message.answer('❌ نام جدید با نام فعلی یکسان است. نام دیگری وارد کنید:')
        return

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.rename_squad(squad_uuid, new_name)

    if success:
        await message.answer(
            f'✅ <b>اسکواد با موفقیت تغییر نام یافت!</b>\n\n'
            f'<b>نام قدیمی:</b> {old_name}\n'
            f'<b>نام جدید:</b> {new_name}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='📋 جزئیات اسکواد', callback_data=f'admin_squad_manage_{squad_uuid}'
                        )
                    ],
                    [types.InlineKeyboardButton(text='⬅️ به اسکوادها', callback_data='admin_rw_squads')],
                ]
            ),
        )
        await state.clear()
    else:
        await message.answer(
            '❌ <b>خطا در تغییر نام اسکواد</b>\n\n'
            'دلایل احتمالی:\n'
            '• اسکوادی با این نام از قبل وجود دارد\n'
            '• مشکل در اتصال به API\n'
            '• دسترسی کافی وجود ندارد\n\n'
            'نام دیگری امتحان کنید:',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'cancel_rename_{squad_uuid}')]
                ]
            ),
        )


@admin_required
@error_handler
async def toggle_squad_inbound(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    inbound_index = int(parts[2])
    short_squad_uuid = parts[3]

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            break

    if not full_squad_uuid:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    all_inbounds = await remnawave_service.get_all_inbounds()
    if inbound_index >= len(all_inbounds):
        await callback.answer('❌ اینباند یافت نشد', show_alert=True)
        return

    selected_inbound = all_inbounds[inbound_index]

    if full_squad_uuid not in squad_inbound_selections:
        squad_inbound_selections[full_squad_uuid] = set()

    if selected_inbound['uuid'] in squad_inbound_selections[full_squad_uuid]:
        squad_inbound_selections[full_squad_uuid].remove(selected_inbound['uuid'])
        await callback.answer(f'➖ حذف شد: {selected_inbound["tag"]}')
    else:
        squad_inbound_selections[full_squad_uuid].add(selected_inbound['uuid'])
        await callback.answer(f'➕ اضافه شد: {selected_inbound["tag"]}')

    text = f"""
🔧 <b>تغییر اینباندها</b>

<b>اسکواد:</b> {squads[0]['name'] if squads else 'نامشخص'}
<b>اینباندهای انتخاب شده:</b> {len(squad_inbound_selections[full_squad_uuid])}

<b>اینباندهای موجود:</b>
"""

    keyboard = []
    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in squad_inbound_selections[full_squad_uuid]
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})',
                    callback_data=f'sqd_tgl_{i}_{short_squad_uuid}',
                )
            ]
        )

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='💾 ذخیره تغییرات', callback_data=f'sqd_save_{short_squad_uuid}')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'sqd_edit_{short_squad_uuid}')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def save_squad_inbounds(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    short_squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    squad_name = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            squad_name = squad['name']
            break

    if not full_squad_uuid:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    selected_inbounds = squad_inbound_selections.get(full_squad_uuid, set())

    try:
        success = await remnawave_service.update_squad_inbounds(full_squad_uuid, list(selected_inbounds))

        if success:
            squad_inbound_selections.pop(full_squad_uuid, None)

            await callback.message.edit_text(
                f'✅ <b>اینباندهای اسکواد به‌روزرسانی شد</b>\n\n'
                f'<b>اسکواد:</b> {squad_name}\n'
                f'<b>تعداد اینباندها:</b> {len(selected_inbounds)}',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='⬅️ به اسکوادها', callback_data='admin_rw_squads')],
                        [
                            types.InlineKeyboardButton(
                                text='📋 جزئیات اسکواد', callback_data=f'admin_squad_manage_{full_squad_uuid}'
                            )
                        ],
                    ]
                ),
            )
            await callback.answer('✅ تغییرات ذخیره شد!')
        else:
            await callback.answer('❌ خطا در ذخیره تغییرات', show_alert=True)

    except Exception as e:
        logger.error('Error saving squad inbounds', error=e)
        await callback.answer('❌ خطا در ذخیره‌سازی', show_alert=True)


@admin_required
@error_handler
async def show_squad_edit_menu_short(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    short_squad_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            break

    if not full_squad_uuid:
        await callback.answer('❌ اسکواد یافت نشد', show_alert=True)
        return

    refreshed_callback = callback.model_copy(update={'data': f'squad_edit_{full_squad_uuid}'}).as_(callback.bot)

    await show_squad_edit_menu(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def start_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(SquadCreateStates.waiting_for_name)

    text = """
➕ <b>ایجاد اسکواد جدید</b>

<b>مرحله ۱ از ۲: نام اسکواد</b>

📝 <b>نام اسکواد جدید را وارد کنید:</b>

<i>الزامات نام:</i>
• از ۲ تا ۲۰ کاراکتر
• فقط حروف، اعداد، خط تیره و زیرخط
• بدون فاصله و کاراکترهای ویژه

پیامی با نام ارسال کنید یا برای خروج «لغو» را بزنید.
"""

    keyboard = [[types.InlineKeyboardButton(text='❌ لغو', callback_data='cancel_squad_create')]]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def process_squad_name(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    squad_name = message.text.strip()

    if not squad_name:
        await message.answer('❌ نام نمی‌تواند خالی باشد. دوباره تلاش کنید:')
        return

    if len(squad_name) < 2 or len(squad_name) > 20:
        await message.answer('❌ نام باید بین ۲ تا ۲۰ کاراکتر باشد. دوباره تلاش کنید:')
        return

    import re

    if not re.match(r'^[A-Za-z0-9_-]+$', squad_name):
        await message.answer(
            '❌ نام فقط می‌تواند شامل حروف، اعداد، خط تیره و زیرخط باشد. دوباره تلاش کنید:'
        )
        return

    await state.update_data(squad_name=squad_name)
    await state.set_state(SquadCreateStates.selecting_inbounds)

    user_id = message.from_user.id
    squad_create_data[user_id] = {'name': squad_name, 'selected_inbounds': set()}

    remnawave_service = RemnaWaveService()
    all_inbounds = await remnawave_service.get_all_inbounds()

    if not all_inbounds:
        await message.answer(
            '❌ <b>اینباند در دسترس نیست</b>\n\nبرای ایجاد اسکواد باید حداقل یک اینباند وجود داشته باشد.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ به اسکوادها', callback_data='admin_rw_squads')]]
            ),
        )
        await state.clear()
        return

    text = f"""
➕ <b>ایجاد اسکواد: {squad_name}</b>

<b>مرحله ۲ از ۲: انتخاب اینباندها</b>

<b>اینباندهای انتخاب شده:</b> 0

<b>اینباندهای موجود:</b>
"""

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'☐ {inbound["tag"]} ({inbound["type"]})', callback_data=f'create_tgl_{i}'
                )
            ]
        )

    if len(all_inbounds) > 15:
        text += f'\n⚠️ ۱۵ اینباند اول از {len(all_inbounds)} نمایش داده می‌شود'

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='✅ ایجاد اسکواد', callback_data='create_squad_finish')],
            [types.InlineKeyboardButton(text='❌ لغو', callback_data='cancel_squad_create')],
        ]
    )

    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def toggle_create_inbound(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    inbound_index = int(callback.data.split('_')[-1])
    user_id = callback.from_user.id

    if user_id not in squad_create_data:
        await callback.answer('❌ خطا: داده‌های جلسه یافت نشد', show_alert=True)
        await state.clear()
        return

    remnawave_service = RemnaWaveService()
    all_inbounds = await remnawave_service.get_all_inbounds()

    if inbound_index >= len(all_inbounds):
        await callback.answer('❌ اینباند یافت نشد', show_alert=True)
        return

    selected_inbound = all_inbounds[inbound_index]
    selected_inbounds = squad_create_data[user_id]['selected_inbounds']

    if selected_inbound['uuid'] in selected_inbounds:
        selected_inbounds.remove(selected_inbound['uuid'])
        await callback.answer(f'➖ حذف شد: {selected_inbound["tag"]}')
    else:
        selected_inbounds.add(selected_inbound['uuid'])
        await callback.answer(f'➕ اضافه شد: {selected_inbound["tag"]}')

    squad_name = squad_create_data[user_id]['name']

    text = f"""
➕ <b>ایجاد اسکواد: {squad_name}</b>

<b>مرحله ۲ از ۲: انتخاب اینباندها</b>

<b>اینباندهای انتخاب شده:</b> {len(selected_inbounds)}

<b>اینباندهای موجود:</b>
"""

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in selected_inbounds
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})', callback_data=f'create_tgl_{i}'
                )
            ]
        )

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='✅ ایجاد اسکواد', callback_data='create_squad_finish')],
            [types.InlineKeyboardButton(text='❌ لغو', callback_data='cancel_squad_create')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def finish_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    user_id = callback.from_user.id

    if user_id not in squad_create_data:
        await callback.answer('❌ خطا: داده‌های جلسه یافت نشد', show_alert=True)
        await state.clear()
        return

    squad_name = squad_create_data[user_id]['name']
    selected_inbounds = list(squad_create_data[user_id]['selected_inbounds'])

    if not selected_inbounds:
        await callback.answer('❌ باید حداقل یک اینباند انتخاب شود', show_alert=True)
        return

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.create_squad(squad_name, selected_inbounds)

    squad_create_data.pop(user_id, None)
    await state.clear()

    if success:
        await callback.message.edit_text(
            f'✅ <b>اسکواد با موفقیت ایجاد شد!</b>\n\n'
            f'<b>نام:</b> {squad_name}\n'
            f'<b>تعداد اینباندها:</b> {len(selected_inbounds)}\n\n'
            f'اسکواد آماده استفاده است!',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📋 لیست اسکوادها', callback_data='admin_rw_squads')],
                    [types.InlineKeyboardButton(text='⬅️ به پنل Remnawave', callback_data='admin_remnawave')],
                ]
            ),
        )
        await callback.answer('✅ اسکواد ایجاد شد!')
    else:
        await callback.message.edit_text(
            f'❌ <b>خطا در ایجاد اسکواد</b>\n\n'
            f'<b>نام:</b> {squad_name}\n\n'
            f'دلایل احتمالی:\n'
            f'• اسکوادی با این نام از قبل وجود دارد\n'
            f'• مشکل در اتصال به API\n'
            f'• دسترسی کافی وجود ندارد\n'
            f'• اینباندهای نامعتبر',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 تلاش مجدد', callback_data='admin_squad_create')],
                    [types.InlineKeyboardButton(text='⬅️ به اسکوادها', callback_data='admin_rw_squads')],
                ]
            ),
        )
        await callback.answer('❌ خطا در ایجاد اسکواد', show_alert=True)


@admin_required
@error_handler
async def cancel_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    user_id = callback.from_user.id

    squad_create_data.pop(user_id, None)
    await state.clear()

    await show_squads_management(callback, db_user, db)


@admin_required
@error_handler
async def restart_all_nodes(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()
    success = await remnawave_service.restart_all_nodes()

    if success:
        await callback.message.edit_text(
            '✅ دستور راه‌اندازی مجدد تمام نودها ارسال شد',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ به نودها', callback_data='admin_rw_nodes')]]
            ),
        )
    else:
        await callback.message.edit_text(
            '❌ خطا در راه‌اندازی مجدد نودها',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ به نودها', callback_data='admin_rw_nodes')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_sync_options(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    status = remnawave_sync_service.get_status()
    times_text = ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else '—'
    next_run_text = format_datetime(status.next_run) if status.next_run else '—'
    last_result = '—'

    if status.last_run_finished_at:
        result_icon = '✅' if status.last_run_success else '❌'
        result_label = 'موفق' if status.last_run_success else 'با خطا'
        finished_text = format_datetime(status.last_run_finished_at)
        last_result = f'{result_icon} {result_label} ({finished_text})'
    elif status.last_run_started_at:
        last_result = f'⏳ شروع شده در {format_datetime(status.last_run_started_at)}'

    status_lines = [
        f'⚙️ وضعیت: {"✅ فعال" if status.enabled else "❌ غیرفعال"}',
        f'🕒 برنامه: {times_text}',
        f'📅 اجرای بعدی: {next_run_text if status.enabled else "—"}',
        f'📊 آخرین اجرا: {last_result}',
    ]

    text = (
        '🔄 <b>همگام‌سازی با Remnawave</b>\n\n'
        '🔄 <b>همگام‌سازی کامل انجام می‌دهد:</b>\n'
        '• ایجاد کاربران جدید از پنل در ربات\n'
        '• به‌روزرسانی اطلاعات کاربران موجود\n'
        '• غیرفعال‌سازی اشتراک کاربران غایب در پنل\n'
        '• ذخیره موجودی کاربران\n'
        '• ⏱️ زمان اجرا: ۲-۵ دقیقه\n\n'
        '⚠️ <b>مهم:</b>\n'
        '• در حین همگام‌سازی عملیات دیگری انجام ندهید\n'
        '• در همگام‌سازی کامل، اشتراک کاربران غایب در پنل غیرفعال می‌شود\n'
        '• توصیه می‌شود همگام‌سازی کامل روزانه انجام شود\n'
        '• موجودی کاربران حذف نمی‌شود\n\n'
        '⬆️ <b>همگام‌سازی معکوس:</b>\n'
        '• کاربران فعال ربات را به پنل ارسال می‌کند\n'
        '• در صورت خرابی پنل یا بازیابی داده استفاده کنید\n\n' + '\n'.join(status_lines)
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text='🔄 شروع همگام‌سازی کامل',
                callback_data='sync_all_users',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='⬆️ همگام‌سازی به پنل',
                callback_data='sync_to_panel',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='⚙️ تنظیمات همگام‌سازی خودکار',
                callback_data='admin_rw_auto_sync',
            )
        ],
        [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_auto_sync_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_auto_sync_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    new_value = not bool(settings.REMNAWAVE_AUTO_SYNC_ENABLED)
    await bot_configuration_service.set_value(
        db,
        'REMNAWAVE_AUTO_SYNC_ENABLED',
        new_value,
    )
    await db.commit()

    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(f'همگام‌سازی خودکار {"فعال" if new_value else "غیرفعال"} شد')


@admin_required
@error_handler
async def prompt_auto_sync_schedule(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    status = remnawave_sync_service.get_status()
    current_schedule = ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else '—'

    instructions = (
        '🕒 <b>تنظیم برنامه همگام‌سازی خودکار</b>\n\n'
        'زمان‌های اجرا را با کاما یا در خط جداگانه با فرمت HH:MM وارد کنید.\n'
        f'برنامه فعلی: <code>{current_schedule}</code>\n\n'
        'مثال‌ها: <code>03:00, 15:30</code> یا <code>00:15\n06:00\n18:45</code>\n\n'
        'برای بازگشت بدون تغییر <b>لغو</b> ارسال کنید.'
    )

    await state.set_state(RemnaWaveSyncStates.waiting_for_schedule)
    await state.update_data(
        auto_sync_message_id=callback.message.message_id,
        auto_sync_message_chat_id=callback.message.chat.id,
    )

    await callback.message.edit_text(
        instructions,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ لغو',
                        callback_data='remnawave_auto_sync_cancel',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def cancel_auto_sync_schedule(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer('تغییر برنامه لغو شد')


@admin_required
@error_handler
async def run_auto_sync_now(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if remnawave_sync_service.get_status().is_running:
        await callback.answer('همگام‌سازی در حال اجراست', show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        '🔄 شروع همگام‌سازی خودکار...\n\nصبر کنید، ممکن است چند دقیقه طول بکشد.',
        parse_mode='HTML',
    )
    await callback.answer('همگام‌سازی خودکار شروع شد')

    result = await remnawave_sync_service.run_sync_now(reason='manual')
    status = remnawave_sync_service.get_status()
    base_text, keyboard = _build_auto_sync_view(status)

    if not result.get('started'):
        await callback.message.edit_text(
            '⚠️ <b>همگام‌سازی در حال اجراست</b>\n\n' + base_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        return

    if result.get('success'):
        user_stats = result.get('user_stats') or {}
        server_stats = result.get('server_stats') or {}
        summary = (
            '✅ <b>همگام‌سازی کامل شد</b>\n'
            f'👥 کاربران: ایجاد {user_stats.get("created", 0)}, به‌روزرسانی {user_stats.get("updated", 0)}, '
            f'غیرفعال {user_stats.get("deleted", user_stats.get("deactivated", 0))}, خطا {user_stats.get("errors", 0)}\n'
            f'🌐 سرورها: ایجاد {server_stats.get("created", 0)}, به‌روزرسانی {server_stats.get("updated", 0)}, حذف {server_stats.get("removed", 0)}\n\n'
        )
        final_text = summary + base_text
        await callback.message.edit_text(
            final_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        error_text = result.get('error') or 'خطای ناشناخته'
        summary = f'❌ <b>همگام‌سازی با خطا پایان یافت</b>\nدلیل: {error_text}\n\n'
        await callback.message.edit_text(
            summary + base_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def save_auto_sync_schedule(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    text = (message.text or '').strip()
    data = await state.get_data()

    if text.lower() in {'отмена', 'cancel'}:
        await state.clear()
        status = remnawave_sync_service.get_status()
        view_text, keyboard = _build_auto_sync_view(status)
        message_id = data.get('auto_sync_message_id')
        chat_id = data.get('auto_sync_message_chat_id', message.chat.id)
        if message_id:
            await message.bot.edit_message_text(
                view_text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        else:
            await message.answer(
                view_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await message.answer('تنظیم برنامه لغو شد')
        return

    parsed_times = settings.parse_daily_time_list(text)

    if not parsed_times:
        await message.answer(
            '❌ زمان شناسایی نشد. از فرمت HH:MM استفاده کنید، مثلاً 03:00 یا 18:45.',
        )
        return

    normalized_value = ', '.join(t.strftime('%H:%M') for t in parsed_times)
    await bot_configuration_service.set_value(
        db,
        'REMNAWAVE_AUTO_SYNC_TIMES',
        normalized_value,
    )
    await db.commit()

    status = remnawave_sync_service.get_status()
    view_text, keyboard = _build_auto_sync_view(status)
    message_id = data.get('auto_sync_message_id')
    chat_id = data.get('auto_sync_message_chat_id', message.chat.id)

    if message_id:
        await message.bot.edit_message_text(
            view_text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        await message.answer(
            view_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    await state.clear()
    await message.answer('✅ برنامه همگام‌سازی خودکار به‌روز شد')


@admin_required
@error_handler
async def sync_all_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Выполняет полную синхронизацию всех пользователей"""

    progress_text = """
🔄 <b>همگام‌سازی کامل در حال انجام...</b>

📋 مراحل:
• بارگذاری تمام کاربران از پنل Remnawave
• ایجاد کاربران جدید در ربات
• به‌روزرسانی کاربران موجود
• غیرفعال‌سازی اشتراک کاربران غایب
• ذخیره موجودی‌ها

⏳ لطفاً صبر کنید...
"""

    await callback.message.edit_text(progress_text, reply_markup=None)

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.sync_users_from_panel(db, 'all')

    total_operations = stats['created'] + stats['updated'] + stats.get('deleted', 0)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    elif stats['errors'] < total_operations:
        status_emoji = '⚠️'
        status_text = 'با هشدار کامل شد'
    else:
        status_emoji = '❌'
        status_text = 'با خطا کامل شد'

    text = f"""
{status_emoji} <b>همگام‌سازی کامل {status_text}</b>

📊 <b>نتیجه:</b>
• 🆕 ایجاد شده: {stats['created']}
• 🔄 به‌روز شده: {stats['updated']}
• 🗑️ غیرفعال شده: {stats.get('deleted', 0)}
• ❌ خطا: {stats['errors']}
"""

    if stats.get('deleted', 0) > 0:
        text += """

🗑️ <b>اشتراک‌های غیرفعال شده:</b>
اشتراک کاربرانی که در پنل Remnawave
وجود ندارند غیرفعال شد.
💰 موجودی کاربران حفظ شد.
"""

    if stats['errors'] > 0:
        text += """

⚠️ <b>توجه:</b>
برخی عملیات با خطا پایان یافت.
برای اطلاعات بیشتر لاگ‌ها را بررسی کنید.
"""

    text += """

💡 <b>توصیه‌ها:</b>
• همگام‌سازی کامل انجام شد
• توصیه می‌شود روزانه اجرا شود
• تمام کاربران پنل همگام شدند
"""

    keyboard = []

    if stats['errors'] > 0:
        keyboard.append([types.InlineKeyboardButton(text='🔄 تکرار همگام‌سازی', callback_data='sync_all_users')])

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(text='📊 آمار سیستم', callback_data='admin_rw_system'),
                types.InlineKeyboardButton(text='🌐 نودها', callback_data='admin_rw_nodes'),
            ],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def sync_users_to_panel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    await callback.message.edit_text(
        '⬆️ در حال همگام‌سازی داده‌های ربات به پنل Remnawave...\n\nممکن است چند دقیقه طول بکشد.',
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.sync_users_to_panel(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    else:
        status_emoji = '⚠️' if (stats['created'] + stats['updated']) > 0 else '❌'
        status_text = 'با هشدار کامل شد' if status_emoji == '⚠️' else 'با خطا کامل شد'

    text = (
        f'{status_emoji} <b>همگام‌سازی به پنل {status_text}</b>\n\n'
        '📊 <b>نتایج:</b>\n'
        f'• 🆕 ایجاد شده: {stats["created"]}\n'
        f'• 🔄 به‌روز شده: {stats["updated"]}\n'
        f'• ❌ خطا: {stats["errors"]}'
    )

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 تکرار', callback_data='sync_to_panel')],
        [types.InlineKeyboardButton(text='🔄 همگام‌سازی کامل', callback_data='sync_all_users')],
        [types.InlineKeyboardButton(text='⬅️ به همگام‌سازی', callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_sync_recommendations(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text('🔍 در حال تحلیل وضعیت همگام‌سازی...', reply_markup=None)

    remnawave_service = RemnaWaveService()
    recommendations = await remnawave_service.get_sync_recommendations(db)

    priority_emoji = {'low': '🟢', 'medium': '🟡', 'high': '🔴'}

    text = f"""
💡 <b>توصیه‌های همگام‌سازی</b>

{priority_emoji.get(recommendations['priority'], '🟢')} <b>اولویت:</b> {recommendations['priority'].upper()}
⏱️ <b>زمان تخمینی:</b> {recommendations['estimated_time']}

<b>عملیات پیشنهادی:</b>
"""

    if recommendations['sync_type'] == 'all':
        text += '🔄 همگام‌سازی کامل'
    elif recommendations['sync_type'] == 'update_only':
        text += '📈 به‌روزرسانی داده‌ها'
    elif recommendations['sync_type'] == 'new_only':
        text += '🆕 همگام‌سازی جدیدها'
    else:
        text += '✅ نیازی به همگام‌سازی نیست'

    text += '\n\n<b>دلایل:</b>\n'
    for reason in recommendations['reasons']:
        text += f'• {reason}\n'

    keyboard = []

    if recommendations['should_sync'] and recommendations['sync_type'] != 'none':
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text='✅ اجرای توصیه',
                    callback_data=f'sync_{recommendations["sync_type"]}_users'
                    if recommendations['sync_type'] != 'update_only'
                    else 'sync_update_data',
                )
            ]
        )

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='🔄 گزینه‌های دیگر', callback_data='admin_rw_sync')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def validate_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🔍 در حال اعتبارسنجی اشتراک‌ها...\n\nداده‌ها در حال بررسی هستند، ممکن است چند دقیقه طول بکشد.', reply_markup=None
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.validate_and_fix_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    else:
        status_emoji = '⚠️'
        status_text = 'با خطا کامل شد'

    text = f"""
{status_emoji} <b>اعتبارسنجی {status_text}</b>

📊 <b>نتایج:</b>
• 🔍 اشتراک‌های بررسی شده: {stats['checked']}
• 🔧 اشتراک‌های اصلاح شده: {stats['fixed']}
• ⚠️ مشکلات یافت شده: {stats['issues_found']}
• ❌ خطا: {stats['errors']}
"""

    if stats['fixed'] > 0:
        text += '\n✅ <b>مشکلات اصلاح شده:</b>\n'
        text += '• وضعیت اشتراک‌های منقضی\n'
        text += '• داده‌های Remnawave گمشده\n'
        text += '• محدودیت‌های ترافیک نادرست\n'
        text += '• تنظیمات دستگاه‌ها\n'

    if stats['errors'] > 0:
        text += '\n⚠️ در پردازش خطاهایی رخ داد.\nبرای اطلاعات بیشتر لاگ‌ها را بررسی کنید.'

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 تکرار اعتبارسنجی', callback_data='sync_validate')],
        [types.InlineKeyboardButton(text='🔄 همگام‌سازی کامل', callback_data='sync_all_users')],
        [types.InlineKeyboardButton(text='⬅️ به همگام‌سازی', callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def cleanup_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🧹 در حال پاکسازی اشتراک‌های منسوخ...\n\nاشتراک کاربران غایب از پنل در حال حذف است.',
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.cleanup_orphaned_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    else:
        status_emoji = '⚠️'
        status_text = 'با خطا کامل شد'

    text = f"""
{status_emoji} <b>پاکسازی {status_text}</b>

📊 <b>نتایج:</b>
• 🔍 اشتراک‌های بررسی شده: {stats['checked']}
• 🗑️ غیرفعال شده: {stats['deactivated']}
• ❌ خطا: {stats['errors']}
"""

    if stats['deactivated'] > 0:
        text += '\n🗑️ <b>اشتراک‌های غیرفعال شده:</b>\n'
        text += 'اشتراک کاربرانی که در\n'
        text += 'پنل Remnawave نیستند غیرفعال شد.\n'
    else:
        text += '\n✅ همه اشتراک‌ها به‌روز هستند!\nاشتراک منسوخی یافت نشد.'

    if stats['errors'] > 0:
        text += '\n⚠️ در پردازش خطاهایی رخ داد.\nبرای اطلاعات بیشتر لاگ‌ها را بررسی کنید.'

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 تکرار پاکسازی', callback_data='sync_cleanup')],
        [types.InlineKeyboardButton(text='🔍 اعتبارسنجی', callback_data='sync_validate')],
        [types.InlineKeyboardButton(text='⬅️ به همگام‌سازی', callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def force_cleanup_all_orphaned_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🗑️ در حال پاکسازی اجباری تمام کاربران غایب از پنل...\n\n'
        '⚠️ هشدار: تمام داده‌های کاربران کاملاً حذف خواهد شد!\n'
        '📊 شامل: تراکنش‌ها، درآمد ارجاع، کدهای تخفیف، سرورها، موجودی‌ها\n\n'
        '⏳ لطفاً صبر کنید...',
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.cleanup_orphaned_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    else:
        status_emoji = '⚠️'
        status_text = 'با خطا کامل شد'

    text = f"""
{status_emoji} <b>پاکسازی اجباری {status_text}</b>

📊 <b>نتایج:</b>
• 🔍 اشتراک‌های بررسی شده: {stats['checked']}
• 🗑️ کاملاً پاکسازی شده: {stats['deactivated']}
• ❌ خطا: {stats['errors']}
"""

    if stats['deactivated'] > 0:
        text += """

🗑️ <b>داده‌های کاملاً پاکسازی شده:</b>
• اشتراک‌ها به حالت اولیه بازگردانده شد
• تمام تراکنش‌های کاربران حذف شد
• تمام درآمدهای ارجاع حذف شد
• استفاده از کدهای تخفیف حذف شد
• موجودی‌ها به صفر بازگردانده شد
• سرورهای متصل حذف شد
• HWID دستگاه‌ها در Remnawave بازنشینی شد
• UUID های Remnawave پاکسازی شد
"""
    else:
        text += '\n✅ اشتراک منسوخی یافت نشد!\nتمام کاربران با پنل همگام هستند.'

    if stats['errors'] > 0:
        text += '\n⚠️ در پردازش خطاهایی رخ داد.\nبرای اطلاعات بیشتر لاگ‌ها را بررسی کنید.'

    keyboard = [
        [types.InlineKeyboardButton(text='🔄 تکرار پاکسازی', callback_data='force_cleanup_orphaned')],
        [types.InlineKeyboardButton(text='🔄 همگام‌سازی کامل', callback_data='sync_all_users')],
        [types.InlineKeyboardButton(text='⬅️ به همگام‌سازی', callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_force_cleanup(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
⚠️ <b>هشدار! عملیات خطرناک!</b>

🗑️ <b>پاکسازی اجباری کاملاً حذف می‌کند:</b>
• تمام تراکنش‌های کاربران غایب از پنل
• تمام درآمدها و روابط ارجاع
• تمام استفاده از کدهای تخفیف
• تمام سرورهای متصل اشتراک‌ها
• تمام موجودی‌ها (بازنشینی به صفر)
• تمام HWID دستگاه‌ها در Remnawave
• تمام UUID های Remnawave و لینک‌ها

⚡ <b>این عملیات غیرقابل برگشت است!</b>

فقط استفاده کنید اگر:
• همگام‌سازی معمولی کمکی نمی‌کند
• نیاز به پاکسازی کامل داده‌های «زباله» دارید
• بعد از حذف انبوه کاربران از پنل

❓ <b>آیا واقعاً می‌خواهید ادامه دهید؟</b>
"""

    keyboard = [
        [types.InlineKeyboardButton(text='🗑️ بله، همه را پاکسازی کن', callback_data='force_cleanup_orphaned')],
        [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def sync_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    sync_type = callback.data.split('_')[-2] + '_' + callback.data.split('_')[-1]

    progress_text = '🔄 در حال همگام‌سازی...\n\n'

    if sync_type == 'all_users':
        progress_text += '📋 نوع: همگام‌سازی کامل\n'
        progress_text += '• ایجاد کاربران جدید\n'
        progress_text += '• به‌روزرسانی موجودها\n'
        progress_text += '• حذف اشتراک‌های منسوخ\n'
    elif sync_type == 'new_users':
        progress_text += '📋 نوع: فقط کاربران جدید\n'
        progress_text += '• ایجاد کاربران از پنل\n'
    elif sync_type == 'update_data':
        progress_text += '📋 نوع: به‌روزرسانی داده‌ها\n'
        progress_text += '• به‌روزرسانی اطلاعات ترافیک\n'
        progress_text += '• همگام‌سازی اشتراک‌ها\n'

    progress_text += '\n⏳ لطفاً صبر کنید...'

    await callback.message.edit_text(progress_text, reply_markup=None)

    remnawave_service = RemnaWaveService()

    sync_map = {'all_users': 'all', 'new_users': 'new_only', 'update_data': 'update_only'}

    stats = await remnawave_service.sync_users_from_panel(db, sync_map.get(sync_type, 'all'))

    total_operations = stats['created'] + stats['updated'] + stats.get('deleted', 0)
    stats['created'] + stats['updated'] + stats.get('deleted', 0)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = 'با موفقیت کامل شد'
    elif stats['errors'] < total_operations:
        status_emoji = '⚠️'
        status_text = 'با هشدار کامل شد'
    else:
        status_emoji = '❌'
        status_text = 'با خطا کامل شد'

    text = f"""
{status_emoji} <b>همگام‌سازی {status_text}</b>

📊 <b>نتیجه:</b>
"""

    if sync_type == 'all_users':
        text += f'• 🆕 ایجاد شده: {stats["created"]}\n'
        text += f'• 🔄 به‌روز شده: {stats["updated"]}\n'
        if 'deleted' in stats:
            text += f'• 🗑️ حذف شده: {stats["deleted"]}\n'
        text += f'• ❌ خطا: {stats["errors"]}\n'
    elif sync_type == 'new_users':
        text += f'• 🆕 ایجاد شده: {stats["created"]}\n'
        text += f'• ❌ خطا: {stats["errors"]}\n'
        if stats['created'] == 0 and stats['errors'] == 0:
            text += '\n💡 کاربر جدیدی یافت نشد'
    elif sync_type == 'update_data':
        text += f'• 🔄 به‌روز شده: {stats["updated"]}\n'
        text += f'• ❌ خطا: {stats["errors"]}\n'
        if stats['updated'] == 0 and stats['errors'] == 0:
            text += '\n💡 تمام داده‌ها به‌روز هستند'

    if stats['errors'] > 0:
        text += '\n⚠️ <b>توجه:</b>\n'
        text += 'برخی عملیات با خطا پایان یافت.\n'
        text += 'برای اطلاعات بیشتر لاگ‌ها را بررسی کنید.'

    if sync_type == 'all_users' and 'deleted' in stats and stats['deleted'] > 0:
        text += '\n🗑️ <b>اشتراک‌های حذف شده:</b>\n'
        text += 'اشتراک کاربرانی که در پنل Remnawave\n'
        text += 'وجود ندارند غیرفعال شد.'

    text += '\n\n💡 <b>توصیه‌ها:</b>\n'
    if sync_type == 'all_users':
        text += '• همگام‌سازی کامل انجام شد\n'
        text += '• توصیه می‌شود روزانه اجرا شود\n'
    elif sync_type == 'new_users':
        text += '• همگام‌سازی کاربران جدید\n'
        text += '• در هنگام اضافه کردن انبوه استفاده کنید\n'
    elif sync_type == 'update_data':
        text += '• به‌روزرسانی داده‌های ترافیک\n'
        text += '• برای به‌روزرسانی آمار اجرا کنید\n'

    keyboard = []

    if stats['errors'] > 0:
        keyboard.append([types.InlineKeyboardButton(text='🔄 تکرار همگام‌سازی', callback_data=callback.data)])

    if sync_type != 'all_users':
        keyboard.append([types.InlineKeyboardButton(text='🔄 همگام‌سازی کامل', callback_data='sync_all_users')])

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(text='📊 آمار سیستم', callback_data='admin_rw_system'),
                types.InlineKeyboardButton(text='🌐 نودها', callback_data='admin_rw_nodes'),
            ],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_squads_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    text = '🌍 <b>مدیریت اسکوادها</b>\n\n'
    keyboard = []

    if squads:
        for squad in squads:
            text += f'🔹 <b>{squad["name"]}</b>\n'
            text += f'👥 اعضا: {squad["members_count"]}\n'
            text += f'📡 اینباندها: {squad["inbounds_count"]}\n\n'

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'⚙️ {squad["name"]}', callback_data=f'admin_squad_manage_{squad["uuid"]}'
                    )
                ]
            )
    else:
        text += 'اسکوادی یافت نشد'

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text='➕ ایجاد اسکواد', callback_data='admin_squad_create')],
            [types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_remnawave_menu, F.data == 'admin_remnawave')
    dp.callback_query.register(show_system_stats, F.data == 'admin_rw_system')
    dp.callback_query.register(show_traffic_stats, F.data == 'admin_rw_traffic')
    dp.callback_query.register(show_nodes_management, F.data == 'admin_rw_nodes')
    dp.callback_query.register(show_node_details, F.data.startswith('admin_node_manage_'))
    dp.callback_query.register(show_node_statistics, F.data.startswith('node_stats_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_enable_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_disable_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_restart_'))
    dp.callback_query.register(restart_all_nodes, F.data == 'admin_restart_all_nodes')
    dp.callback_query.register(show_sync_options, F.data == 'admin_rw_sync')
    dp.callback_query.register(show_auto_sync_settings, F.data == 'admin_rw_auto_sync')
    dp.callback_query.register(toggle_auto_sync_setting, F.data == 'remnawave_auto_sync_toggle')
    dp.callback_query.register(prompt_auto_sync_schedule, F.data == 'remnawave_auto_sync_times')
    dp.callback_query.register(cancel_auto_sync_schedule, F.data == 'remnawave_auto_sync_cancel')
    dp.callback_query.register(run_auto_sync_now, F.data == 'remnawave_auto_sync_run')
    dp.callback_query.register(sync_all_users, F.data == 'sync_all_users')
    dp.callback_query.register(sync_users_to_panel, F.data == 'sync_to_panel')
    dp.callback_query.register(show_squad_migration_menu, F.data == 'admin_rw_migration')
    dp.callback_query.register(paginate_migration_source, F.data.startswith('admin_migration_source_page_'))
    dp.callback_query.register(handle_migration_source_selection, F.data.startswith('admin_migration_source_'))
    dp.callback_query.register(paginate_migration_target, F.data.startswith('admin_migration_target_page_'))
    dp.callback_query.register(handle_migration_target_selection, F.data.startswith('admin_migration_target_'))
    dp.callback_query.register(change_migration_target, F.data == 'admin_migration_change_target')
    dp.callback_query.register(confirm_squad_migration, F.data == 'admin_migration_confirm')
    dp.callback_query.register(cancel_squad_migration, F.data == 'admin_migration_cancel')
    dp.callback_query.register(handle_migration_page_info, F.data == 'admin_migration_page_info')
    dp.callback_query.register(show_squads_management, F.data == 'admin_rw_squads')
    dp.callback_query.register(show_squad_details, F.data.startswith('admin_squad_manage_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_add_users_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_remove_users_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_delete_'))
    dp.callback_query.register(
        show_squad_edit_menu, F.data.startswith('squad_edit_') & ~F.data.startswith('squad_edit_inbounds_')
    )
    dp.callback_query.register(show_squad_inbounds_selection, F.data.startswith('squad_edit_inbounds_'))
    dp.callback_query.register(show_squad_rename_form, F.data.startswith('squad_rename_'))
    dp.callback_query.register(cancel_squad_rename, F.data.startswith('cancel_rename_'))
    dp.callback_query.register(toggle_squad_inbound, F.data.startswith('sqd_tgl_'))
    dp.callback_query.register(save_squad_inbounds, F.data.startswith('sqd_save_'))
    dp.callback_query.register(show_squad_edit_menu_short, F.data.startswith('sqd_edit_'))
    dp.callback_query.register(start_squad_creation, F.data == 'admin_squad_create')
    dp.callback_query.register(cancel_squad_creation, F.data == 'cancel_squad_create')
    dp.callback_query.register(toggle_create_inbound, F.data.startswith('create_tgl_'))
    dp.callback_query.register(finish_squad_creation, F.data == 'create_squad_finish')

    dp.message.register(process_squad_new_name, SquadRenameStates.waiting_for_new_name, F.text)

    dp.message.register(process_squad_name, SquadCreateStates.waiting_for_name, F.text)

    dp.message.register(
        save_auto_sync_schedule,
        RemnaWaveSyncStates.waiting_for_schedule,
        F.text,
    )
