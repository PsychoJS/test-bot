import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.version_service import version_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def get_updates_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text='🔄 بررسی به‌روزرسانی‌ها', callback_data='admin_updates_check')],
        [InlineKeyboardButton(text='📋 اطلاعات نسخه', callback_data='admin_updates_info')],
        [
            InlineKeyboardButton(
                text='🔗 باز کردن مخزن', url=f'https://github.com/{version_service.repo}/releases'
            )
        ],
        [InlineKeyboardButton(text='◀️ بازگشت', callback_data='admin_panel')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_version_info_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text='🔄 به‌روزرسانی', callback_data='admin_updates_info')],
        [InlineKeyboardButton(text='◀️ بازگشت به به‌روزرسانی‌ها', callback_data='admin_updates')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def show_updates_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        version_info = await version_service.get_version_info()

        current_version = version_info['current_version']
        has_updates = version_info['has_updates']
        total_newer = version_info['total_newer']
        last_check = version_info['last_check']

        status_icon = '🆕' if has_updates else '✅'
        status_text = f'{total_newer} به‌روزرسانی در دسترس است' if has_updates else 'به‌روز است'

        last_check_text = ''
        if last_check:
            last_check_text = f'\n🕐 آخرین بررسی: {last_check.strftime("%d.%m.%Y %H:%M")}'

        message = f"""🔄 <b>سیستم به‌روزرسانی</b>

📦 <b>نسخه فعلی:</b> <code>{current_version}</code>
{status_icon} <b>وضعیت:</b> {status_text}

🔗 <b>مخزن:</b> {version_service.repo}{last_check_text}

ℹ️ سیستم هر ساعت به‌طور خودکار به‌روزرسانی‌ها را بررسی می‌کند و درباره نسخه‌های جدید اطلاعیه ارسال می‌نماید."""

        await callback.message.edit_text(
            message, reply_markup=get_updates_keyboard(db_user.language), parse_mode='HTML'
        )
        await callback.answer()

    except Exception as e:
        if 'message is not modified' in str(e).lower():
            logger.debug('📝 Message not modified in show_updates_menu')
            await callback.answer()
            return
        logger.error('Error showing updates menu', error=e)
        await callback.answer('❌ خطا در بارگذاری منوی به‌روزرسانی', show_alert=True)


@admin_required
@error_handler
async def check_updates(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('🔄 در حال بررسی به‌روزرسانی‌ها...')

    try:
        has_updates, newer_releases = await version_service.check_for_updates(force=True)

        if not has_updates:
            message = f"""✅ <b>به‌روزرسانی وجود ندارد</b>

📦 <b>نسخه فعلی:</b> <code>{version_service.current_version}</code>
🎯 <b>وضعیت:</b> آخرین نسخه نصب شده است

🔗 <b>مخزن:</b> {version_service.repo}"""

        else:
            updates_list = []
            for i, release in enumerate(newer_releases[:5]):
                icon = version_service.format_version_display(release).split()[0]
                updates_list.append(f'{i + 1}. {icon} <code>{release.tag_name}</code> • {release.formatted_date}')

            updates_text = '\n'.join(updates_list)
            more_text = f'\n\n📋 و {len(newer_releases) - 5} به‌روزرسانی دیگر...' if len(newer_releases) > 5 else ''

            message = f"""🆕 <b>به‌روزرسانی در دسترس است</b>

📦 <b>نسخه فعلی:</b> <code>{version_service.current_version}</code>
🎯 <b>به‌روزرسانی‌های موجود:</b> {len(newer_releases)}

📋 <b>آخرین نسخه‌ها:</b>
{updates_text}{more_text}

🔗 <b>مخزن:</b> {version_service.repo}"""

        keyboard = get_updates_keyboard(db_user.language)

        if has_updates:
            keyboard.inline_keyboard.insert(
                -2, [InlineKeyboardButton(text='📋 جزئیات نسخه‌ها', callback_data='admin_updates_info')]
            )

        await callback.message.edit_text(message, reply_markup=keyboard, parse_mode='HTML')

    except Exception as e:
        if 'message is not modified' in str(e).lower():
            logger.debug('📝 Message not modified in check_updates')
            return
        logger.error('Error checking updates', error=e)
        await callback.message.edit_text(
            f'❌ <b>خطا در بررسی به‌روزرسانی‌ها</b>\n\n'
            f'ارتباط با سرور GitHub برقرار نشد.\n'
            f'لطفاً بعداً دوباره امتحان کنید.\n\n'
            f'📦 <b>نسخه فعلی:</b> <code>{version_service.current_version}</code>',
            reply_markup=get_updates_keyboard(db_user.language),
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def show_version_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('📋 در حال بارگذاری اطلاعات نسخه‌ها...')

    try:
        version_info = await version_service.get_version_info()

        current_version = version_info['current_version']
        current_release = version_info['current_release']
        newer_releases = version_info['newer_releases']
        has_updates = version_info['has_updates']
        last_check = version_info['last_check']

        current_info = '📦 <b>نسخه فعلی</b>\n\n'

        if current_release:
            current_info += f'🏷️ <b>نسخه:</b> <code>{current_release.tag_name}</code>\n'
            current_info += f'📅 <b>تاریخ انتشار:</b> {current_release.formatted_date}\n'
            if current_release.short_description:
                current_info += f'📝 <b>توضیحات:</b>\n{current_release.short_description}\n'
        else:
            current_info += f'🏷️ <b>نسخه:</b> <code>{current_version}</code>\n'
            current_info += 'ℹ️ <b>وضعیت:</b> اطلاعات انتشار در دسترس نیست\n'

        message_parts = [current_info]

        if has_updates and newer_releases:
            updates_info = '\n🆕 <b>به‌روزرسانی‌های در دسترس</b>\n\n'

            for i, release in enumerate(newer_releases):
                icon = '🔥' if i == 0 else '📦'
                if release.prerelease:
                    icon = '🧪'
                elif release.is_dev:
                    icon = '🔧'

                updates_info += f'{icon} <b>{release.tag_name}</b>\n'
                updates_info += f'   📅 {release.formatted_date}\n'
                if release.short_description:
                    updates_info += f'   📝 {release.short_description}\n'
                updates_info += '\n'

            message_parts.append(updates_info.rstrip())

        system_info = '\n🔧 <b>سیستم به‌روزرسانی</b>\n\n'
        system_info += f'🔗 <b>مخزن:</b> {version_service.repo}\n'
        system_info += f'⚡ <b>بررسی خودکار:</b> {"فعال" if version_service.enabled else "غیرفعال"}\n'
        system_info += '🕐 <b>بازه زمانی:</b> هر ساعت\n'

        if last_check:
            system_info += f'🕐 <b>آخرین بررسی:</b> {last_check.strftime("%d.%m.%Y %H:%M")}\n'

        message_parts.append(system_info.rstrip())

        final_message = '\n'.join(message_parts)

        if len(final_message) > 4000:
            final_message = final_message[:3900] + '\n\n... (اطلاعات کوتاه شد)'

        await callback.message.edit_text(
            final_message,
            reply_markup=get_version_info_keyboard(db_user.language),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    except Exception as e:
        if 'message is not modified' in str(e).lower():
            logger.debug('📝 Message not modified in show_version_info')
            return
        logger.error('Error retrieving version information', error=e)
        await callback.message.edit_text(
            f'❌ <b>خطا در بارگذاری</b>\n\n'
            f'دریافت اطلاعات نسخه‌ها ناموفق بود.\n\n'
            f'📦 <b>نسخه فعلی:</b> <code>{version_service.current_version}</code>',
            reply_markup=get_version_info_keyboard(db_user.language),
            parse_mode='HTML',
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_updates_menu, F.data == 'admin_updates')

    dp.callback_query.register(check_updates, F.data == 'admin_updates_check')

    dp.callback_query.register(show_version_info, F.data == 'admin_updates_info')
