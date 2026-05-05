import html
import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_contest import (
    add_virtual_participant,
    create_referral_contest,
    delete_referral_contest,
    delete_virtual_participant,
    get_contest_events_count,
    get_contest_leaderboard_with_virtual,
    get_referral_contest,
    get_referral_contests_count,
    list_referral_contests,
    list_virtual_participants,
    toggle_referral_contest,
    update_referral_contest,
    update_virtual_participant_count,
)
from app.keyboards.admin import (
    get_admin_contests_keyboard,
    get_admin_contests_root_keyboard,
    get_admin_pagination_keyboard,
    get_contest_mode_keyboard,
    get_referral_contest_manage_keyboard,
)
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

PAGE_SIZE = 5


def _ensure_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning('Failed to load TZ, falling back to UTC', tz_name=tz_name)
        return ZoneInfo('UTC')


def _format_contest_summary(contest, texts, tz: ZoneInfo) -> str:
    start_local = contest.start_at if contest.start_at.tzinfo else contest.start_at.replace(tzinfo=UTC)
    end_local = contest.end_at if contest.end_at.tzinfo else contest.end_at.replace(tzinfo=UTC)
    start_local = start_local.astimezone(tz)
    end_local = end_local.astimezone(tz)

    status = (
        texts.t('ADMIN_CONTEST_STATUS_ACTIVE', '🟢 فعال')
        if contest.is_active
        else texts.t('ADMIN_CONTEST_STATUS_INACTIVE', '⚪️ غیرفعال')
    )

    period = f'{start_local.strftime("%d.%m %H:%M")} — {end_local.strftime("%d.%m %H:%M")} ({tz.key})'

    summary_time = contest.daily_summary_time.strftime('%H:%M') if contest.daily_summary_time else '12:00'
    summary_times = contest.daily_summary_times or summary_time
    parts = [
        f'{status}',
        f'دوره: <b>{period}</b>',
        f'خلاصه روزانه: <b>{summary_times}</b>',
    ]
    if contest.prize_text:
        parts.append(texts.t('ADMIN_CONTEST_PRIZE', 'جایزه: {prize}').format(prize=html.escape(contest.prize_text)))
    if contest.last_daily_summary_date:
        parts.append(
            texts.t('ADMIN_CONTEST_LAST_DAILY', 'آخرین خلاصه: {date}').format(
                date=contest.last_daily_summary_date.strftime('%d.%m')
            )
        )
    return '\n'.join(parts)


def _parse_local_datetime(value: str, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.strptime(value.strip(), '%d.%m.%Y %H:%M')
    except ValueError:
        return None
    return dt.replace(tzinfo=tz)


def _parse_time(value: str):
    try:
        return datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError:
        return None


def _parse_times(value: str) -> list[time]:
    times: list[time] = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        parsed = _parse_time(part)
        if parsed:
            times.append(parsed)
    return times


@admin_required
@error_handler
async def show_contests_menu(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    if not settings.is_contests_enabled():
        await callback.message.edit_text(
            texts.t(
                'ADMIN_CONTESTS_DISABLED',
                'مسابقات از طریق متغیر محیطی CONTESTS_ENABLED غیرفعال شده‌اند.',
            ),
            reply_markup=get_admin_contests_root_keyboard(db_user.language),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        texts.t('ADMIN_CONTESTS_TITLE', '🏆 <b>مسابقات</b>\n\nیک عملیات را انتخاب کنید:'),
        reply_markup=get_admin_contests_root_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_referral_contests_menu(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_CONTESTS_TITLE', '🏆 <b>مسابقات</b>\n\nیک عملیات را انتخاب کنید:'),
        reply_markup=get_admin_contests_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def list_contests(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t(
                'ADMIN_CONTESTS_DISABLED',
                'مسابقات از طریق متغیر محیطی CONTESTS_ENABLED غیرفعال شده‌اند.',
            ),
            show_alert=True,
        )
        return

    page = 1
    if callback.data.startswith('admin_contests_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except Exception:
            page = 1

    total = await get_referral_contests_count(db)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE

    contests = await list_referral_contests(db, limit=PAGE_SIZE, offset=offset)
    texts = get_texts(db_user.language)

    lines = [texts.t('ADMIN_CONTESTS_LIST_HEADER', '🏆 <b>مسابقات</b>\n')]

    if not contests:
        lines.append(texts.t('ADMIN_CONTESTS_EMPTY', 'هیچ مسابقه‌ای ایجاد نشده است.'))
    else:
        for contest in contests:
            lines.append(f'• <b>{html.escape(contest.title)}</b> (#{contest.id})')
            contest_tz = _ensure_timezone(contest.timezone or settings.TIMEZONE)
            lines.append(_format_contest_summary(contest, texts, contest_tz))
            lines.append('')

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []
    for contest in contests:
        title = contest.title if len(contest.title) <= 25 else contest.title[:22] + '...'
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'🔎 {title}',
                    callback_data=f'admin_contest_view_{contest.id}',
                )
            ]
        )

    pagination = get_admin_pagination_keyboard(
        page,
        total_pages,
        'admin_contests_list',
        back_callback='admin_contests',
        language=db_user.language,
    )
    keyboard_rows.extend(pagination.inline_keyboard)

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_contest_details(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    texts = get_texts(db_user.language)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND', 'مسابقه یافت نشد.'), show_alert=True)
        return

    tz = _ensure_timezone(contest.timezone or settings.TIMEZONE)
    leaderboard = await get_contest_leaderboard_with_virtual(db, contest.id, limit=5)
    virtual_list = await list_virtual_participants(db, contest.id)
    virtual_count = sum(vp.referral_count for vp in virtual_list)
    total_events = await get_contest_events_count(db, contest.id) + virtual_count

    lines = [
        f'🏆 <b>{html.escape(contest.title)}</b>',
        _format_contest_summary(contest, texts, tz),
        texts.t('ADMIN_CONTEST_TOTAL_EVENTS', 'امتیازات: <b>{count}</b>').format(count=total_events),
    ]

    if contest.description:
        lines.append('')
        lines.append(html.escape(contest.description))

    if leaderboard:
        lines.append('')
        lines.append(texts.t('ADMIN_CONTEST_LEADERBOARD_TITLE', '📊 برترین شرکت‌کنندگان:'))
        for idx, (name, score, _, is_virtual) in enumerate(leaderboard, start=1):
            virt_mark = ' 👻' if is_virtual else ''
            lines.append(f'{idx}. {html.escape(name)}{virt_mark} — {score}')

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest.id,
            is_active=contest.is_active,
            can_delete=(
                not contest.is_active
                and (contest.end_at.replace(tzinfo=UTC) if contest.end_at.tzinfo is None else contest.end_at)
                < datetime.now(UTC)
            ),
            language=db_user.language,
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer('مسابقه یافت نشد', show_alert=True)
        return

    await toggle_referral_contest(db, contest, not contest.is_active)
    await show_contest_details(callback, db_user, db)


@admin_required
@error_handler
async def prompt_edit_summary_times(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND', 'مسابقه یافت نشد.'), show_alert=True)
        return
    await state.set_state(AdminStates.editing_referral_contest_summary_times)
    await state.update_data(contest_id=contest_id)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'admin_contest_view_{contest_id}',
                )
            ]
        ]
    )
    await callback.message.edit_text(
        texts.t(
            'ADMIN_CONTEST_ENTER_DAILY_TIME',
            'چه زمانی خلاصه‌های روزانه ارسال شود؟ فرمت HH:MM یا چند مورد با کاما (12:00,18:00).',
        ),
        reply_markup=kb,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_summary_times(
    message: types.Message,
    state: FSMContext,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    contest_id = data.get('contest_id')
    if not contest_id:
        await message.answer(texts.ERROR)
        await state.clear()
        return

    times = _parse_times(message.text or '')
    summary_time = times[0] if times else _parse_time(message.text or '')
    if not summary_time:
        await message.answer(
            texts.t('ADMIN_CONTEST_INVALID_TIME', 'زمان قابل تشخیص نیست. فرمت: 12:00 یا 12:00,18:00')
        )
        await state.clear()
        return

    contest = await get_referral_contest(db, int(contest_id))
    if not contest:
        await message.answer(texts.t('ADMIN_CONTEST_NOT_FOUND', 'مسابقه یافت نشد.'))
        await state.clear()
        return

    await update_referral_contest(
        db,
        contest,
        daily_summary_time=summary_time,
        daily_summary_times=','.join(t.strftime('%H:%M') for t in times) if times else None,
    )

    await message.answer(texts.t('ADMIN_UPDATED', 'به‌روز شد'))
    await state.clear()


@admin_required
@error_handler
async def delete_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND', 'مسابقه یافت نشد.'), show_alert=True)
        return

    now_utc = datetime.now(UTC)
    if contest.is_active or contest.end_at > now_utc:
        await callback.answer(
            texts.t('ADMIN_CONTEST_DELETE_RESTRICT', 'فقط مسابقات پایان‌یافته قابل حذف هستند.'),
            show_alert=True,
        )
        return

    await delete_referral_contest(db, contest)
    await callback.answer(texts.t('ADMIN_CONTEST_DELETED', 'مسابقه حذف شد.'), show_alert=True)
    await list_contests(callback, db_user, db)


@admin_required
@error_handler
async def show_leaderboard(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    texts = get_texts(db_user.language)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND', 'مسابقه یافت نشد.'), show_alert=True)
        return

    leaderboard = await get_contest_leaderboard_with_virtual(db, contest_id, limit=10)
    if not leaderboard:
        await callback.answer(texts.t('ADMIN_CONTEST_EMPTY_LEADERBOARD', 'هیچ شرکت‌کننده‌ای وجود ندارد.'), show_alert=True)
        return

    lines = [
        texts.t('ADMIN_CONTEST_LEADERBOARD_TITLE', '📊 برترین شرکت‌کنندگان:'),
    ]
    for idx, (name, score, _, is_virtual) in enumerate(leaderboard, start=1):
        virt_mark = ' 👻' if is_virtual else ''
        lines.append(f'{idx}. {html.escape(name)}{virt_mark} — {score}')

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_contest_creation(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    if not settings.is_contests_enabled():
        await callback.answer(
            texts.t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال است.'),
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(AdminStates.creating_referral_contest_mode)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_CONTEST_MODE_PROMPT',
            'شرط امتیاز را انتخاب کنید: رفرال باید اشتراک خریداری کند یا فقط ثبت‌نام کافی است.',
        ),
        reply_markup=get_contest_mode_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_contest_mode(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    mode = 'referral_paid' if callback.data == 'admin_contest_mode_paid' else 'referral_registered'
    await state.update_data(contest_type=mode)
    await state.set_state(AdminStates.creating_referral_contest_title)
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_ENTER_TITLE', 'نام مسابقه را وارد کنید:'),
        reply_markup=None,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_title(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    title = message.text.strip()
    texts = get_texts(db_user.language)

    await state.update_data(title=title)
    await state.set_state(AdminStates.creating_referral_contest_description)
    await message.answer(
        texts.t('ADMIN_CONTEST_ENTER_DESCRIPTION', "مسابقه را توصیف کنید (یا '-' برای رد کردن ارسال کنید):")
    )


@admin_required
@error_handler
async def process_description(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    description = message.text.strip()
    if description in {'-', 'skip', 'رد'}:
        description = None

    await state.update_data(description=description)
    await state.set_state(AdminStates.creating_referral_contest_prize)
    texts = get_texts(db_user.language)
    await message.answer(
        texts.t('ADMIN_CONTEST_ENTER_PRIZE', "جوایز/مزایای مسابقه را مشخص کنید (یا '-' برای رد کردن):")
    )


@admin_required
@error_handler
async def process_prize(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    prize = message.text.strip()
    if prize in {'-', 'skip', 'رد'}:
        prize = None

    await state.update_data(prize=prize)
    await state.set_state(AdminStates.creating_referral_contest_start)
    texts = get_texts(db_user.language)
    await message.answer(
        texts.t(
            'ADMIN_CONTEST_ENTER_START',
            'تاریخ و زمان شروع را وارد کنید (DD.MM.YYYY HH:MM) بر اساس منطقه زمانی شما:',
        )
    )


@admin_required
@error_handler
async def process_start_date(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    tz = _ensure_timezone(settings.TIMEZONE)
    start_dt = _parse_local_datetime(message.text, tz)
    texts = get_texts(db_user.language)

    if not start_dt:
        await message.answer(
            texts.t('ADMIN_CONTEST_INVALID_DATE', 'تاریخ قابل تشخیص نیست. فرمت: 01.06.2024 12:00')
        )
        return

    await state.update_data(start_at=start_dt.isoformat())
    await state.set_state(AdminStates.creating_referral_contest_end)
    await message.answer(
        texts.t(
            'ADMIN_CONTEST_ENTER_END',
            'تاریخ و زمان پایان را وارد کنید (DD.MM.YYYY HH:MM) بر اساس منطقه زمانی شما:',
        )
    )


@admin_required
@error_handler
async def process_end_date(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    tz = _ensure_timezone(settings.TIMEZONE)
    end_dt = _parse_local_datetime(message.text, tz)
    texts = get_texts(db_user.language)

    if not end_dt:
        await message.answer(
            texts.t('ADMIN_CONTEST_INVALID_DATE', 'تاریخ قابل تشخیص نیست. فرمت: 01.06.2024 12:00')
        )
        return

    data = await state.get_data()
    start_raw = data.get('start_at')
    start_dt = datetime.fromisoformat(start_raw) if start_raw else None
    if start_dt and end_dt <= start_dt:
        await message.answer(
            texts.t(
                'ADMIN_CONTEST_END_BEFORE_START',
                'تاریخ پایان باید بعد از تاریخ شروع باشد.',
            )
        )
        return

    await state.update_data(end_at=end_dt.isoformat())
    await state.set_state(AdminStates.creating_referral_contest_time)
    await message.answer(
        texts.t(
            'ADMIN_CONTEST_ENTER_DAILY_TIME',
            'چه زمانی خلاصه‌های روزانه ارسال شود؟ زمان را به فرمت HH:MM وارد کنید (مثلاً 12:00).',
        )
    )


@admin_required
@error_handler
async def finalize_contest_creation(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    times = _parse_times(message.text or '')
    summary_time = times[0] if times else _parse_time(message.text)
    texts = get_texts(db_user.language)

    if not summary_time:
        await message.answer(
            texts.t('ADMIN_CONTEST_INVALID_TIME', 'زمان قابل تشخیص نیست. فرمت: 12:00 یا 12:00,18:00')
        )
        return

    data = await state.get_data()
    tz = _ensure_timezone(settings.TIMEZONE)

    start_at_raw = data.get('start_at')
    end_at_raw = data.get('end_at')
    if not start_at_raw or not end_at_raw:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_DATE', 'تاریخ قابل تشخیص نیست.'))
        return

    start_at = datetime.fromisoformat(start_at_raw).astimezone(UTC)
    end_at = datetime.fromisoformat(end_at_raw).astimezone(UTC)

    contest_type = data.get('contest_type') or 'referral_paid'

    contest = await create_referral_contest(
        db,
        title=data.get('title'),
        description=data.get('description'),
        prize_text=data.get('prize'),
        contest_type=contest_type,
        start_at=start_at,
        end_at=end_at,
        daily_summary_time=summary_time,
        daily_summary_times=','.join(t.strftime('%H:%M') for t in times) if times else None,
        timezone_name=tz.key,
        created_by=db_user.id,
    )

    await state.clear()

    await message.answer(
        texts.t('ADMIN_CONTEST_CREATED', 'مسابقه ایجاد شد!'),
        reply_markup=get_referral_contest_manage_keyboard(
            contest.id,
            is_active=contest.is_active,
            language=db_user.language,
        ),
    )


@admin_required
@error_handler
async def show_detailed_stats(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer('مسابقه یافت نشد.', show_alert=True)
        return

    from app.services.referral_contest_service import referral_contest_service

    stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
    virtual = await list_virtual_participants(db, contest_id)
    virtual_count = len(virtual)
    virtual_referrals = sum(vp.referral_count for vp in virtual)

    # General message with main statistics
    general_lines = [
        '📈 <b>آمار مسابقه</b>',
        f'🏆 {html.escape(contest.title)}',
        '',
        f'👥 شرکت‌کنندگان (معرف‌ها): <b>{stats["total_participants"]}</b>',
        f'📨 رفرال‌های دعوت‌شده: <b>{stats["total_invited"]}</b>',
        '',
        f'💳 رفرال‌های پرداخت‌کننده: <b>{stats.get("paid_count", 0)}</b>',
        f'❌ رفرال‌های بدون پرداخت: <b>{stats.get("unpaid_count", 0)}</b>',
        '',
        '<b>💰 مبالغ:</b>',
        f'   🛒 خریدهای اشتراک: <b>{stats.get("subscription_total", 0) // 100} ر.</b>',
        f'   📥 شارژ موجودی: <b>{stats.get("deposit_total", 0) // 100} ر.</b>',
    ]

    if virtual_count > 0:
        general_lines.append('')
        general_lines.append(f'👻 مجازی‌ها: <b>{virtual_count}</b> (رفرال‌ها: {virtual_referrals})')

    await callback.message.edit_text(
        '\n'.join(general_lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )

    await callback.answer()


@admin_required
@error_handler
async def show_detailed_stats_page(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    contest_id: int = None,
    page: int = 1,
    stats: dict = None,
):
    if contest_id is None or stats is None:
        # Parse from callback.data: admin_contest_detailed_stats_page_{contest_id}_page_{page}
        parts = callback.data.split('_')
        contest_id = int(parts[5])  # contest_id after page
        page = int(parts[7])  # page after the second page

        # Get stats if not provided
        from app.services.referral_contest_service import referral_contest_service

        stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)

    participants = stats['participants']
    total_participants = len(participants)
    PAGE_SIZE = 10
    total_pages = math.ceil(total_participants / PAGE_SIZE)

    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    page_participants = participants[offset : offset + PAGE_SIZE]

    lines = [f'📊 بر اساس شرکت‌کنندگان (صفحه {page}/{total_pages}):']
    for p in page_participants:
        lines.extend(
            [
                f'• <b>{html.escape(p["full_name"] or "")}</b>',
                f'  📨 دعوت‌شده: {p["total_referrals"]}',
                f'  💰 پرداخت‌کرده: {p["paid_referrals"]}',
                f'  ❌ بدون پرداخت: {p["unpaid_referrals"]}',
                f'  💵 مبلغ: {p["total_paid_amount"] // 100} ر.',
                '',  # Empty line for separation
            ]
        )

    pagination = get_admin_pagination_keyboard(
        page,
        total_pages,
        f'admin_contest_detailed_stats_page_{contest_id}',
        back_callback=f'admin_contest_view_{contest_id}',
        language=db_user.language,
    )

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=pagination,
    )

    await callback.answer()


@admin_required
@error_handler
async def sync_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    """Synchronize contest events with real payments."""
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer('مسابقه یافت نشد.', show_alert=True)
        return

    await callback.answer('🔄 همگام‌سازی شروع شد...', show_alert=False)

    from app.services.referral_contest_service import referral_contest_service

    # STEP 1: Clean up invalid events (referrals registered outside the contest period)
    cleanup_stats = await referral_contest_service.cleanup_contest(db, contest_id)

    if 'error' in cleanup_stats:
        await callback.message.answer(
            f'❌ Cleanup error:\n{cleanup_stats["error"]}',
        )
        return

    # STEP 2: Synchronize amounts for remaining valid events
    stats = await referral_contest_service.sync_contest(db, contest_id)

    if 'error' in stats:
        await callback.message.answer(
            f'❌ Sync error:\n{stats["error"]}',
        )
        return

    # Build result message
    # Show exact dates used for filtering
    start_str = stats.get('contest_start', contest.start_at.isoformat())
    end_str = stats.get('contest_end', contest.end_at.isoformat())

    lines = [
        '✅ <b>همگام‌سازی کامل شد!</b>',
        '',
        f'📊 <b>مسابقه:</b> {html.escape(contest.title)}',
        f'📅 <b>دوره:</b> {contest.start_at.strftime("%d.%m.%Y")} - {contest.end_at.strftime("%d.%m.%Y")}',
        '🔍 <b>فیلتر تراکنش‌ها:</b>',
        f'   <code>{start_str}</code>',
        f'   <code>{end_str}</code>',
        '',
        '�� <b>پاکسازی:</b>',
        f'   🗑 رویدادهای نامعتبر حذف‌شده: <b>{cleanup_stats.get("deleted", 0)}</b>',
        f'   ✅ رویدادهای معتبر باقی‌مانده: <b>{cleanup_stats.get("remaining", 0)}</b>',
        f'   📊 رویدادهای قبل از پاکسازی: <b>{cleanup_stats.get("total_before", 0)}</b>',
        '',
        '📊 <b>همگام‌سازی:</b>',
        f'   📝 رفرال‌ها در دوره: <b>{stats.get("total_events", 0)}</b>',
        f'   ⚠️ فیلترشده (خارج از دوره): <b>{stats.get("filtered_out_events", 0)}</b>',
        f'   🔄 مبالغ به‌روزشده: <b>{stats.get("updated", 0)}</b>',
        f'   ⏭ بدون تغییر: <b>{stats.get("skipped", 0)}</b>',
        '',
        f'💳 رفرال‌های پرداخت‌کننده: <b>{stats.get("paid_count", 0)}</b>',
        f'❌ رفرال‌های بدون پرداخت: <b>{stats.get("unpaid_count", 0)}</b>',
        '',
        '<b>💰 مبالغ:</b>',
        f'   🛒 خریدهای اشتراک: <b>{stats.get("subscription_total", 0) // 100} ر.</b>',
        f'   📥 شارژ موجودی: <b>{stats.get("deposit_total", 0) // 100} ر.</b>',
    ]

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='⬅️ بازگشت به مسابقه', callback_data=f'admin_contest_view_{contest_id}')]
        ]
    )

    await callback.message.answer(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=back_keyboard,
    )

    # Update the main message with new statistics
    detailed_stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
    general_lines = [
        f'🏆 <b>{html.escape(contest.title)}</b>',
        f'📅 دوره: {contest.start_at.strftime("%d.%m.%Y")} - {contest.end_at.strftime("%d.%m.%Y")}',
        '',
        f'👥 شرکت‌کنندگان (معرف‌ها): <b>{detailed_stats["total_participants"]}</b>',
        f'📨 رفرال‌های دعوت‌شده: <b>{detailed_stats["total_invited"]}</b>',
        '',
        f'💳 رفرال‌های پرداخت‌کننده: <b>{detailed_stats.get("paid_count", 0)}</b>',
        f'❌ رفرال‌های بدون پرداخت: <b>{detailed_stats.get("unpaid_count", 0)}</b>',
        f'🛒 خریدهای اشتراک: <b>{detailed_stats["total_paid_amount"] // 100} ر.</b>',
    ]

    await callback.message.edit_text(
        '\n'.join(general_lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )


@admin_required
@error_handler
async def debug_contest_transactions(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    """Show referral transactions of the contest for debugging."""
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED', 'مسابقات غیرفعال شده‌اند.'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer('مسابقه یافت نشد.', show_alert=True)
        return

    await callback.answer('🔍 در حال بارگذاری...', show_alert=False)

    from app.database.crud.referral_contest import debug_contest_transactions as debug_txs

    debug_data = await debug_txs(db, contest_id, limit=10)

    if 'error' in debug_data:
        await callback.message.answer(f'❌ خطا: {debug_data["error"]}')
        return

    deposit_total = debug_data.get('deposit_total_kopeks', 0) // 100
    subscription_total = debug_data.get('subscription_total_kopeks', 0) // 100

    lines = [
        '🔍 <b>اشکال‌زدایی تراکنش‌های مسابقه</b>',
        '',
        f'📊 <b>مسابقه:</b> {html.escape(contest.title)}',
        '📅 <b>دوره فیلتر:</b>',
        f'   شروع: <code>{debug_data.get("contest_start")}</code>',
        f'   پایان: <code>{debug_data.get("contest_end")}</code>',
        f'👥 <b>رفرال‌ها در دوره:</b> {debug_data.get("referral_count", 0)}',
        f'⚠️ <b>فیلترشده (خارج از دوره):</b> {debug_data.get("filtered_out", 0)}',
        f'📊 <b>کل رویدادها در پایگاه داده:</b> {debug_data.get("total_all_events", 0)}',
        '',
        '<b>💰 مبالغ:</b>',
        f'   📥 شارژ موجودی: <b>{deposit_total}</b> ر.',
        f'   🛒 خریدهای اشتراک: <b>{subscription_total}</b> ر.',
        '',
    ]

    # Show transactions IN period
    txs_in = debug_data.get('transactions_in_period', [])
    if txs_in:
        lines.append(f'✅ <b>تراکنش‌ها در دوره</b> (اولین {len(txs_in)}):')
        for tx in txs_in[:5]:  # Show maximum 5
            lines.append(
                f'  • {tx["created_at"][:10]} | {tx["type"]} | {tx["amount_kopeks"] // 100}₽ | user={tx["user_id"]}'
            )
        if len(txs_in) > 5:
            lines.append(f'  ... و {len(txs_in) - 5} مورد دیگر')
    else:
        lines.append('✅ <b>تراکنش‌ها در دوره:</b> 0')

    lines.append('')

    # Show transactions OUTSIDE period
    txs_out = debug_data.get('transactions_outside_period', [])
    if txs_out:
        lines.append(f'❌ <b>تراکنش‌ها خارج از دوره</b> (اولین {len(txs_out)}):')
        for tx in txs_out[:5]:
            lines.append(
                f'  • {tx["created_at"][:10]} | {tx["type"]} | {tx["amount_kopeks"] // 100}₽ | user={tx["user_id"]}'
            )
        if len(txs_out) > 5:
            lines.append(f'  ... و {len(txs_out) - 5} مورد دیگر')
    else:
        lines.append('❌ <b>تراکنش‌ها خارج از دوره:</b> 0')

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='⬅️ بازگشت به مسابقه', callback_data=f'admin_contest_view_{contest_id}')]
        ]
    )

    await callback.message.answer(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=back_keyboard,
    )


# ── Virtual Participants ──────────────────────────────────────────────


@admin_required
@error_handler
async def show_virtual_participants(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer('مسابقه یافت نشد.', show_alert=True)
        return

    vps = await list_virtual_participants(db, contest_id)

    lines = [f'👻 <b>شرکت‌کنندگان مجازی</b> — {html.escape(contest.title)}', '']
    if vps:
        for vp in vps:
            lines.append(f'• {html.escape(vp.display_name)} — {vp.referral_count} رف.')
    else:
        lines.append('هیچ شرکت‌کننده مجازی وجود ندارد.')

    rows = [
        [
            types.InlineKeyboardButton(
                text='➕ افزودن',
                callback_data=f'admin_contest_vp_add_{contest_id}',
            ),
            types.InlineKeyboardButton(
                text='🎭 انبوه',
                callback_data=f'admin_contest_vp_mass_{contest_id}',
            ),
        ],
    ]
    if vps:
        for vp in vps:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        text=f'✏️ {vp.display_name}',
                        callback_data=f'admin_contest_vp_edit_{vp.id}',
                    ),
                    types.InlineKeyboardButton(
                        text='🗑',
                        callback_data=f'admin_contest_vp_del_{vp.id}',
                    ),
                ]
            )
    rows.append(
        [
            types.InlineKeyboardButton(
                text='⬅️ بازگشت',
                callback_data=f'admin_contest_view_{contest_id}',
            ),
        ]
    )

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_add_virtual_participant(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    contest_id = int(callback.data.split('_')[-1])
    await state.set_state(AdminStates.adding_virtual_participant_name)
    await state.update_data(vp_contest_id=contest_id)
    await callback.message.edit_text(
        '👻 نام نمایشی شرکت‌کننده مجازی را وارد کنید:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_virtual_participant_name(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    name = message.text.strip()
    if not name or len(name) > 200:
        await message.answer('نام باید بین 1 تا 200 کاراکتر باشد. دوباره امتحان کنید:')
        return
    await state.update_data(vp_name=name)
    await state.set_state(AdminStates.adding_virtual_participant_count)
    await message.answer(f'نام: <b>{name}</b>\n\nتعداد رفرال‌ها را وارد کنید (عدد):')


@admin_required
@error_handler
async def process_virtual_participant_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer('یک عدد صحیح مثبت وارد کنید:')
        return

    data = await state.get_data()
    contest_id = data['vp_contest_id']
    display_name = data['vp_name']
    await state.clear()

    vp = await add_virtual_participant(db, contest_id, display_name, count)
    await message.answer(
        f'✅ شرکت‌کننده مجازی اضافه شد:\nنام: <b>{vp.display_name}</b>\nرفرال‌ها: <b>{vp.referral_count}</b>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='👻 به لیست', callback_data=f'admin_contest_vp_{contest_id}')],
                [types.InlineKeyboardButton(text='⬅️ به مسابقه', callback_data=f'admin_contest_view_{contest_id}')],
            ]
        ),
    )


@admin_required
@error_handler
async def delete_virtual_participant_handler(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    vp_id = int(callback.data.split('_')[-1])

    # Get contest_id before deletion
    from sqlalchemy import select as sa_select

    from app.database.models import ReferralContestVirtualParticipant

    result = await db.execute(
        sa_select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == vp_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        await callback.answer('شرکت‌کننده یافت نشد.', show_alert=True)
        return

    contest_id = vp.contest_id
    deleted = await delete_virtual_participant(db, vp_id)
    if deleted:
        await callback.answer('✅ حذف شد', show_alert=False)
    else:
        await callback.answer('حذف ناموفق بود.', show_alert=True)

    # Return to list
    vps = await list_virtual_participants(db, contest_id)
    contest = await get_referral_contest(db, contest_id)

    lines = [f'👻 <b>شرکت‌کنندگان مجازی</b> — {html.escape(contest.title)}', '']
    if vps:
        for v in vps:
            lines.append(f'• {html.escape(v.display_name)} — {v.referral_count} رف.')
    else:
        lines.append('هیچ شرکت‌کننده مجازی وجود ندارد.')

    rows = [
        [
            types.InlineKeyboardButton(text='➕ افزودن', callback_data=f'admin_contest_vp_add_{contest_id}'),
            types.InlineKeyboardButton(text='🎭 انبوه', callback_data=f'admin_contest_vp_mass_{contest_id}'),
        ],
    ]
    if vps:
        for v in vps:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        text=f'✏️ {v.display_name}', callback_data=f'admin_contest_vp_edit_{v.id}'
                    ),
                    types.InlineKeyboardButton(text='🗑', callback_data=f'admin_contest_vp_del_{v.id}'),
                ]
            )
    rows.append([types.InlineKeyboardButton(text='⬅️ بازگشت', callback_data=f'admin_contest_view_{contest_id}')])

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_required
@error_handler
async def start_mass_virtual_participants(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    """Starts bulk creation of virtual participants (extras)."""
    contest_id = int(callback.data.split('_')[-1])
    await state.set_state(AdminStates.adding_mass_virtual_count)
    await state.update_data(mass_vp_contest_id=contest_id)

    text = """
🎭 <b>انبوه — ایجاد دسته‌جمعی شرکت‌کنندگان مجازی</b>

<i>این برای چیست؟</i>
شرکت‌کنندگان مجازی (شبح‌ها) به ایجاد تصویری از فعالیت در مسابقه کمک می‌کنند. آن‌ها در جدول رتبه‌بندی کنار شرکت‌کنندگان واقعی نمایش داده می‌شوند، اما با نماد 👻 مشخص می‌شوند.

این کمک می‌کند تا:
• شرکت‌کنندگان واقعی انگیزه رقابت داشته باشند
• سطح مشخصی برای شرکت تعیین شود
• مسابقه پویاتر به نظر برسد

<b>تعداد شبح‌هایی که باید ایجاد شوند را وارد کنید:</b>
<i>(از 1 تا 50)</i>
"""

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_mass_virtual_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    """Processes the number of ghosts for bulk creation."""
    try:
        count = int(message.text.strip())
        if count < 1 or count > 50:
            await message.answer(
                '❌ یک عدد از 1 تا 50 وارد کنید:',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_contests_referral')],
                    ]
                ),
            )
            return
    except ValueError:
        await message.answer(
            '❌ یک عدد معتبر از 1 تا 50 وارد کنید:',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ لغو', callback_data='admin_contests_referral')],
                ]
            ),
        )
        return

    await state.update_data(mass_vp_count=count)
    await state.set_state(AdminStates.adding_mass_virtual_referrals)

    data = await state.get_data()
    contest_id = data.get('mass_vp_contest_id')

    await message.answer(
        f'✅ <b>{count}</b> شبح ایجاد خواهد شد.\n\n'
        f'<b>تعداد رفرال‌های هر کدام را وارد کنید:</b>\n'
        f'<i>(از 1 تا 100)</i>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )


@admin_required
@error_handler
async def process_mass_virtual_referrals(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    """Creates bulk ghosts with random names."""
    import random
    import string

    try:
        referrals_count = int(message.text.strip())
        if referrals_count < 1 or referrals_count > 100:
            await message.answer('❌ یک عدد از 1 تا 100 وارد کنید:')
            return
    except ValueError:
        await message.answer('❌ یک عدد معتبر از 1 تا 100 وارد کنید:')
        return

    data = await state.get_data()
    contest_id = data.get('mass_vp_contest_id')
    ghost_count = data.get('mass_vp_count', 1)

    await state.clear()

    # Generate and create ghosts
    created = []
    for _ in range(ghost_count):
        # Random name up to 5 characters (letters + digits)
        name_length = random.randint(3, 5)
        name = ''.join(random.choices(string.ascii_letters + string.digits, k=name_length))

        vp = await add_virtual_participant(db, contest_id, name, referrals_count)
        created.append(vp)

    # Show result
    text = f"""
✅ <b>ایجاد انبوه انجام شد!</b>

📊 <b>نتیجه:</b>
• شبح‌های ایجادشده: {len(created)}
• رفرال‌های هر کدام: {referrals_count}
• مجموع رفرال‌های مجازی: {len(created) * referrals_count}

👻 <b>شبح‌های ایجادشده:</b>
"""
    for vp in created[:10]:
        text += f'• {vp.display_name} — {vp.referral_count} رف.\n'

    if len(created) > 10:
        text += f'<i>... و {len(created) - 10} مورد دیگر</i>\n'

    await message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='👻 به لیست شبح‌ها', callback_data=f'admin_contest_vp_{contest_id}'
                    )
                ],
                [types.InlineKeyboardButton(text='⬅️ به مسابقه', callback_data=f'admin_contest_view_{contest_id}')],
            ]
        ),
    )


@admin_required
@error_handler
async def start_edit_virtual_participant(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    vp_id = int(callback.data.split('_')[-1])

    from sqlalchemy import select as sa_select

    from app.database.models import ReferralContestVirtualParticipant

    result = await db.execute(
        sa_select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == vp_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        await callback.answer('شرکت‌کننده یافت نشد.', show_alert=True)
        return

    await state.set_state(AdminStates.editing_virtual_participant_count)
    await state.update_data(vp_edit_id=vp_id, vp_edit_contest_id=vp.contest_id)
    await callback.message.edit_text(
        f'✏️ <b>{vp.display_name}</b>\n'
        f'تعداد فعلی رفرال‌ها: <b>{vp.referral_count}</b>\n\n'
        f'تعداد جدید را وارد کنید:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ لغو', callback_data=f'admin_contest_vp_{vp.contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_virtual_participant_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer('یک عدد صحیح مثبت وارد کنید:')
        return

    data = await state.get_data()
    vp_id = data['vp_edit_id']
    contest_id = data['vp_edit_contest_id']
    await state.clear()

    vp = await update_virtual_participant_count(db, vp_id, count)
    if vp:
        await message.answer(
            f'✅ به‌روزرسانی شد: <b>{vp.display_name}</b> — {vp.referral_count} رف.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='👻 به لیست', callback_data=f'admin_contest_vp_{contest_id}')],
                ]
            ),
        )
    else:
        await message.answer('شرکت‌کننده یافت نشد.')


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_contests_menu, F.data == 'admin_contests')
    dp.callback_query.register(show_referral_contests_menu, F.data == 'admin_contests_referral')
    dp.callback_query.register(list_contests, F.data == 'admin_contests_list')
    dp.callback_query.register(list_contests, F.data.startswith('admin_contests_list_page_'))
    dp.callback_query.register(show_contest_details, F.data.startswith('admin_contest_view_'))
    dp.callback_query.register(toggle_contest, F.data.startswith('admin_contest_toggle_'))
    dp.callback_query.register(prompt_edit_summary_times, F.data.startswith('admin_contest_edit_times_'))
    dp.callback_query.register(delete_contest, F.data.startswith('admin_contest_delete_'))
    dp.callback_query.register(show_leaderboard, F.data.startswith('admin_contest_leaderboard_'))
    dp.callback_query.register(show_detailed_stats_page, F.data.startswith('admin_contest_detailed_stats_page_'))
    dp.callback_query.register(show_detailed_stats, F.data.startswith('admin_contest_detailed_stats_'))
    dp.callback_query.register(sync_contest, F.data.startswith('admin_contest_sync_'))
    dp.callback_query.register(debug_contest_transactions, F.data.startswith('admin_contest_debug_'))
    dp.callback_query.register(start_contest_creation, F.data == 'admin_contests_create')
    dp.callback_query.register(
        select_contest_mode,
        F.data.in_(['admin_contest_mode_paid', 'admin_contest_mode_registered']),
        AdminStates.creating_referral_contest_mode,
    )

    dp.message.register(process_title, AdminStates.creating_referral_contest_title)
    dp.message.register(process_description, AdminStates.creating_referral_contest_description)
    dp.message.register(process_prize, AdminStates.creating_referral_contest_prize)
    dp.message.register(process_start_date, AdminStates.creating_referral_contest_start)
    dp.message.register(process_end_date, AdminStates.creating_referral_contest_end)
    dp.message.register(finalize_contest_creation, AdminStates.creating_referral_contest_time)
    dp.message.register(process_edit_summary_times, AdminStates.editing_referral_contest_summary_times)

    dp.callback_query.register(start_add_virtual_participant, F.data.startswith('admin_contest_vp_add_'))
    dp.callback_query.register(delete_virtual_participant_handler, F.data.startswith('admin_contest_vp_del_'))
    dp.callback_query.register(start_edit_virtual_participant, F.data.startswith('admin_contest_vp_edit_'))
    dp.callback_query.register(start_mass_virtual_participants, F.data.startswith('admin_contest_vp_mass_'))
    dp.callback_query.register(show_virtual_participants, F.data.regexp(r'^admin_contest_vp_\d+$'))
    dp.message.register(process_virtual_participant_name, AdminStates.adding_virtual_participant_name)
    dp.message.register(process_virtual_participant_count, AdminStates.adding_virtual_participant_count)
    dp.message.register(process_edit_virtual_participant_count, AdminStates.editing_virtual_participant_count)
    dp.message.register(process_mass_virtual_count, AdminStates.adding_mass_virtual_count)
    dp.message.register(process_mass_virtual_referrals, AdminStates.adding_mass_virtual_referrals)
