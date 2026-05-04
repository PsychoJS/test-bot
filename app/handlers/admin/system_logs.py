from datetime import UTC, datetime
from html import escape
from pathlib import Path

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

LOG_PREVIEW_LIMIT = 2300


def _resolve_log_path() -> Path:
    log_path = Path(settings.LOG_FILE)
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    return log_path


def _format_preview_block(text: str) -> str:
    escaped_text = escape(text) if text else ''
    return f'<blockquote expandable><pre><code>{escaped_text}</code></pre></blockquote>'


def _build_logs_message(log_path: Path) -> str:
    if not log_path.exists():
        message = (
            '🧾 <b>لاگ‌های سیستم</b>\n\n'
            f'فایل <code>{log_path}</code> هنوز ایجاد نشده است.\n'
            'لاگ‌ها پس از اولین ثبت به صورت خودکار ظاهر می‌شوند.'
        )
        return message

    try:
        content = log_path.read_text(encoding='utf-8', errors='ignore')
    except Exception as error:  # pragma: no cover - защита от проблем чтения
        logger.error('Error reading log file', log_path=log_path, error=error)
        message = f'❌ <b>خطا در خواندن لاگ‌ها</b>\n\nخواندن فایل <code>{log_path}</code> ممکن نشد.'
        return message

    total_length = len(content)
    stats = log_path.stat()
    updated_at = datetime.fromtimestamp(stats.st_mtime, tz=UTC)

    if not content:
        preview_text = 'فایل لاگ خالی است.'
        truncated = False
    else:
        preview_text = content[-LOG_PREVIEW_LIMIT:]
        truncated = total_length > LOG_PREVIEW_LIMIT

    details_lines = [
        '🧾 <b>لاگ‌های سیستم</b>',
        '',
        f'📁 <b>فایل:</b> <code>{log_path}</code>',
        f'🕒 <b>به‌روزرسانی:</b> {updated_at.strftime("%d.%m.%Y %H:%M:%S")}',
        f'🧮 <b>حجم:</b> {total_length} کاراکتر',
        (f'👇 آخرین {LOG_PREVIEW_LIMIT} کاراکتر نمایش داده شده.' if truncated else '📄 تمام محتوای فایل نمایش داده شده.'),
        '',
        _format_preview_block(preview_text),
    ]

    return '\n'.join(details_lines)


def _get_logs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_system_logs_refresh')],
            [InlineKeyboardButton(text='⬇️ دانلود لاگ', callback_data='admin_system_logs_download')],
            [InlineKeyboardButton(text='⬅️ بازگشت', callback_data='admin_submenu_system')],
        ]
    )


@admin_required
@error_handler
async def show_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()
    message = _build_logs_message(log_path)

    reply_markup = _get_logs_keyboard()
    await callback.message.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def refresh_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()
    message = _build_logs_message(log_path)

    reply_markup = _get_logs_keyboard()
    await callback.message.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')
    await callback.answer('🔄 به‌روزرسانی شد')


@admin_required
@error_handler
async def download_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()

    if not log_path.exists() or not log_path.is_file():
        await callback.answer('❌ فایل لاگ یافت نشد', show_alert=True)
        return

    try:
        await callback.answer('⬇️ در حال ارسال لاگ...')

        document = FSInputFile(log_path)
        stats = log_path.stat()
        updated_at = datetime.fromtimestamp(stats.st_mtime, tz=UTC).strftime('%d.%m.%Y %H:%M:%S')
        caption = (
            f'🧾 فایل لاگ <code>{log_path.name}</code>\n📁 مسیر: <code>{log_path}</code>\n🕒 به‌روزرسانی: {updated_at}'
        )
        await callback.message.answer_document(document=document, caption=caption, parse_mode='HTML')
    except Exception as error:  # pragma: no cover - защита от ошибок отправки
        logger.error('Error sending log file', log_path=log_path, error=error)
        await callback.message.answer(
            '❌ <b>ارسال فایل لاگ ممکن نشد</b>\n\nلاگ‌های برنامه را بررسی کنید یا بعداً دوباره امتحان کنید.',
            parse_mode='HTML',
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(
        show_system_logs,
        F.data == 'admin_system_logs',
    )
    dp.callback_query.register(
        refresh_system_logs,
        F.data == 'admin_system_logs_refresh',
    )
    dp.callback_query.register(
        download_system_logs,
        F.data == 'admin_system_logs_download',
    )
