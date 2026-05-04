import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import get_admin_main_keyboard, get_maintenance_keyboard
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class MaintenanceStates(StatesGroup):
    waiting_for_reason = State()
    waiting_for_notification_message = State()


@admin_required
@error_handler
async def show_maintenance_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    get_texts(db_user.language)

    status_info = maintenance_service.get_status_info()

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()
        panel_status = await rw_service.get_panel_status_summary()
    except Exception as e:
        logger.error('Error retrieving panel status', error=e)
        panel_status = {'description': '❓ بررسی ناموفق بود', 'has_issues': True}

    status_emoji = '🔧' if status_info['is_active'] else '✅'
    status_text = 'فعال' if status_info['is_active'] else 'غیرفعال'

    api_emoji = '✅' if status_info['api_status'] else '❌'
    api_text = 'در دسترس' if status_info['api_status'] else 'در دسترس نیست'

    monitoring_emoji = '🔄' if status_info['monitoring_active'] else '⏹️'
    monitoring_text = 'در حال اجرا' if status_info['monitoring_active'] else 'متوقف'

    enabled_info = ''
    if status_info['is_active'] and status_info['enabled_at']:
        enabled_time = status_info['enabled_at'].strftime('%d.%m.%Y %H:%M:%S')
        enabled_info = f'\n📅 <b>فعال شده در:</b> {enabled_time}'
        if status_info['reason']:
            enabled_info += f'\n📝 <b>دلیل:</b> {status_info["reason"]}'

    last_check_info = ''
    if status_info['last_check']:
        last_check_time = status_info['last_check'].strftime('%H:%M:%S')
        last_check_info = f'\n🕐 <b>آخرین بررسی:</b> {last_check_time}'

    failures_info = ''
    if status_info['consecutive_failures'] > 0:
        failures_info = f'\n⚠️ <b>بررسی‌های ناموفق متوالی:</b> {status_info["consecutive_failures"]}'

    panel_info = f'\n🌐 <b>پنل Remnawave:</b> {panel_status["description"]}'
    if panel_status.get('response_time'):
        panel_info += f'\n⚡ <b>زمان پاسخ:</b> {panel_status["response_time"]}s'

    message_text = f"""
🔧 <b>مدیریت نگهداری سیستم</b>

{status_emoji} <b>حالت نگهداری:</b> {status_text}
{api_emoji} <b>API Remnawave:</b> {api_text}
{monitoring_emoji} <b>پایش:</b> {monitoring_text}
🛠️ <b>راه‌اندازی خودکار پایش:</b> {'فعال' if status_info['monitoring_configured'] else 'غیرفعال'}
⏱️ <b>بازه بررسی:</b> {status_info['check_interval']}s
🤖 <b>فعال‌سازی خودکار:</b> {'فعال' if status_info['auto_enable_configured'] else 'غیرفعال'}
{panel_info}
{enabled_info}
{last_check_info}
{failures_info}

ℹ️ <i>در حالت نگهداری، کاربران عادی نمی‌توانند از ربات استفاده کنند. مدیران دسترسی کامل دارند.</i>
"""

    await callback.message.edit_text(
        message_text,
        reply_markup=get_maintenance_keyboard(
            db_user.language,
            status_info['is_active'],
            status_info['monitoring_active'],
            panel_status.get('has_issues', False),
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_maintenance_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    is_active = maintenance_service.is_maintenance_active()

    if is_active:
        success = await maintenance_service.disable_maintenance()
        if success:
            await callback.answer('حالت نگهداری غیرفعال شد', show_alert=True)
        else:
            await callback.answer('خطا در غیرفعال کردن حالت نگهداری', show_alert=True)
    else:
        await state.set_state(MaintenanceStates.waiting_for_reason)
        await callback.message.edit_text(
            '🔧 <b>فعال‌سازی حالت نگهداری</b>\n\nدلیل فعال‌سازی را وارد کنید یا برای رد شدن /skip ارسال کنید:',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='maintenance_panel')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_maintenance_reason(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_reason:
        return

    reason = None
    if message.text and message.text != '/skip':
        reason = message.text[:200]

    success = await maintenance_service.enable_maintenance(reason=reason, auto=False)

    if success:
        response_text = 'حالت نگهداری فعال شد'
        if reason:
            response_text += f'\nدلیل: {html.escape(reason)}'
    else:
        response_text = 'خطا در فعال‌سازی حالت نگهداری'

    await message.answer(response_text)
    await state.clear()

    maintenance_service.get_status_info()
    await message.answer(
        'بازگشت به پنل مدیریت نگهداری:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='🔧 پنل نگهداری', callback_data='maintenance_panel')]]
        ),
    )


@admin_required
@error_handler
async def toggle_monitoring(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    status_info = maintenance_service.get_status_info()

    if status_info['monitoring_active']:
        success = await maintenance_service.stop_monitoring()
        message = 'پایش متوقف شد' if success else 'خطا در توقف پایش'
    else:
        success = await maintenance_service.start_monitoring()
        message = 'پایش آغاز شد' if success else 'خطا در راه‌اندازی پایش'

    await callback.answer(message, show_alert=True)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def force_api_check(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('در حال بررسی API...', show_alert=False)

    check_result = await maintenance_service.force_api_check()

    if check_result['success']:
        status_text = 'در دسترس' if check_result['api_available'] else 'در دسترس نیست'
        message = f'API {status_text}\nزمان پاسخ: {check_result["response_time"]}s'
    else:
        message = f'خطا در بررسی: {check_result.get("error", "خطای ناشناخته")}'

    await callback.message.answer(message)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def check_panel_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('در حال بررسی وضعیت پنل...', show_alert=False)

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        status_data = await rw_service.check_panel_health()

        status_text = {
            'online': '🟢 پنل به درستی کار می‌کند',
            'offline': '🔴 پنل در دسترس نیست',
            'degraded': '🟡 پنل با اختلال کار می‌کند',
        }.get(status_data['status'], '❓ وضعیت نامشخص')

        message_parts = [
            '🌐 <b>وضعیت پنل Remnawave</b>\n',
            f'{status_text}',
            f'⚡ زمان پاسخ: {status_data.get("response_time", 0)}s',
            f'👥 کاربران آنلاین: {status_data.get("users_online", 0)}',
            f'🖥️ نودهای آنلاین: {status_data.get("nodes_online", 0)}/{status_data.get("total_nodes", 0)}',
        ]

        attempts_used = status_data.get('attempts_used')
        if attempts_used:
            message_parts.append(f'🔁 تعداد تلاش‌های بررسی: {attempts_used}')

        if status_data.get('api_error'):
            message_parts.append(f'❌ خطا: {status_data["api_error"][:100]}')

        message = '\n'.join(message_parts)

        await callback.message.answer(message, parse_mode='HTML')

    except Exception as e:
        await callback.message.answer(f'❌ خطا در بررسی وضعیت: {e!s}')


@admin_required
@error_handler
async def send_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(MaintenanceStates.waiting_for_notification_message)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='🟢 آنلاین', callback_data='manual_notify_online'),
                types.InlineKeyboardButton(text='🔴 آفلاین', callback_data='manual_notify_offline'),
            ],
            [
                types.InlineKeyboardButton(text='🟡 اختلال', callback_data='manual_notify_degraded'),
                types.InlineKeyboardButton(text='🔧 نگهداری', callback_data='manual_notify_maintenance'),
            ],
            [types.InlineKeyboardButton(text='❌ لغو', callback_data='maintenance_panel')],
        ]
    )

    await callback.message.edit_text(
        '📢 <b>ارسال دستی اطلاعیه</b>\n\nوضعیت مورد نظر را انتخاب کنید:', reply_markup=keyboard
    )


@admin_required
@error_handler
async def handle_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    status_map = {
        'manual_notify_online': 'online',
        'manual_notify_offline': 'offline',
        'manual_notify_degraded': 'degraded',
        'manual_notify_maintenance': 'maintenance',
    }

    status = status_map.get(callback.data)
    if not status:
        await callback.answer('وضعیت ناشناخته')
        return

    await state.update_data(notification_status=status)

    status_names = {
        'online': '🟢 آنلاین',
        'offline': '🔴 آفلاین',
        'degraded': '🟡 اختلال',
        'maintenance': '🔧 نگهداری',
    }

    await callback.message.edit_text(
        f'📢 <b>ارسال اطلاعیه: {status_names[status]}</b>\n\n'
        f'پیام اطلاعیه را وارد کنید یا برای ارسال بدون متن اضافی /skip ارسال کنید:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ لغو', callback_data='maintenance_panel')]]
        ),
    )


@admin_required
@error_handler
async def process_notification_message(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_notification_message:
        return

    data = await state.get_data()
    status = data.get('notification_status')

    if not status:
        await message.answer('خطا: وضعیت انتخاب نشده')
        await state.clear()
        return

    notification_message = ''
    if message.text and message.text != '/skip':
        notification_message = message.text[:300]

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        success = await rw_service.send_manual_status_notification(message.bot, status, notification_message)

        if success:
            await message.answer('✅ اطلاعیه ارسال شد')
        else:
            await message.answer('❌ خطا در ارسال اطلاعیه')

    except Exception as e:
        logger.error('Error sending manual notification', error=e)
        await message.answer(f'❌ خطا: {e!s}')

    await state.clear()

    await message.answer(
        'بازگشت به پنل نگهداری:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='🔧 پنل نگهداری', callback_data='maintenance_panel')]]
        ),
    )


@admin_required
@error_handler
async def back_to_admin_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(texts.ADMIN_PANEL, reply_markup=get_admin_main_keyboard(db_user.language))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_maintenance_panel, F.data == 'maintenance_panel')

    dp.callback_query.register(toggle_maintenance_mode, F.data == 'maintenance_toggle')

    dp.callback_query.register(toggle_monitoring, F.data == 'maintenance_monitoring')

    dp.callback_query.register(force_api_check, F.data == 'maintenance_check_api')

    dp.callback_query.register(check_panel_status, F.data == 'maintenance_check_panel')

    dp.callback_query.register(send_manual_notification, F.data == 'maintenance_manual_notify')

    dp.callback_query.register(handle_manual_notification, F.data.startswith('manual_notify_'))

    dp.callback_query.register(back_to_admin_panel, F.data == 'admin_panel')

    dp.message.register(process_maintenance_reason, MaintenanceStates.waiting_for_reason)

    dp.message.register(process_notification_message, MaintenanceStates.waiting_for_notification_message)
