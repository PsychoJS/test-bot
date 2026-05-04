"""Admin handler for managing required channel subscriptions."""

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    get_channel_by_id,
    toggle_channel,
    validate_channel_id,
)
from app.database.database import AsyncSessionLocal
from app.services.channel_subscription_service import channel_subscription_service
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_required_channels')


class AddChannelStates(StatesGroup):
    waiting_channel_id = State()
    waiting_channel_link = State()
    waiting_channel_title = State()


# -- List channels ----------------------------------------------------------------


def _channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        status = '✅' if ch.is_active else '❌'
        title = ch.title or ch.channel_id
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{status} {title}',
                    callback_data=f'reqch:view:{ch.id}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text='➕ افزودن کانال', callback_data='reqch:add')])
    buttons.append([InlineKeyboardButton(text='◀️ بازگشت', callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _channel_detail_keyboard(channel_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = '❌ غیرفعال کردن' if is_active else '✅ فعال کردن'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f'reqch:toggle:{channel_id}')],
            [InlineKeyboardButton(text='🗑 حذف', callback_data=f'reqch:delete:{channel_id}')],
            [InlineKeyboardButton(text='◀️ به لیست', callback_data='reqch:list')],
        ]
    )


@router.callback_query(F.data == 'reqch:list')
@admin_required
async def show_channels_list(callback: CallbackQuery, **kwargs) -> None:
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    if not channels:
        text = '<b>📢 کانال‌های اجباری</b>\n\nکانالی تنظیم نشده است. برای ایجاد «افزودن» را بزنید.'
    else:
        lines = ['<b>📢 کانال‌های اجباری</b>\n']
        for ch in channels:
            status = '✅' if ch.is_active else '❌'
            title = ch.title or ch.channel_id
            lines.append(f'{status} <code>{ch.channel_id}</code> — {title}')
        text = '\n'.join(lines)

    await callback.message.edit_text(text, reply_markup=_channels_keyboard(channels))
    await callback.answer()


@router.callback_query(F.data.startswith('reqch:view:'))
@admin_required
async def view_channel(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('شناسه کانال نامعتبر است', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await get_channel_by_id(db, channel_db_id)

    if not ch:
        await callback.answer('کانال پیدا نشد', show_alert=True)
        return

    status = '✅ فعال' if ch.is_active else '❌ غیرفعال'
    text = (
        f'<b>{ch.title or "بدون عنوان"}</b>\n\n'
        f'<b>ID:</b> <code>{ch.channel_id}</code>\n'
        f'<b>لینک:</b> {ch.channel_link or "—"}\n'
        f'<b>وضعیت:</b> {status}\n'
        f'<b>ترتیب:</b> {ch.sort_order}'
    )

    await callback.message.edit_text(text, reply_markup=_channel_detail_keyboard(ch.id, ch.is_active))
    await callback.answer()


# -- Toggle / Delete ---------------------------------------------------------------


@router.callback_query(F.data.startswith('reqch:toggle:'))
@admin_required
async def toggle_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('شناسه کانال نامعتبر است', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ch = await toggle_channel(db, channel_db_id)

    if ch:
        await channel_subscription_service.invalidate_channels_cache()
        status = 'فعال شد' if ch.is_active else 'غیرفعال شد'
        await callback.answer(f'کانال {status}', show_alert=True)

    # Refresh list
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        '<b>📢 کانال‌های اجباری</b>',
        reply_markup=_channels_keyboard(channels),
    )


@router.callback_query(F.data.startswith('reqch:delete:'))
@admin_required
async def delete_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer('شناسه کانال نامعتبر است', show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        ok = await delete_channel(db, channel_db_id)

    if ok:
        await channel_subscription_service.invalidate_channels_cache()
        await callback.answer('کانال حذف شد', show_alert=True)
    else:
        await callback.answer('خطا در حذف', show_alert=True)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        '<b>📢 کانال‌های اجباری</b>',
        reply_markup=_channels_keyboard(channels),
    )


# -- Add channel flow --------------------------------------------------------------


@router.callback_query(F.data == 'reqch:add')
@admin_required
async def start_add_channel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    await state.set_state(AddChannelStates.waiting_channel_id)
    await callback.message.edit_text(
        '<b>➕ افزودن کانال</b>\n\n'
        'شناسه عددی کانال را ارسال کنید (مثلاً <code>1234567890</code>).\n'
        'پیشوند <code>-100</code> به طور خودکار افزوده می‌شود.'
    )
    await callback.answer()


@router.message(AddChannelStates.waiting_channel_id)
@admin_required
async def process_channel_id(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('یک پیام متنی ارسال کنید.')
        return
    channel_id = message.text.strip()

    # Validate and normalize channel_id (auto-prefixes -100 for bare digits)
    try:
        channel_id = validate_channel_id(channel_id)
    except ValueError as e:
        await message.answer(f'فرمت نامعتبر. {e}\n\nدوباره امتحان کنید:')
        return

    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannelStates.waiting_channel_link)
    await message.answer(
        f'کانال: <code>{channel_id}</code>\n\n'
        'حالا لینک کانال را ارسال کنید (مثلاً <code>https://t.me/mychannel</code>)\n'
        'یا <code>-</code> ارسال کنید تا رد شوید:'
    )


@router.message(AddChannelStates.waiting_channel_link)
@admin_required
async def process_channel_link(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('یک پیام متنی ارسال کنید.')
        return
    link = message.text.strip()
    if link == '-':
        link = None

    if link is not None:
        # Validate and normalize channel link
        if not link.startswith(('https://t.me/', 'http://t.me/', '@')):
            await message.answer('لینک باید URL از نوع t.me یا @username باشد. دوباره امتحان کنید:')
            return
        if link.startswith('@'):
            link = f'https://t.me/{link[1:]}'
        if link.startswith('http://'):
            link = link.replace('http://', 'https://', 1)

    await state.update_data(channel_link=link)
    await state.set_state(AddChannelStates.waiting_channel_title)
    await message.answer(
        'نام کانال را ارسال کنید (مثلاً <code>اخبار پروژه</code>)\n'
        'یا <code>-</code> ارسال کنید تا رد شوید:'
    )


@router.message(AddChannelStates.waiting_channel_title)
@admin_required
async def process_channel_title(message: Message, state: FSMContext, **kwargs) -> None:
    if not message.text:
        await message.answer('یک پیام متنی ارسال کنید.')
        return
    title = message.text.strip()
    if title == '-':
        title = None

    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        try:
            ch = await add_channel(
                db,
                channel_id=data['channel_id'],
                channel_link=data.get('channel_link'),
                title=title,
            )
            await channel_subscription_service.invalidate_channels_cache()

            text = (
                '✅ کانال اضافه شد!\n\n'
                f'<b>ID:</b> <code>{ch.channel_id}</code>\n'
                f'<b>لینک:</b> {ch.channel_link or "—"}\n'
                f'<b>نام:</b> {ch.title or "—"}'
            )
        except Exception as e:
            text = '❌ خطا در افزودن کانال. دوباره امتحان کنید.'
            logger.error('Error adding channel', error=e)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    await message.answer(text, reply_markup=_channels_keyboard(channels))


def register_handlers(dp_router: Router) -> None:
    dp_router.include_router(router)
