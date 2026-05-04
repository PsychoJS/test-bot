import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.welcome_text import (
    get_available_placeholders,
    get_current_welcome_text_or_default,
    get_current_welcome_text_settings,
    set_welcome_text,
    toggle_welcome_text_status,
)
from app.database.models import User
from app.keyboards.admin import get_welcome_text_keyboard
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def validate_html_tags(text: str) -> tuple[bool, str]:
    """
    Проверяет HTML-теги в тексте на соответствие требованиям Telegram API.

    Args:
        text: Текст для проверки

    Returns:
        Кортеж из (валидно ли, сообщение об ошибке или None)
    """
    # Поддерживаемые теги в parse_mode="HTML" для Telegram API
    allowed_tags = {
        'b',
        'strong',  # жирный
        'i',
        'em',  # курсив
        'u',
        'ins',  # подчеркнуто
        's',
        'strike',
        'del',  # зачеркнуто
        'code',  # моноширинный для коротких фрагментов
        'pre',  # моноширинный блок кода
        'a',  # ссылки
    }

    # Убираем плейсхолдеры из строки перед проверкой тегов
    # Плейсхолдеры имеют формат {ключ}, и не являются тегами
    placeholder_pattern = r'\{[^{}]+\}'
    clean_text = re.sub(placeholder_pattern, '', text)

    # Находим все открывающие и закрывающие теги
    tag_pattern = r'<(/?)([a-zA-Z]+)(\s[^>]*)?>'
    tags_with_pos = [
        (m.group(1), m.group(2), m.group(3), m.start(), m.end()) for m in re.finditer(tag_pattern, clean_text)
    ]

    for closing, tag, attrs, start_pos, end_pos in tags_with_pos:
        tag_lower = tag.lower()

        # Проверяем, является ли тег поддерживаемым
        if tag_lower not in allowed_tags:
            return (
                False,
                f'تگ HTML پشتیبانی نمی‌شود: <{tag}>. فقط از تگ‌های زیر استفاده کنید: {", ".join(sorted(allowed_tags))}',
            )

        # Проверяем атрибуты для тега <a>
        if tag_lower == 'a':
            if closing:
                continue  # Для закрывающего тега не нужно проверять атрибуты
            if not attrs:
                return False, "تگ <a> باید دارای ویژگی href باشد، مثال: <a href='URL'>لینک</a>"

            # Проверяем, что есть атрибут href
            if 'href=' not in attrs.lower():
                return False, "تگ <a> باید دارای ویژگی href باشد، مثال: <a href='URL'>لینک</a>"

            # Проверяем формат URL
            href_match = re.search(r'href\s*=\s*[\'"]([^\'"]+)[\'"]', attrs, re.IGNORECASE)
            if href_match:
                url = href_match.group(1)
                # Проверяем, что URL начинается с поддерживаемой схемы
                if not re.match(r'^https?://|^tg://', url, re.IGNORECASE):
                    return False, f'URL در تگ <a> باید با http://، https:// یا tg:// شروع شود. مقدار یافت‌شده: {url}'
            else:
                return False, 'نتوانستیم URL را از ویژگی href تگ <a> استخراج کنیم'

    # Проверяем парность тегов с использованием стека
    stack = []
    for closing, tag, attrs, start_pos, end_pos in tags_with_pos:
        tag_lower = tag.lower()

        if tag_lower not in allowed_tags:
            continue

        if closing:
            # Это закрывающий тег
            if not stack:
                return False, f'تگ بسته‌شونده اضافی: </{tag}>'

            last_opening_tag = stack.pop()
            if last_opening_tag.lower() != tag_lower:
                return False, f'تگ </{tag}> با تگ باز‌شونده <{last_opening_tag}> مطابقت ندارد'
        else:
            # Это открывающий тег
            stack.append(tag)

    # Если остались незакрытые теги
    if stack:
        unclosed_tags = ', '.join([f'<{tag}>' for tag in stack])
        return False, f'تگ‌های بسته‌نشده: {unclosed_tags}'

    return True, None


def get_telegram_formatting_info() -> str:
    return """
📝 <b>تگ‌های قالب‌بندی پشتیبانی‌شده:</b>

• <code>&lt;b&gt;متن ضخیم&lt;/b&gt;</code> → <b>متن ضخیم</b>
• <code>&lt;i&gt;ایتالیک&lt;/i&gt;</code> → <i>ایتالیک</i>
• <code>&lt;u&gt;زیرخط‌دار&lt;/u&gt;</code> → <u>زیرخط‌دار</u>
• <code>&lt;s&gt;خط‌خورده&lt;/s&gt;</code> → <s>خط‌خورده</s>
• <code>&lt;code&gt;تک‌فضایی&lt;/code&gt;</code> → <code>تک‌فضایی</code>
• <code>&lt;pre&gt;بلوک کد&lt;/pre&gt;</code> → کد چندخطی
• <code>&lt;a href="URL"&gt;لینک&lt;/a&gt;</code> → لینک

⚠️ <b>توجه:</b> فقط از تگ‌های ذکرشده بالا استفاده کنید!
هر تگ HTML دیگری پشتیبانی نمی‌شود و به صورت متن معمولی نمایش داده خواهد شد.

❌ <b>استفاده نکنید از:</b> &lt;div&gt;, &lt;span&gt;, &lt;p&gt;, &lt;br&gt;, &lt;h1&gt;-&lt;h6&gt;, &lt;img&gt; و سایر تگ‌های HTML.
"""


@admin_required
@error_handler
async def show_welcome_text_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    welcome_settings = await get_current_welcome_text_settings(db)
    status_emoji = '🟢' if welcome_settings['is_enabled'] else '🔴'
    status_text = 'فعال' if welcome_settings['is_enabled'] else 'غیرفعال'

    await callback.message.edit_text(
        f'👋 مدیریت متن خوش‌آمدگویی\n\n'
        f'{status_emoji} <b>وضعیت:</b> {status_text}\n\n'
        f'در اینجا می‌توانید متنی را که پس از ثبت‌نام به کاربران جدید نمایش داده می‌شود مدیریت کنید.\n\n'
        f'💡 متغیرهای موجود برای جایگزینی خودکار:',
        reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_welcome_text(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    new_status = await toggle_welcome_text_status(db, db_user.id)

    status_emoji = '🟢' if new_status else '🔴'
    status_text = 'فعال' if new_status else 'غیرفعال'
    action_text = 'فعال شدند' if new_status else 'غیرفعال شدند'

    await callback.message.edit_text(
        f'👋 مدیریت متن خوش‌آمدگویی\n\n'
        f'{status_emoji} <b>وضعیت:</b> {status_text}\n\n'
        f'✅ پیام‌های خوش‌آمدگویی {action_text}!\n\n'
        f'در اینجا می‌توانید متنی را که پس از ثبت‌نام به کاربران جدید نمایش داده می‌شود مدیریت کنید.\n\n'
        f'💡 متغیرهای موجود برای جایگزینی خودکار:',
        reply_markup=get_welcome_text_keyboard(db_user.language, new_status),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_current_welcome_text(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    welcome_settings = await get_current_welcome_text_settings(db)
    current_text = welcome_settings['text']
    is_enabled = welcome_settings['is_enabled']

    if not welcome_settings['id']:
        status = '📝 متن پیش‌فرض استفاده می‌شود:'
    else:
        status = '📝 متن خوش‌آمدگویی فعلی:'

    status_emoji = '🟢' if is_enabled else '🔴'
    status_text = 'فعال' if is_enabled else 'غیرفعال'

    placeholders = get_available_placeholders()
    placeholders_text = '\n'.join([f'• <code>{key}</code> - {desc}' for key, desc in placeholders.items()])

    await callback.message.edit_text(
        f'{status_emoji} <b>وضعیت:</b> {status_text}\n\n'
        f'{status}\n\n'
        f'<code>{current_text}</code>\n\n'
        f'💡 متغیرهای موجود:\n{placeholders_text}',
        reply_markup=get_welcome_text_keyboard(db_user.language, is_enabled),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_placeholders_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    welcome_settings = await get_current_welcome_text_settings(db)
    placeholders = get_available_placeholders()
    placeholders_text = '\n'.join([f'• <code>{key}</code>\n  {desc}' for key, desc in placeholders.items()])

    help_text = (
        '💡 متغیرهای موجود برای جایگزینی خودکار:\n\n'
        f'{placeholders_text}\n\n'
        '📌 مثال‌های استفاده:\n'
        '• <code>سلام، {user_name}! خوش آمدید!</code>\n'
        '• <code>درود، {first_name}! خوشحالیم که اینجا هستید!</code>\n'
        '• <code>سلام، {username}! ممنون از ثبت‌نام شما!</code>\n\n'
        "اگر داده‌های کاربر موجود نباشد، از کلمه 'دوست' استفاده می‌شود."
    )

    await callback.message.edit_text(
        help_text,
        reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_formatting_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    welcome_settings = await get_current_welcome_text_settings(db)
    formatting_info = get_telegram_formatting_info()

    await callback.message.edit_text(
        formatting_info,
        reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_welcome_text(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    welcome_settings = await get_current_welcome_text_settings(db)
    current_text = welcome_settings['text']

    placeholders = get_available_placeholders()
    placeholders_text = '\n'.join([f'• <code>{key}</code> - {desc}' for key, desc in placeholders.items()])

    await callback.message.edit_text(
        f'📝 ویرایش متن خوش‌آمدگویی\n\n'
        f'متن فعلی:\n'
        f'<code>{current_text}</code>\n\n'
        f'💡 متغیرهای موجود:\n{placeholders_text}\n\n'
        f'متن جدید را ارسال کنید:',
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.editing_welcome_text)
    await callback.answer()


@admin_required
@error_handler
async def process_welcome_text_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    new_text = message.text.strip()

    if len(new_text) < 10:
        await message.answer('❌ متن خیلی کوتاه است! حداقل ۱۰ کاراکتر.')
        return

    if len(new_text) > 4000:
        await message.answer('❌ متن خیلی طولانی است! حداکثر ۴۰۰۰ کاراکتر.')
        return

    # Проверяем HTML-теги на валидность
    is_valid, error_msg = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(f'❌ خطا در قالب‌بندی HTML:\n\n{error_msg}')
        return

    success = await set_welcome_text(db, new_text, db_user.id)

    if success:
        welcome_settings = await get_current_welcome_text_settings(db)
        status_emoji = '🟢' if welcome_settings['is_enabled'] else '🔴'
        status_text = 'فعال' if welcome_settings['is_enabled'] else 'غیرفعال'

        placeholders = get_available_placeholders()
        placeholders_text = '\n'.join([f'• <code>{key}</code>' for key in placeholders.keys()])

        await message.answer(
            f'✅ متن خوش‌آمدگویی با موفقیت به‌روزرسانی شد!\n\n'
            f'{status_emoji} <b>وضعیت:</b> {status_text}\n\n'
            f'متن جدید:\n'
            f'<code>{new_text}</code>\n\n'
            f'💡 متغیرهای زیر جایگزین خواهند شد: {placeholders_text}',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
            parse_mode='HTML',
        )
    else:
        welcome_settings = await get_current_welcome_text_settings(db)
        await message.answer(
            '❌ خطا در ذخیره متن. دوباره امتحان کنید.',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
        )

    await state.clear()


@admin_required
@error_handler
async def reset_welcome_text(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    default_text = await get_current_welcome_text_or_default()
    success = await set_welcome_text(db, default_text, db_user.id)

    if success:
        welcome_settings = await get_current_welcome_text_settings(db)
        status_emoji = '🟢' if welcome_settings['is_enabled'] else '🔴'
        status_text = 'فعال' if welcome_settings['is_enabled'] else 'غیرفعال'

        await callback.message.edit_text(
            f'✅ متن خوش‌آمدگویی به پیش‌فرض بازنشانی شد!\n\n'
            f'{status_emoji} <b>وضعیت:</b> {status_text}\n\n'
            f'متن پیش‌فرض:\n'
            f'<code>{default_text}</code>\n\n'
            f'💡 متغیر <code>{{user_name}}</code> با نام کاربر جایگزین می‌شود',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
            parse_mode='HTML',
        )
    else:
        welcome_settings = await get_current_welcome_text_settings(db)
        await callback.message.edit_text(
            '❌ خطا در بازنشانی متن. دوباره امتحان کنید.',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_preview_welcome_text(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.database.crud.welcome_text import get_welcome_text_for_user

    class TestUser:
        def __init__(self):
            self.first_name = 'علی'
            self.username = 'test_user'

    test_user = TestUser()
    preview_text = await get_welcome_text_for_user(db, test_user)

    welcome_settings = await get_current_welcome_text_settings(db)

    if preview_text:
        await callback.message.edit_text(
            f'👁️ پیش‌نمایش\n\n'
            f"نمایش متن برای کاربر 'علی' (@test_user):\n\n"
            f'<code>{preview_text}</code>',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            '👁️ پیش‌نمایش\n\n'
            '🔴 پیام‌های خوش‌آمدگویی غیرفعال هستند.\n'
            'کاربران جدید پس از ثبت‌نام متن خوش‌آمدگویی دریافت نخواهند کرد.',
            reply_markup=get_welcome_text_keyboard(db_user.language, welcome_settings['is_enabled']),
            parse_mode='HTML',
        )

    await callback.answer()


def register_welcome_text_handlers(dp: Dispatcher):
    dp.callback_query.register(show_welcome_text_panel, F.data == 'welcome_text_panel')

    dp.callback_query.register(toggle_welcome_text, F.data == 'toggle_welcome_text')

    dp.callback_query.register(show_current_welcome_text, F.data == 'show_welcome_text')

    dp.callback_query.register(show_placeholders_help, F.data == 'show_placeholders_help')

    dp.callback_query.register(show_formatting_help, F.data == 'show_formatting_help')

    dp.callback_query.register(show_preview_welcome_text, F.data == 'preview_welcome_text')

    dp.callback_query.register(start_edit_welcome_text, F.data == 'edit_welcome_text')

    dp.callback_query.register(reset_welcome_text, F.data == 'reset_welcome_text')

    dp.message.register(process_welcome_text_edit, AdminStates.editing_welcome_text)
