import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user_message import (
    create_user_message,
    delete_user_message,
    get_all_user_messages,
    get_user_message_by_id,
    get_user_messages_stats,
    toggle_user_message_status,
    update_user_message,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.validators import (
    get_html_help_text,
    sanitize_html,
    validate_html_tags,
)


logger = structlog.get_logger(__name__)


class UserMessageStates(StatesGroup):
    waiting_for_message_text = State()
    waiting_for_edit_text = State()


def get_user_messages_keyboard(language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📝 افزودن پیام', callback_data='add_user_message')],
            [InlineKeyboardButton(text='📋 لیست پیام‌ها', callback_data='list_user_messages:0')],
            [InlineKeyboardButton(text='📊 آمار', callback_data='user_messages_stats')],
            [InlineKeyboardButton(text='🔙 بازگشت به پنل ادمین', callback_data='admin_panel')],
        ]
    )


def get_message_actions_keyboard(message_id: int, is_active: bool, language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    status_text = '🔴 غیرفعال کردن' if is_active else '🟢 فعال کردن'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✏️ ویرایش', callback_data=f'edit_user_message:{message_id}')],
            [InlineKeyboardButton(text=status_text, callback_data=f'toggle_user_message:{message_id}')],
            [InlineKeyboardButton(text='🗑️ حذف', callback_data=f'delete_user_message:{message_id}')],
            [InlineKeyboardButton(text='🔙 به لیست', callback_data='list_user_messages:0')],
        ]
    )


@admin_required
@error_handler
async def show_user_messages_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    get_texts(db_user.language)

    text = (
        '📢 <b>مدیریت پیام‌های منوی اصلی</b>\n\n'
        'در اینجا می‌توانید پیام‌هایی اضافه کنید که به کاربران '
        'در منوی اصلی بین اطلاعات اشتراک و دکمه‌های عمل نمایش داده می‌شوند.\n\n'
        '• پیام‌ها از تگ‌های HTML پشتیبانی می‌کنند\n'
        '• می‌توانید چندین پیام ایجاد کنید\n'
        '• پیام‌های فعال به صورت تصادفی نمایش داده می‌شوند\n'
        '• پیام‌های غیرفعال نمایش داده نمی‌شوند'
    )

    await callback.message.edit_text(text, reply_markup=get_user_messages_keyboard(db_user.language), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def add_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        f'📝 <b>افزودن پیام جدید</b>\n\n'
        f'متن پیامی را که در منوی اصلی نمایش داده می‌شود وارد کنید.\n\n'
        f'{get_html_help_text()}\n\n'
        f'برای لغو /cancel ارسال کنید.',
        parse_mode='HTML',
    )

    await state.set_state(UserMessageStates.waiting_for_message_text)
    await callback.answer()


@admin_required
@error_handler
async def process_new_message_text(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    if message.text == '/cancel':
        await state.clear()
        await message.answer(
            '❌ افزودن پیام لغو شد.', reply_markup=get_user_messages_keyboard(db_user.language)
        )
        return

    message_text = message.text.strip()

    if len(message_text) > 4000:
        await message.answer(
            '❌ پیام خیلی طولانی است. حداکثر ۴۰۰۰ کاراکتر.\n'
            'دوباره امتحان کنید یا برای لغو /cancel ارسال کنید.'
        )
        return

    is_valid, error_msg = validate_html_tags(message_text)
    if not is_valid:
        await message.answer(
            f'❌ خطا در قالب‌بندی HTML: {error_msg}\n\n'
            f'خطا را اصلاح کرده و دوباره امتحان کنید، یا برای لغو /cancel ارسال کنید.',
            parse_mode=None,
        )
        return

    try:
        new_message = await create_user_message(db=db, message_text=message_text, created_by=db_user.id, is_active=True)

        await state.clear()

        await message.answer(
            f'✅ <b>پیام افزوده شد!</b>\n\n'
            f'<b>ID:</b> {new_message.id}\n'
            f'<b>وضعیت:</b> {"🟢 فعال" if new_message.is_active else "🔴 غیرفعال"}\n'
            f'<b>ایجادشده:</b> {new_message.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'
            f'<b>پیش‌نمایش:</b>\n'
            f'<blockquote>{message_text}</blockquote>',
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error('Error creating message', error=e)
        await state.clear()
        await message.answer(
            '❌ خطایی در ایجاد پیام رخ داد. دوباره امتحان کنید.',
            reply_markup=get_user_messages_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def list_user_messages(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = 0
    if ':' in callback.data:
        try:
            page = int(callback.data.split(':')[1])
        except (ValueError, IndexError):
            page = 0

    limit = 5
    offset = page * limit

    messages = await get_all_user_messages(db, offset=offset, limit=limit)

    if not messages:
        await callback.message.edit_text(
            '📋 <b>لیست پیام‌ها</b>\n\nهنوز پیامی وجود ندارد. اولین پیام را اضافه کنید!',
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    text = '📋 <b>لیست پیام‌ها</b>\n\n'

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        preview = msg.message_text[:100] + '...' if len(msg.message_text) > 100 else msg.message_text
        preview = preview.replace('<', '&lt;').replace('>', '&gt;')

        text += f'{status_emoji} <b>ID {msg.id}</b>\n{preview}\n📅 {msg.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = []

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        keyboard.append(
            [InlineKeyboardButton(text=f'{status_emoji} ID {msg.id}', callback_data=f'view_user_message:{msg.id}')]
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'list_user_messages:{page - 1}'))

    nav_buttons.append(InlineKeyboardButton(text='➕ افزودن', callback_data='add_user_message'))

    if len(messages) == limit:
        nav_buttons.append(InlineKeyboardButton(text='جلو ➡️', callback_data=f'list_user_messages:{page + 1}'))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text='🔙 بازگشت', callback_data='user_messages_panel')])

    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def view_user_message(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('❌ شناسه پیام نامعتبر است', show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer('❌ پیام پیدا نشد', show_alert=True)
        return

    safe_content = sanitize_html(message.message_text)

    status_text = '🟢 فعال' if message.is_active else '🔴 غیرفعال'

    text = (
        f'📋 <b>پیام ID {message.id}</b>\n\n'
        f'<b>وضعیت:</b> {status_text}\n'
        f'<b>ایجادشده:</b> {message.created_at.strftime("%d.%m.%Y %H:%M")}\n'
        f'<b>به‌روزرسانی:</b> {message.updated_at.strftime("%d.%m.%Y %H:%M")}\n\n'
        f'<b>محتوا:</b>\n'
        f'<blockquote>{safe_content}</blockquote>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_message_actions_keyboard(message_id, message.is_active, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_message_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('❌ شناسه پیام نامعتبر است', show_alert=True)
        return

    message = await toggle_user_message_status(db, message_id)

    if not message:
        await callback.answer('❌ پیام پیدا نشد', show_alert=True)
        return

    status_text = 'فعال شد' if message.is_active else 'غیرفعال شد'
    await callback.answer(f'✅ پیام {status_text}')

    await view_user_message(callback, db_user, db)


@admin_required
@error_handler
async def delete_message_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """تأیید حذف پیام"""
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('❌ شناسه پیام نامعتبر است', show_alert=True)
        return

    success = await delete_user_message(db, message_id)

    if success:
        await callback.answer('✅ پیام حذف شد')
        await list_user_messages(
            types.CallbackQuery(
                id=callback.id,
                from_user=callback.from_user,
                chat_instance=callback.chat_instance,
                data='list_user_messages:0',
                message=callback.message,
            ),
            db_user,
            db,
        )
    else:
        await callback.answer('❌ خطا در حذف پیام', show_alert=True)


@admin_required
@error_handler
async def show_messages_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_user_messages_stats(db)

    text = (
        '📊 <b>آمار پیام‌ها</b>\n\n'
        f'📝 کل پیام‌ها: <b>{stats["total_messages"]}</b>\n'
        f'🟢 فعال: <b>{stats["active_messages"]}</b>\n'
        f'🔴 غیرفعال: <b>{stats["inactive_messages"]}</b>\n\n'
        'پیام‌های فعال به صورت تصادفی به کاربران '
        'در منوی اصلی بین اطلاعات اشتراک و دکمه‌های عمل نمایش داده می‌شوند.'
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='🔙 بازگشت', callback_data='user_messages_panel')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def edit_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer('❌ شناسه پیام نامعتبر است', show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer('❌ پیام پیدا نشد', show_alert=True)
        return

    await callback.message.edit_text(
        f'✏️ <b>ویرایش پیام ID {message.id}</b>\n\n'
        f'<b>متن فعلی:</b>\n'
        f'<blockquote>{sanitize_html(message.message_text)}</blockquote>\n\n'
        f'متن جدید پیام را وارد کنید یا برای لغو /cancel ارسال کنید:',
        parse_mode='HTML',
    )

    await state.set_data({'editing_message_id': message_id})
    await state.set_state(UserMessageStates.waiting_for_edit_text)
    await callback.answer()


@admin_required
@error_handler
async def process_edit_message_text(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    if message.text == '/cancel':
        await state.clear()
        await message.answer('❌ ویرایش لغو شد.', reply_markup=get_user_messages_keyboard(db_user.language))
        return

    data = await state.get_data()
    message_id = data.get('editing_message_id')

    if not message_id:
        await state.clear()
        await message.answer('❌ خطا: شناسه پیام پیدا نشد')
        return

    new_text = message.text.strip()

    if len(new_text) > 4000:
        await message.answer(
            '❌ پیام خیلی طولانی است. حداکثر ۴۰۰۰ کاراکتر.\n'
            'دوباره امتحان کنید یا برای لغو /cancel ارسال کنید.'
        )
        return

    is_valid, error_msg = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(
            f'❌ خطا در قالب‌بندی HTML: {error_msg}\n\n'
            f'خطا را اصلاح کرده و دوباره امتحان کنید، یا برای لغو /cancel ارسال کنید.',
            parse_mode=None,
        )
        return

    try:
        updated_message = await update_user_message(db=db, message_id=message_id, message_text=new_text)

        if updated_message:
            await state.clear()
            await message.answer(
                f'✅ <b>پیام به‌روزرسانی شد!</b>\n\n'
                f'<b>ID:</b> {updated_message.id}\n'
                f'<b>به‌روزرسانی‌شده:</b> {updated_message.updated_at.strftime("%d.%m.%Y %H:%M")}\n\n'
                f'<b>متن جدید:</b>\n'
                f'<blockquote>{sanitize_html(new_text)}</blockquote>',
                reply_markup=get_user_messages_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await state.clear()
            await message.answer(
                '❌ پیام پیدا نشد یا خطا در به‌روزرسانی.',
                reply_markup=get_user_messages_keyboard(db_user.language),
            )

    except Exception as e:
        logger.error('Error updating message', error=e)
        await state.clear()
        await message.answer(
            '❌ خطایی در به‌روزرسانی پیام رخ داد.', reply_markup=get_user_messages_keyboard(db_user.language)
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_user_messages_panel, F.data == 'user_messages_panel')

    dp.callback_query.register(add_user_message_start, F.data == 'add_user_message')

    dp.message.register(process_new_message_text, StateFilter(UserMessageStates.waiting_for_message_text))

    dp.callback_query.register(edit_user_message_start, F.data.startswith('edit_user_message:'))

    dp.message.register(process_edit_message_text, StateFilter(UserMessageStates.waiting_for_edit_text))

    dp.callback_query.register(list_user_messages, F.data.startswith('list_user_messages'))

    dp.callback_query.register(view_user_message, F.data.startswith('view_user_message:'))

    dp.callback_query.register(toggle_message_status, F.data.startswith('toggle_user_message:'))

    dp.callback_query.register(delete_message_confirm, F.data.startswith('delete_user_message:'))

    dp.callback_query.register(show_messages_stats, F.data == 'user_messages_stats')
