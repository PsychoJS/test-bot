import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rules import clear_all_rules, get_rules_statistics
from app.database.crud.ticket import TicketCRUD
from app.database.models import User
from app.handlers.admin import support_settings as support_settings_handlers
from app.keyboards.admin import (
    get_admin_communications_submenu_keyboard,
    get_admin_main_keyboard,
    get_admin_promo_submenu_keyboard,
    get_admin_settings_submenu_keyboard,
    get_admin_support_submenu_keyboard,
    get_admin_system_submenu_keyboard,
    get_admin_users_submenu_keyboard,
)
from app.localization.texts import clear_rules_cache, get_texts
from app.services.support_settings_service import SupportSettingsService
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_admin_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    admin_text = texts.ADMIN_PANEL
    try:
        from app.services.remnawave_service import RemnaWaveService

        remnawave_service = RemnaWaveService()
        stats = await remnawave_service.get_system_statistics()
        system_stats = stats.get('system', {})
        users_online = system_stats.get('users_online', 0)
        users_today = system_stats.get('users_last_day', 0)
        users_week = system_stats.get('users_last_week', 0)
        admin_text = admin_text.replace(
            '\n\nانتخاب بخش مورد نظر:',
            (
                f'\n\n- 🟢 آنلاین اکنون: {users_online}'
                f'\n- 📅 آنلاین امروز: {users_today}'
                f'\n- 🗓️ این هفته: {users_week}'
                '\n\nانتخاب بخش مورد نظر:'
            ),
        )
    except Exception as e:
        logger.error('Failed to fetch Remnawave statistics for admin panel', error=e)

    await callback.message.edit_text(admin_text, reply_markup=get_admin_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USERS_SUBMENU_TITLE', '👥 **مدیریت کاربران و اشتراک‌ها**\n\n')
        + texts.t('ADMIN_SUBMENU_SELECT_SECTION', 'بخش مورد نظر را انتخاب کنید:'),
        reply_markup=get_admin_users_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_promo_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_PROMO_SUBMENU_TITLE', '💰 **پروموکدها و آمار**\n\n')
        + texts.t('ADMIN_SUBMENU_SELECT_SECTION', 'بخش مورد نظر را انتخاب کنید:'),
        reply_markup=get_admin_promo_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_communications_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_COMMUNICATIONS_SUBMENU_TITLE', '📨 **ارتباطات**\n\n')
        + texts.t('ADMIN_COMMUNICATIONS_SUBMENU_DESCRIPTION', 'مدیریت پخش پیام و متون رابط کاربری:'),
        reply_markup=get_admin_communications_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_support_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # Moderators have access only to tickets and not to settings
    is_moderator_only = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
        callback.from_user.id
    )

    kb = get_admin_support_submenu_keyboard(db_user.language)
    if is_moderator_only:
        # Rebuild keyboard to include only tickets and back to main menu
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_SUPPORT_TICKETS', '🎫 تیکت‌های پشتیبانی'), callback_data='admin_tickets'
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
            ]
        )
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_SUBMENU_TITLE', '🛟 **پشتیبانی**\n\n')
        + (
            texts.t('ADMIN_SUPPORT_SUBMENU_DESCRIPTION_MODERATOR', 'دسترسی به تیکت‌ها.')
            if is_moderator_only
            else texts.t('ADMIN_SUPPORT_SUBMENU_DESCRIPTION', 'مدیریت تیکت‌ها و تنظیمات پشتیبانی:')
        ),
        reply_markup=kb,
        parse_mode='Markdown',
    )
    await callback.answer()


# Moderator panel entry (from main menu quick button)
@admin_required
@error_handler
async def show_moderator_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_SUPPORT_TICKETS', '🎫 تیکت‌های پشتیبانی'), callback_data='admin_tickets'
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ منوی اصلی'), callback_data='back_to_menu'
                )
            ],
        ]
    )
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_MODERATION_TITLE', '🧑‍⚖️ <b>مدیریت پشتیبانی</b>')
        + '\n\n'
        + texts.t('ADMIN_SUPPORT_MODERATION_DESCRIPTION', 'دسترسی به تیکت‌های پشتیبانی.'),
        parse_mode='HTML',
        reply_markup=kb,
    )
    await callback.answer()


@admin_required
@error_handler
async def show_support_audit(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # pagination
    page = 1
    if callback.data.startswith('admin_support_audit_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except Exception:
            page = 1
    per_page = 10
    total = await TicketCRUD.count_support_audit(db)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(page, 1)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    logs = await TicketCRUD.list_support_audit(db, limit=per_page, offset=offset)

    lines = [texts.t('ADMIN_SUPPORT_AUDIT_TITLE', '🧾 <b>ممیزی مدیران</b>'), '']
    if not logs:
        lines.append(texts.t('ADMIN_SUPPORT_AUDIT_EMPTY', 'هنوز خالی است'))
    else:
        for log in logs:
            role = (
                texts.t('ADMIN_SUPPORT_AUDIT_ROLE_MODERATOR', 'مدیر میانی')
                if getattr(log, 'is_moderator', False)
                else texts.t('ADMIN_SUPPORT_AUDIT_ROLE_ADMIN', 'ادمین')
            )
            ts = log.created_at.strftime('%d.%m.%Y %H:%M') if getattr(log, 'created_at', None) else ''
            action_map = {
                'close_ticket': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_CLOSE_TICKET', 'بستن تیکت'),
                'block_user_timed': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_BLOCK_TIMED', 'مسدودسازی (موقت)'),
                'block_user_perm': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_BLOCK_PERM', 'مسدودسازی (دائمی)'),
                'close_all_tickets': texts.t(
                    'ADMIN_SUPPORT_AUDIT_ACTION_CLOSE_ALL_TICKETS', 'بستن دسته‌جمعی تیکت‌ها'
                ),
                'unblock_user': texts.t('ADMIN_SUPPORT_AUDIT_ACTION_UNBLOCK', 'رفع مسدودیت'),
            }
            action_text = action_map.get(log.action, log.action)
            ticket_part = f' تیکت #{log.ticket_id}' if log.ticket_id else ''
            details = log.details or {}
            extra = ''
            if log.action == 'block_user_timed' and 'minutes' in details:
                extra = f' ({details["minutes"]} دقیقه)'
            elif log.action == 'close_all_tickets' and 'count' in details:
                extra = f' ({details["count"]})'
            actor_id_display = log.actor_telegram_id or f'user#{log.actor_user_id}' if log.actor_user_id else 'unknown'
            lines.append(f'{ts} • {role} <code>{actor_id_display}</code> — {action_text}{ticket_part}{extra}')

    # keyboard with pagination
    nav_row = []
    if total_pages > 1:
        if page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_support_audit_page_{page - 1}'))
        nav_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_support_audit_page_{page + 1}'))

    kb_rows = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_support')])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=kb)
    await callback.answer()


@admin_required
@error_handler
async def show_settings_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_SETTINGS_SUBMENU_TITLE', '⚙️ **تنظیمات سیستم**\n\n')
        + texts.t('ADMIN_SETTINGS_SUBMENU_DESCRIPTION', 'مدیریت Remnawave، نظارت و سایر تنظیمات:'),
        reply_markup=get_admin_settings_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_system_submenu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_SYSTEM_SUBMENU_TITLE', '🛠️ **توابع سیستم**\n\n')
        + texts.t(
            'ADMIN_SYSTEM_SUBMENU_DESCRIPTION', 'گزارش‌ها، به‌روزرسانی‌ها، لاگ‌ها، نسخه‌های پشتیبان و عملیات سیستم:'
        ),
        reply_markup=get_admin_system_submenu_keyboard(db_user.language),
        parse_mode='Markdown',
    )
    await callback.answer()


@admin_required
@error_handler
async def clear_rules_command(message: types.Message, db_user: User, db: AsyncSession):
    try:
        stats = await get_rules_statistics(db)

        if stats['total_active'] == 0:
            await message.reply(
                'ℹ️ <b>قوانین از قبل پاک شده‌اند</b>\n\n'
                'هیچ قانون فعالی در سیستم وجود ندارد. از قوانین پیش‌فرض استفاده می‌شود.'
            )
            return

        success = await clear_all_rules(db, db_user.language)

        if success:
            clear_rules_cache()

            await message.reply(
                f'✅ <b>قوانین با موفقیت پاک شدند!</b>\n\n'
                f'📊 <b>آمار:</b>\n'
                f'• قوانین پاک‌شده: {stats["total_active"]}\n'
                f'• زبان: {db_user.language}\n'
                f'• اجراکننده: {html.escape(db_user.full_name or "")}\n\n'
                f'اکنون از قوانین پیش‌فرض استفاده می‌شود.'
            )

            logger.info(
                'Rules cleared by admin command', telegram_id=db_user.telegram_id, full_name=db_user.full_name
            )
        else:
            await message.reply('⚠️ <b>هیچ قانونی برای پاک کردن وجود ندارد</b>\n\nقانون فعالی یافت نشد.')

    except Exception as e:
        logger.error('Error clearing rules via command', error=e)
        await message.reply(
            '❌ <b>خطا در پاک کردن قوانین</b>\n\n'
            f'خطایی رخ داد: {e!s}\n'
            'از طریق پنل ادمین تلاش کنید یا بعداً دوباره امتحان کنید.'
        )


@admin_required
@error_handler
async def rules_stats_command(message: types.Message, db_user: User, db: AsyncSession):
    try:
        stats = await get_rules_statistics(db)

        if 'error' in stats:
            await message.reply(f'❌ خطا در دریافت آمار: {stats["error"]}')
            return

        text = '📊 <b>آمار قوانین سرویس</b>\n\n'
        text += '📋 <b>اطلاعات کلی:</b>\n'
        text += f'• قوانین فعال: {stats["total_active"]}\n'
        text += f'• کل تاریخچه: {stats["total_all_time"]}\n'
        text += f'• زبان‌های پشتیبانی‌شده: {stats["total_languages"]}\n\n'

        if stats['languages']:
            text += '🌐 <b>بر اساس زبان:</b>\n'
            for lang, lang_stats in stats['languages'].items():
                text += f'• <code>{lang}</code>: {lang_stats["active_count"]} قانون، '
                text += f'{lang_stats["content_length"]} کاراکتر\n'
                if lang_stats['last_updated']:
                    text += f'  به‌روز شده: {lang_stats["last_updated"].strftime("%d.%m.%Y %H:%M")}\n'
        else:
            text += 'ℹ️ هیچ قانون فعالی وجود ندارد - از قوانین پیش‌فرض استفاده می‌شود'

        await message.reply(text)

    except Exception as e:
        logger.error('Error fetching rules statistics', error=e)
        await message.reply(f'❌ <b>خطا در دریافت آمار</b>\n\nخطایی رخ داد: {e!s}')


@admin_required
@error_handler
async def admin_commands_help(message: types.Message, db_user: User, db: AsyncSession):
    help_text = """
🔧 <b>دستورات ادمین موجود:</b>

<b>📋 مدیریت قوانین:</b>
• <code>/clear_rules</code> - پاک کردن تمام قوانین
• <code>/rules_stats</code> - آمار قوانین

<b>ℹ️ راهنما:</b>
• <code>/admin_help</code> - این پیام

<b>📱 پنل مدیریت:</b>
از دکمه "پنل ادمین" در منوی اصلی برای دسترسی کامل به تمام ویژگی‌ها استفاده کنید.

<b>⚠️ مهم:</b>
تمام دستورات ثبت می‌شوند و نیاز به دسترسی ادمین دارند.
"""

    await message.reply(help_text)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_admin_panel, F.data == 'admin_panel')

    dp.callback_query.register(show_users_submenu, F.data == 'admin_submenu_users')

    dp.callback_query.register(show_promo_submenu, F.data == 'admin_submenu_promo')

    dp.callback_query.register(show_communications_submenu, F.data == 'admin_submenu_communications')

    dp.callback_query.register(show_support_submenu, F.data == 'admin_submenu_support')
    dp.callback_query.register(
        show_support_audit, F.data.in_(['admin_support_audit']) | F.data.startswith('admin_support_audit_page_')
    )

    dp.callback_query.register(show_settings_submenu, F.data == 'admin_submenu_settings')

    dp.callback_query.register(show_system_submenu, F.data == 'admin_submenu_system')
    dp.callback_query.register(show_moderator_panel, F.data == 'moderator_panel')
    # Support settings module
    support_settings_handlers.register_handlers(dp)

    dp.message.register(clear_rules_command, Command('clear_rules'))

    dp.message.register(rules_stats_command, Command('rules_stats'))

    dp.message.register(admin_commands_help, Command('admin_help'))
