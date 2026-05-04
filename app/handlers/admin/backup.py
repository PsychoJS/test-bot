import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.backup_service import backup_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class BackupStates(StatesGroup):
    waiting_backup_file = State()
    waiting_settings_update = State()


def get_backup_main_keyboard(language: str = 'ru'):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='🚀 ایجاد پشتیبان', callback_data='backup_create'),
                InlineKeyboardButton(text='📥 بازیابی', callback_data='backup_restore'),
            ],
            [
                InlineKeyboardButton(text='📋 لیست پشتیبان‌ها', callback_data='backup_list'),
                InlineKeyboardButton(text='⚙️ تنظیمات', callback_data='backup_settings'),
            ],
            [InlineKeyboardButton(text='◀️ بازگشت', callback_data='admin_panel')],
        ]
    )


def get_backup_list_keyboard(backups: list, page: int = 1, per_page: int = 5):
    keyboard = []

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_backups = backups[start_idx:end_idx]

    for backup in page_backups:
        try:
            if backup.get('timestamp'):
                dt = datetime.fromisoformat(backup['timestamp'].replace('Z', '+00:00'))
                date_str = dt.strftime('%d.%m %H:%M')
            else:
                date_str = '?'
        except:
            date_str = '?'

        size_str = f'{backup.get("file_size_mb", 0):.1f}MB'
        records_str = backup.get('total_records', '?')

        button_text = f'📦 {date_str} • {size_str} • {records_str} رکورد'
        callback_data = f'backup_manage_{backup["filename"]}'

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=callback_data)])

    if len(backups) > per_page:
        total_pages = (len(backups) + per_page - 1) // per_page
        nav_row = []

        if page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'backup_list_page_{page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))

        if page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'backup_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend([[InlineKeyboardButton(text='◀️ بازگشت', callback_data='backup_panel')]])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_backup_manage_keyboard(backup_filename: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📥 بازیابی', callback_data=f'backup_restore_file_{backup_filename}')],
            [InlineKeyboardButton(text='🗑️ حذف', callback_data=f'backup_delete_{backup_filename}')],
            [InlineKeyboardButton(text='◀️ به لیست', callback_data='backup_list')],
        ]
    )


def get_backup_settings_keyboard(settings_obj):
    auto_status = '✅ فعال' if settings_obj.auto_backup_enabled else '❌ غیرفعال'
    compression_status = '✅ فعال' if settings_obj.compression_enabled else '❌ غیرفعال'
    logs_status = '✅ فعال' if settings_obj.include_logs else '❌ غیرفعال'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f'🔄 پشتیبان‌گیری خودکار: {auto_status}', callback_data='backup_toggle_auto')],
            [InlineKeyboardButton(text=f'🗜️ فشرده‌سازی: {compression_status}', callback_data='backup_toggle_compression')],
            [InlineKeyboardButton(text=f'📋 لاگ‌ها در پشتیبان: {logs_status}', callback_data='backup_toggle_logs')],
            [InlineKeyboardButton(text='◀️ بازگشت', callback_data='backup_panel')],
        ]
    )


@admin_required
@error_handler
async def show_backup_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    status_auto = '✅ فعال' if settings_obj.auto_backup_enabled else '❌ غیرفعال'

    text = f"""🗄️ <b>سیستم پشتیبان‌گیری</b>

📊 <b>وضعیت:</b>
• پشتیبان‌گیری خودکار: {status_auto}
• فاصله زمانی: {settings_obj.backup_interval_hours} ساعت
• نگهداری: {settings_obj.max_backups_keep} فایل
• فشرده‌سازی: {'بله' if settings_obj.compression_enabled else 'خیر'}

📁 <b>مسیر:</b> <code>/app/data/backups</code>

⚡ <b>عملیات موجود:</b>
• ایجاد پشتیبان کامل از تمام داده‌ها
• بازیابی از فایل پشتیبان
• مدیریت پشتیبان‌گیری خودکار
"""

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def create_backup_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('🔄 ایجاد پشتیبان آغاز شد...')

    progress_msg = await callback.message.edit_text(
        '🔄 <b>در حال ایجاد پشتیبان...</b>\n\n⏳ در حال صدور داده‌ها از پایگاه داده...\nاین فرایند ممکن است چند دقیقه طول بکشد.',
        parse_mode='HTML',
    )

    # Создаем бекап
    created_by_id = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    success, message, file_path = await backup_service.create_backup(created_by=created_by_id, compress=True)

    if success:
        await progress_msg.edit_text(
            f'✅ <b>پشتیبان با موفقیت ایجاد شد!</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            f'❌ <b>خطا در ایجاد پشتیبان</b>\n\n{html.escape(message)}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def show_backup_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = 1
    if callback.data.startswith('backup_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except:
            page = 1

    backups = await backup_service.get_backup_list()

    if not backups:
        text = '📦 <b>لیست پشتیبان‌ها خالی است</b>\n\nهنوز هیچ پشتیبانی ایجاد نشده است.'
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🚀 ایجاد اولین پشتیبان', callback_data='backup_create')],
                [InlineKeyboardButton(text='◀️ بازگشت', callback_data='backup_panel')],
            ]
        )
    else:
        text = f'📦 <b>لیست پشتیبان‌ها</b> (تعداد: {len(backups)})\n\n'
        text += 'یک پشتیبان برای مدیریت انتخاب کنید:'
        keyboard = get_backup_list_keyboard(backups, page)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def manage_backup_file(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_manage_', '')

    backups = await backup_service.get_backup_list()
    backup_info = None

    for backup in backups:
        if backup['filename'] == filename:
            backup_info = backup
            break

    if not backup_info:
        await callback.answer('❌ فایل پشتیبان یافت نشد', show_alert=True)
        return

    try:
        if backup_info.get('timestamp'):
            dt = datetime.fromisoformat(backup_info['timestamp'].replace('Z', '+00:00'))
            date_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        else:
            date_str = 'نامشخص'
    except:
        date_str = 'خطای فرمت تاریخ'

    text = f"""📦 <b>اطلاعات پشتیبان</b>

📄 <b>فایل:</b> <code>{filename}</code>
📅 <b>ایجاد شده:</b> {date_str}
💾 <b>حجم:</b> {backup_info.get('file_size_mb', 0):.2f} MB
📊 <b>جداول:</b> {backup_info.get('tables_count', '?')}
📈 <b>رکوردها:</b> {backup_info.get('total_records', '?'):,}
🗜️ <b>فشرده‌سازی:</b> {'بله' if backup_info.get('compressed') else 'خیر'}
🗄️ <b>پایگاه داده:</b> {backup_info.get('database_type', 'unknown')}
"""

    if backup_info.get('error'):
        text += f'\n⚠️ <b>خطا:</b> {backup_info["error"]}'

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_manage_keyboard(filename))
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_delete_', '')

    text = '🗑️ <b>حذف پشتیبان</b>\n\n'
    text += 'آیا مطمئن هستید که می‌خواهید این پشتیبان را حذف کنید؟\n\n'
    text += f'📄 <code>{filename}</code>\n\n'
    text += '⚠️ <b>این عمل قابل بازگشت نیست!</b>'

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ بله، حذف', callback_data=f'backup_delete_confirm_{filename}'),
                InlineKeyboardButton(text='❌ لغو', callback_data=f'backup_manage_{filename}'),
            ]
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_delete_confirm_', '')

    success, message = await backup_service.delete_backup(filename)

    if success:
        await callback.message.edit_text(
            f'✅ <b>پشتیبان حذف شد</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='📋 به لیست پشتیبان‌ها', callback_data='backup_list')]]
            ),
        )
    else:
        await callback.message.edit_text(
            f'❌ <b>خطا در حذف</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )

    await callback.answer()


@admin_required
@error_handler
async def restore_backup_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if callback.data.startswith('backup_restore_file_'):
        # Восстановление из конкретного файла
        filename = callback.data.replace('backup_restore_file_', '')

        text = '📥 <b>بازیابی از پشتیبان</b>\n\n'
        text += f'📄 <b>فایل:</b> <code>{filename}</code>\n\n'
        text += '⚠️ <b>توجه!</b>\n'
        text += '• این فرایند ممکن است چند دقیقه طول بکشد\n'
        text += '• توصیه می‌شود قبل از بازیابی یک پشتیبان بگیرید\n'
        text += '• داده‌های موجود تکمیل خواهند شد\n\n'
        text += 'آیا می‌خواهید ادامه دهید؟'

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ بله، بازیابی', callback_data=f'backup_restore_execute_{filename}'
                    ),
                    InlineKeyboardButton(
                        text='🗑️ پاک کردن و بازیابی', callback_data=f'backup_restore_clear_{filename}'
                    ),
                ],
                [InlineKeyboardButton(text='❌ لغو', callback_data=f'backup_manage_{filename}')],
            ]
        )
    else:
        text = """📥 <b>بازیابی از پشتیبان</b>

📎 فایل پشتیبان را ارسال کنید (.json، .json.gz یا .tar.gz)

⚠️ <b>مهم:</b>
• فایل باید توسط همین سیستم پشتیبان‌گیری ایجاد شده باشد
• این فرایند ممکن است چند دقیقه طول بکشد
• توصیه می‌شود قبل از بازیابی یک پشتیبان بگیرید

💡 یا از پشتیبان‌های موجود در زیر انتخاب کنید."""

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='📋 انتخاب از لیست', callback_data='backup_list')],
                [InlineKeyboardButton(text='❌ لغو', callback_data='backup_panel')],
            ]
        )

        await state.set_state(BackupStates.waiting_backup_file)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def restore_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if callback.data.startswith('backup_restore_execute_'):
        filename = callback.data.replace('backup_restore_execute_', '')
        clear_existing = False
    elif callback.data.startswith('backup_restore_clear_'):
        filename = callback.data.replace('backup_restore_clear_', '')
        clear_existing = True
    else:
        await callback.answer('❌ فرمت دستور نامعتبر', show_alert=True)
        return

    await callback.answer('🔄 بازیابی آغاز شد...')

    # Показываем прогресс
    action_text = 'پاک کردن و بازیابی' if clear_existing else 'بازیابی'
    progress_msg = await callback.message.edit_text(
        f'📥 <b>در حال بازیابی از پشتیبان...</b>\n\n'
        f'⏳ در حال انجام {action_text} داده‌ها...\n'
        f'📄 فایل: <code>{filename}</code>\n\n'
        f'این فرایند ممکن است چند دقیقه طول بکشد.',
        parse_mode='HTML',
    )

    backup_path = backup_service.backup_dir / filename

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=clear_existing)

    if success:
        await progress_msg.edit_text(
            f'✅ <b>بازیابی با موفقیت انجام شد!</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            f'❌ <b>خطا در بازیابی</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )


@admin_required
@error_handler
async def handle_backup_file_upload(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if not message.document:
        await message.answer(
            '❌ لطفاً فایل پشتیبان را ارسال کنید (.json، .json.gz یا .tar.gz)',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ لغو', callback_data='backup_panel')]]
            ),
        )
        return

    document = message.document
    allowed_extensions = ('.json', '.json.gz', '.tar.gz', '.tar')

    if not document.file_name or not any(document.file_name.endswith(ext) for ext in allowed_extensions):
        await message.answer(
            '❌ فرمت فایل پشتیبانی نمی‌شود. یک فایل .json، .json.gz یا .tar.gz بارگذاری کنید',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ لغو', callback_data='backup_panel')]]
            ),
        )
        return

    if document.file_size > 50 * 1024 * 1024:
        await message.answer(
            '❌ فایل خیلی بزرگ است (حداکثر 50MB)',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ لغو', callback_data='backup_panel')]]
            ),
        )
        return

    try:
        file = await message.bot.get_file(document.file_id)

        temp_path = backup_service.backup_dir / f'uploaded_{document.file_name}'

        await message.bot.download_file(file.file_path, temp_path)

        text = f"""📥 <b>فایل بارگذاری شد</b>

📄 <b>نام:</b> <code>{document.file_name}</code>
💾 <b>حجم:</b> {document.file_size / 1024 / 1024:.2f} MB

⚠️ <b>توجه!</b>
فرایند بازیابی داده‌های پایگاه داده را تغییر خواهد داد.
توصیه می‌شود قبل از بازیابی یک پشتیبان بگیرید.

ادامه می‌دهید؟"""

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ بازیابی', callback_data=f'backup_restore_execute_{temp_path.name}'
                    ),
                    InlineKeyboardButton(
                        text='🗑️ پاک کردن و بازیابی',
                        callback_data=f'backup_restore_clear_{temp_path.name}',
                    ),
                ],
                [InlineKeyboardButton(text='❌ لغو', callback_data='backup_panel')],
            ]
        )

        await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.error('Backup file upload error', error=e)
        await message.answer(
            f'❌ خطا در بارگذاری فایل: {e!s}',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ لغو', callback_data='backup_panel')]]
            ),
        )


@admin_required
@error_handler
async def show_backup_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    text = f"""⚙️ <b>تنظیمات سیستم پشتیبان‌گیری</b>

🔄 <b>پشتیبان‌گیری خودکار:</b>
• وضعیت: {'✅ فعال' if settings_obj.auto_backup_enabled else '❌ غیرفعال'}
• فاصله زمانی: {settings_obj.backup_interval_hours} ساعت
• زمان اجرا: {settings_obj.backup_time}

📦 <b>نگهداری:</b>
• حداکثر فایل‌ها: {settings_obj.max_backups_keep}
• فشرده‌سازی: {'✅ فعال' if settings_obj.compression_enabled else '❌ غیرفعال'}
• شامل لاگ‌ها: {'✅ بله' if settings_obj.include_logs else '❌ خیر'}

📁 <b>مسیر:</b> <code>{settings_obj.backup_location}</code>
"""

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_settings_keyboard(settings_obj))
    await callback.answer()


@admin_required
@error_handler
async def toggle_backup_setting(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    if callback.data == 'backup_toggle_auto':
        new_value = not settings_obj.auto_backup_enabled
        await backup_service.update_backup_settings(auto_backup_enabled=new_value)
        status = 'فعال' if new_value else 'غیرفعال'
        await callback.answer(f'پشتیبان‌گیری خودکار {status}')

    elif callback.data == 'backup_toggle_compression':
        new_value = not settings_obj.compression_enabled
        await backup_service.update_backup_settings(compression_enabled=new_value)
        status = 'فعال' if new_value else 'غیرفعال'
        await callback.answer(f'فشرده‌سازی {status}')

    elif callback.data == 'backup_toggle_logs':
        new_value = not settings_obj.include_logs
        await backup_service.update_backup_settings(include_logs=new_value)
        status = 'فعال' if new_value else 'غیرفعال'
        await callback.answer(f'لاگ‌ها در پشتیبان {status}')

    await show_backup_settings(callback, db_user, db)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_backup_panel, F.data == 'backup_panel')

    dp.callback_query.register(create_backup_handler, F.data == 'backup_create')

    dp.callback_query.register(show_backup_list, F.data.startswith('backup_list'))

    dp.callback_query.register(manage_backup_file, F.data.startswith('backup_manage_'))

    dp.callback_query.register(
        delete_backup_confirm, F.data.startswith('backup_delete_') & ~F.data.startswith('backup_delete_confirm_')
    )

    dp.callback_query.register(delete_backup_execute, F.data.startswith('backup_delete_confirm_'))

    dp.callback_query.register(
        restore_backup_start, F.data.in_(['backup_restore']) | F.data.startswith('backup_restore_file_')
    )

    dp.callback_query.register(
        restore_backup_execute,
        F.data.startswith('backup_restore_execute_') | F.data.startswith('backup_restore_clear_'),
    )

    dp.callback_query.register(show_backup_settings, F.data == 'backup_settings')

    dp.callback_query.register(
        toggle_backup_setting, F.data.in_(['backup_toggle_auto', 'backup_toggle_compression', 'backup_toggle_logs'])
    )

    dp.message.register(handle_backup_file_upload, BackupStates.waiting_backup_file)
