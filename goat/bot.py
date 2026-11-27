from datetime import UTC, datetime, timedelta
from decimal import Decimal
import asyncio
import io
import json
import re
import secrets
from uuid import UUID, uuid4

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PreCheckoutQuery, WebAppInfo

from goat.accounts import audit, enqueue, grant, manage_access, notify_admins, profile, register
from goat.billing import DAY, PLAN, approve, checkout, history, order, payment, record_refund, redeem, statistics
from goat.db import one, query, rows, scalar
from goat.finance import Calculation
from goat.jobs import EMOJIS, EmojiManager, buttons
from goat.terminal import Preferences, save_prefs

CATEGORIES = ("Оплата", "Подписка", "Ошибка приложения", "Сигналы", "Предложение", "Другое")
ADMIN = {
    "Статистика": "/stats [1|7|30|0] [csv]",
    "Пользователи": "/user ID или @username",
    "Продажи": "/stats 30",
    "Заявки": "/orders",
    "Подписки": "/access ID freeze|unfreeze|revoke|block|unblock",
    "Цена": "/price ЧИСЛО",
    "Выдать доступ": "/grant ID или @username 24h|3d|7d|14d|30d|90d|forever|СРОКd [причина]",
    "Поддержка": "/tickets",
    "Рассылка": "/broadcast all|active|paid|free|none|expired|new МИНУТЫ ТЕКСТ [| кнопка | https://…]",
    "Промокоды": "/promocreate КОД percent|fixed|days ЗНАЧЕНИЕ ДНИ_ДЕЙСТВИЯ ЛИМИТ НА_ПОЛЬЗОВАТЕЛЯ 0|1",
    "Настройки": "/plan automatic|manual|on|off\n/description ТЕКСТ\n/discount ЦЕНА ЧАСЫ\n/fees Portals|MRKT JSON\n/rate GRAM TON КУРС ИСТОЧНИК\n/legal JSON\n/report ЧАС_UTC|off",
    "Сервер": "/server",
    "Эмодзи": "/emojiset КАТЕГОРИЯ [кастомный эмодзи]\n/emoji [кастомный эмодзи]",
}
STATS = dict(zip(
    "users new_today new_week new_month active_today active_week blocked subscribed paid_subscribers free_subscribers expired free_grants conversion sales gross average buyers first_purchases renewals refunds refund_count net pending".split(),
    ("Пользователи", "Новые сегодня", "Новые за неделю", "Новые за месяц", "Активные сегодня",
     "Активные за неделю", "Заблокированные", "Активные подписчики", "Платные", "Бесплатные",
     "Истёкшие", "Бесплатные выдачи", "Конверсия, %", "Покупки", "Валовая выручка, Stars",
     "Средний чек, Stars", "Уникальные покупатели", "Первые покупки", "Продления",
     "Возвращено, Stars", "Возвраты", "После возвратов, Stars", "Ожидают одобрения"), strict=True))


def stats_text(data):
    return "\n".join(f"{label} · {data[key] if data[key] is not None else '—'}" for key, label in STATS.items())


async def resolve(conn, value):
    if value.isdecimal():
        uid = await scalar(conn, "select id from users where id=:id and not erased and registered", id=int(value))
    else:
        uid = await scalar(conn, "select id from users where lower(username)=lower(:name) and not erased and registered",
                           name=value.removeprefix("@"))
    if uid is None:
        raise ValueError("Пользователь ещё не зарегистрирован")
    return uid


def user_menu(config):
    items = []
    if config.app_url:
        items.append(({"text": "🚀 Открыть терминал", "web_app": WebAppInfo(url=config.app_url)},))
    items.extend([
        ({"text": "💎 Купить подписку", "callback_data": "buy"},
         {"text": "👤 Мой профиль", "callback_data": "profile"}),
        ({"text": "📊 Возможности сервиса", "callback_data": "about"},
         {"text": "🎧 Поддержка", "callback_data": "support"}),
    ])
    if config.news_channel_url:
        items.append(({"text": "📢 Новости проекта", "url": config.news_channel_url},))
    return buttons(*items)


def emoji_ids(message):
    return [entity.custom_emoji_id for entity in (message.entities or message.caption_entities or [])
            if entity.custom_emoji_id]


def make_dispatcher(db, config):
    dp = Dispatcher()
    router = Router()
    router.message.filter(F.chat.type == "private")
    dp.include_router(router)

    async def touch(conn, user, source=None):
        return await register(conn, user.id, user.username, user.full_name, source=source, bot=True)

    async def create_order(message, user, code=None):
        async with db.transaction() as conn:
            await touch(conn, user)
            entry = await order(conn, user.id, config.admins, code)
            if entry["status"] == "awaiting_payment":
                sent = await one(conn, """
                    select status from notification_deliveries where user_id=:uid
                    and (dedupe=:a or dedupe=:b) order by id desc limit 1
                """, uid=user.id, a=f"invoice:{entry['id']}", b=f"decision:{entry['id']}")
                if sent and sent["status"] in {"failed", "sent", "skipped"} and entry["checkout_at"] is None:
                    await query(conn, """
                        update notification_deliveries set status='queued',attempts=0,run_at=now()
                        where user_id=:uid and dedupe in (:a,:b) and run_at<now()-interval '30 seconds'
                    """, uid=user.id, a=f"invoice:{entry['id']}", b=f"decision:{entry['id']}")
        await message.answer(f"⏳ Заявка ожидает одобрения · {entry['amount']} Stars."
                             if entry["status"] == "pending_approval" else
                             f"Счёт GOAT PRO · {entry['amount']} Stars. Проверьте чат с ботом.")

    async def show_profile(message, user):
        async with db.transaction() as conn:
            await touch(conn, user)
            data = await profile(conn, user.id)
            end = data["ends_at"]
            until = "Бессрочно" if data["lifetime"] else end.strftime("%d.%m.%Y %H:%M UTC") if end else "—"
            active = "Активна" if data["active"] else "Заморожена" if data["frozen_at"] else "Неактивна"
            started = data["starts_at"].strftime("%d.%m.%Y %H:%M UTC") if data["starts_at"] else "—"
            remaining = "∞" if data["lifetime"] else str(max(timedelta(), end-datetime.now(UTC))).split(".")[0] if end else "—"
            await EmojiManager.send(message.bot, conn, user.id, (
                f"{data['name']}\nTelegram ID {user.id}\nПодписка · {active}\nАктивация · {started}\n"
                f"Окончание · {until}\nОсталось · {remaining}\nПокупок · {data['purchases']}"),
                "premium", reply_markup=buttons(({"text": "Продлить", "callback_data": "buy"},),
                    ({"text": "Торговые уведомления", "callback_data": "toggle:signals"},
                     {"text": "Напоминания", "callback_data": "toggle:reminders"},
                     {"text": "Рассылки", "callback_data": "toggle:marketing"})))

    async def support_menu(message):
        await message.answer("Выберите категорию обращения. После выбора отправьте сообщение или медиа.",
                             reply_markup=buttons(*[({"text": category, "callback_data": f"category:{idx}"},)
                                                    for idx, category in enumerate(CATEGORIES)]))

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        async with db.transaction() as conn:
            source = command.args
            await touch(conn, message.from_user, None if source and source.startswith("gift_") else source)
            response = ""
            if source and source.startswith("gift_"):
                try:
                    await redeem(conn, message.from_user.id, source[5:])
                    response = "\n🎁 Доступ по приглашению активирован."
                except ValueError as exc:
                    response = f"\n{exc}"
            data = await profile(conn, message.from_user.id)
            await EmojiManager.send(message.bot, conn, message.from_user.id, (
                "GOAT Gifts AI\nТерминал коллекционных Telegram Gifts.\n"
                + ("GOAT PRO активен." if data["active"] else "Доступен демонстрационный терминал.") + response),
                "logo", reply_markup=user_menu(config))
        if source == "support":
            await support_menu(message)
        if source == "buy":
            try:
                await create_order(message, message.from_user)
            except ValueError as exc:
                await message.answer(str(exc))

    @router.message(Command("buy", "profile", "support", "paysupport", "promo", "terms", "privacy", "unsubscribe"))
    async def client_commands(message: Message, command: CommandObject):
        cmd, uid = command.command, message.from_user.id
        try:
            if cmd == "buy":
                await create_order(message, message.from_user, command.args)
            elif cmd == "profile":
                await show_profile(message, message.from_user)
            elif cmd in {"support", "paysupport"}:
                if cmd == "paysupport":
                    await message.answer("Для вопросов о Stars выберите «Оплата».")
                await support_menu(message)
            else:
                async with db.transaction() as conn:
                    await touch(conn, message.from_user)
                    if cmd == "promo":
                        await redeem(conn, uid, command.args or "")
                        text = "🎁 Бесплатный доступ активирован."
                    elif cmd == "unsubscribe":
                        prefs = Preferences.model_validate((await profile(conn, uid))["prefs"])
                        await save_prefs(conn, uid, prefs.model_copy(update={"marketing": False}))
                        text = "Маркетинговые уведомления отключены."
                    else:
                        data = await scalar(conn, "select value from system_settings where key='legal'")
                        field = "terms" if cmd == "terms" else "privacy"
                        text = data[field] if data and field in data else (
                            "Документы и сведения об операторе доступны в терминале и по запросу /support.")
                await message.answer(text)
        except (ValueError, ArithmeticError) as exc:
            await message.answer(str(exc))

    @router.pre_checkout_query()
    async def pre_checkout(check: PreCheckoutQuery):
        try:
            async with asyncio.timeout(7):
                async with db.transaction() as conn:
                    valid = await checkout(conn, UUID(check.invoice_payload), check.from_user.id,
                                           check.total_amount, check.currency)
        except (TimeoutError, ValueError):
            valid = False
        await check.answer(ok=valid, error_message=None if valid else
                           "Счёт недействителен или уже обрабатывается. Откройте /buy.")

    @router.message(F.successful_payment)
    async def success(message: Message):
        p = message.successful_payment
        async with db.transaction() as conn:
            try:
                await payment(conn, UUID(p.invoice_payload), message.from_user.id, p.total_amount, p.currency,
                              p.telegram_payment_charge_id, message.date, config.admins)
            except ValueError:
                await notify_admins(conn, config.admins, f"payment-review:{p.telegram_payment_charge_id}",
                    text=f"Платёж требует сверки: Telegram ID {message.from_user.id}, заказ {p.invoice_payload}.")

    @router.message(F.refunded_payment)
    async def refund_event(message: Message):
        p = message.refunded_payment
        async with db.transaction() as conn:
            uid = await scalar(conn, """
                select o.user_id from payments p join orders o on o.id=p.order_id where p.charge=:charge
            """, charge=p.telegram_payment_charge_id)
            if uid:
                await record_refund(conn, p.telegram_payment_charge_id, uid)

    async def confirm(message, actor, action, payload, summary):
        token = secrets.token_urlsafe(12)
        async with db.transaction() as conn:
            await query(conn, "insert into system_settings(key,value) values(:key,cast(:v as jsonb))",
                        key=f"confirm:{token}", v=json.dumps({"actor": actor, "action": action,
                        "payload": payload, "expires": datetime.now(UTC).timestamp() + 600}, ensure_ascii=False))
        await message.answer(summary[:3800], reply_markup=buttons(
            ({"text": "Подтвердить", "callback_data": f"confirm:{token}"},
             {"text": "Отмена", "callback_data": f"cancel:{token}"})))

    @router.message(Command("emoji"))
    async def get_emoji(message: Message):
        if message.from_user.id in config.admins:
            await message.answer("\n".join(emoji_ids(message)) or "Отправьте /emoji с кастомным эмодзи.")

    @router.message(Command(*("admin stats user price description plan discount grant invite access orders refund "
                               "tickets reply close_ticket dm promocreate promotoggle promolist broadcast bstats bcancel "
                               "server emojiset fees rate legal report".split())))
    async def owner(message: Message, command: CommandObject):
        uid = message.from_user.id
        if uid not in config.admins:
            return
        name, raw = command.command, command.args or ""
        args = raw.split()
        if name == "admin":
            labels = list(ADMIN)
            await message.answer("👑 GOAT Gifts AI · Администрирование", reply_markup=buttons(*[
                tuple({"text": label, "callback_data": f"owner:{index+j}"} for j, label in enumerate(labels[index:index+2]))
                for index in range(0, len(labels), 2)]))
            return
        try:
            payload = {}
            summary = f"Подтвердить /{name} {raw}"
            async with db.transaction() as conn:
                await touch(conn, message.from_user)
                if name == "stats":
                    days = int(args[0]) if args else 30
                    if days not in {0, 1, 7, 30}:
                        raise ValueError("Период: 1, 7, 30 или 0 для всей истории")
                    data = await statistics(conn, days)
                    if "csv" in args:
                        import csv
                        buffer = io.StringIO()
                        writer = csv.writer(buffer)
                        writer.writerow(["period_days", *data])
                        writer.writerow([days, *data.values()])
                        await message.answer_document(BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), "goat-statistics.csv"))
                    else:
                        await EmojiManager.send(message.bot, conn, uid, stats_text(data), "chart")
                    return
                if name == "user":
                    target = await resolve(conn, args[0])
                    p = await profile(conn, target)
                    purchases = await history(conn, target)
                    grants = await rows(conn, "select * from access_grants where user_id=:uid order by id desc limit 10", uid=target)
                    tickets = await rows(conn, "select * from support_tickets where user_id=:uid order by id desc limit 10", uid=target)
                    text = [f"{p['name']} · @{p['username']} · {target}",
                            f"Доступ {p['active']} · до {p['ends_at']} · блокировка {p['blocked']}", "Заказы"]
                    text += [f"{o['id']} · {o['amount']} ⭐ · {o['status']}" for o in purchases[:5]]
                    text += ["Доступы"] + [f"{g['kind']} · {g['starts_at']:%d.%m.%Y} → {g['ends_at']} · {g['reason'] or '—'}"
                                            for g in grants]
                    text += ["Поддержка"] + [f"#{t['id']} · {t['category']} · {t['status']}" for t in tickets]
                    await message.answer("\n".join(text)[:3900])
                    return
                if name in {"orders", "tickets", "server", "promolist", "bstats"}:
                    statements = {
                        "orders": "select id,user_id,amount,status,expires_at from orders where status='pending_approval' order by created_at desc limit 15",
                        "tickets": "select id,user_id,category,status,unread_admin from support_tickets where status<>'closed' order by unread_admin desc,id desc limit 15",
                        "server": "select component,status,checked_at from system_health",
                        "promolist": """select c.code,c.kind,c.value,c.enabled,c.expires_at,count(r.id) uses from promo_codes c
                                        left join promo_redemptions r using(code) group by c.code order by c.expires_at desc limit 15""",
                        "bstats": """select b.id,b.segment,b.status,count(d.id) total,
                                        count(*) filter(where d.status='sent') sent,count(*) filter(where d.status='failed') failed
                                        from broadcasts b left join broadcast_deliveries bd on bd.broadcast_id=b.id
                                        left join notification_deliveries d on d.id=bd.delivery_id group by b.id order by b.id desc limit 15""",
                    }
                    data = await rows(conn, statements[name])
                    markup = None
                    if name == "orders" and data:
                        markup = buttons(*[({"text": f"Одобрить · {row['user_id']}", "callback_data": f"review:y:{row['id']}"},
                                             {"text": "Отклонить", "callback_data": f"review:n:{row['id']}"}) for row in data])
                    if name == "tickets" and data:
                        markup = buttons(*[({"text": f"Открыть #{row['id']}", "callback_data": f"ticket:{row['id']}"},) for row in data])
                    await message.answer("\n".join(" · ".join(map(str, row.values())) for row in data)[:3900] or "Записей нет.",
                                         reply_markup=markup)
                    return
                if name in {"grant", "access", "dm"}:
                    payload["target"] = await resolve(conn, args[0])
                if name == "grant":
                    term = args[1]
                    match = re.fullmatch(r"([1-9][0-9]{0,3})(h|d)", term)
                    if term != "forever" and not match:
                        raise ValueError("Укажите срок, например 24h, 30d или forever")
                    payload["seconds"] = None if term == "forever" else int(match[1]) * (DAY if match[2] == "d" else 3600)
                    payload["reason"] = " ".join(args[2:])[:500]
                elif name == "invite":
                    payload["days"] = int(args[0])
                    if not 1 <= payload["days"] <= 3650:
                        raise ValueError("Срок от 1 до 3650 дней")
                elif name == "access":
                    payload["operation"] = args[1]
                    if payload["operation"] not in {"revoke", "freeze", "unfreeze", "block", "unblock"}:
                        raise ValueError(ADMIN["Подписки"])
                elif name in {"price", "discount"}:
                    payload["price"] = int(args[0])
                    if not 1 <= payload["price"] <= 100000:
                        raise ValueError("Допустимая цена от 1 до 100000 Stars")
                    if name == "discount":
                        payload["hours"] = int(args[1])
                        if not 0 <= payload["hours"] <= 8760:
                            raise ValueError("Допустимый срок от 0 до 8760 часов")
                elif name == "description":
                    if not 1 <= len(raw) <= 255:
                        raise ValueError("Описание от 1 до 255 символов")
                    payload["description"] = raw
                elif name == "plan":
                    if raw not in {"automatic", "manual", "on", "off"}:
                        raise ValueError(ADMIN["Настройки"])
                    payload["mode"] = raw
                elif name == "refund":
                    payload["id"] = str(UUID(args[0]))
                elif name in {"reply", "close_ticket", "dm"}:
                    if name != "dm":
                        payload["ticket"] = int(args[0])
                    if name != "close_ticket":
                        payload["text"] = raw.partition(" ")[2]
                        if not payload["text"]:
                            raise ValueError("Добавьте текст сообщения")
                        payload.update(media_of(message))
                elif name == "emojiset":
                    if args[0] not in EMOJIS or not emoji_ids(message):
                        raise ValueError("Категории: " + ", ".join(EMOJIS))
                    payload = {"key": args[0], "id": emoji_ids(message)[0]}
                elif name == "promocreate":
                    code, kind, value, days, limit, individual, new = args
                    value, days, limit, individual = map(int, (value, days, limit, individual))
                    if (not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", code) or kind not in {"percent", "fixed", "days"}
                            or not all(0 < n <= 100000 for n in (value, days, limit, individual))
                            or (kind == "percent" and value > 99) or new not in {"0", "1"}):
                        raise ValueError(ADMIN["Промокоды"])
                    payload = dict(code=code.upper(), kind=kind, value=value, days=days, limit=limit,
                                   individual=individual, new=new == "1")
                    if await scalar(conn, "select code from promo_codes where code=:c", c=payload["code"]):
                        raise ValueError("Код уже существует")
                elif name == "promotoggle":
                    payload = {"code": args[0].upper(), "enabled": args[1] == "on"}
                    if args[1] not in {"on", "off"}:
                        raise ValueError("/promotoggle КОД on|off")
                elif name == "broadcast":
                    segment, minute, body = raw.split(maxsplit=2)
                    segments = {"all", "active", "paid", "free", "none", "expired", "new"}
                    if segment not in segments or not 0 <= int(minute) <= 43200:
                        raise ValueError(ADMIN["Рассылка"])
                    fields = [part.strip() for part in body.split("|", 2)]
                    data = {"text": fields[0], **media_of(message)}
                    if not fields[0] or len(fields[0]) > (1024 if data.get("media") else 3900):
                        raise ValueError("Проверьте длину текста рассылки")
                    original = message.text or message.caption or ""
                    position = original.find(fields[0], max(0, original.find(body)))
                    if position >= 0:
                        start = len(original[:position].encode("utf-16-le")) // 2
                        end = start + len(fields[0].encode("utf-16-le")) // 2
                        data["entities"] = [
                            dict(type="custom_emoji", offset=e.offset-start, length=e.length,
                                 custom_emoji_id=e.custom_emoji_id)
                            for e in (message.entities or message.caption_entities or [])
                            if e.custom_emoji_id and start<=e.offset and e.offset+e.length<=end
                        ]
                    if len(fields) == 3 and fields[1] and fields[2].startswith("https://"):
                        data.update(label=fields[1][:64], link=fields[2])
                    elif len(fields) != 1:
                        raise ValueError("Укажите текст кнопки и ссылку HTTPS")
                    summary = f"Предпросмотр рассылки · {segment} · через {minute} мин\n\n{fields[0]}"
                    from goat.jobs import deliver
                    await deliver(message.bot, conn, {"user_id": uid, "payload": data}, config.app_url)
                    payload = {"segment": segment, "minute": int(minute), "data": data}
                elif name == "bcancel":
                    payload["id"] = int(args[0])
                elif name == "fees":
                    market, value = raw.split(maxsplit=1)
                    if market not in {"Portals", "MRKT"}:
                        raise ValueError("Неизвестная площадка")
                    fields = {"buy_fee", "sell_fee", "transfer", "network", "conversion", "extra_sell"}
                    values = json.loads(value, parse_float=Decimal)
                    if set(values) != fields:
                        raise ValueError("Поля: " + ", ".join(sorted(fields)))
                    checked = Calculation(buy=1, sell=1, **values)
                    payload = {"market": market, "data": {key: str(getattr(checked, key)) for key in fields}}
                elif name == "rate":
                    base, quote, amount, source = raw.split(maxsplit=3)
                    rate = Decimal(amount)
                    if base not in {"GRAM", "TON"} or quote not in {"GRAM", "TON"} or base == quote or not rate.is_finite() or rate <= 0:
                        raise ValueError(ADMIN["Настройки"])
                    payload = {"base": base, "quote": quote, "rate": amount, "source": source}
                elif name == "legal":
                    values = json.loads(raw)
                    if set(values) != {"owner", "terms", "privacy", "risk"} or not all(isinstance(v,str) and 0<len(v)<=3500 for v in values.values()):
                        raise ValueError("JSON с полями owner, terms, privacy, risk")
                    payload = values
                elif name == "report":
                    hour = None if raw == "off" else int(raw)
                    if hour is not None and not 0 <= hour < 24:
                        raise ValueError("/report ЧАС_UTC|off")
                    payload = {"hour": hour}
            await confirm(message, uid, name, payload, summary)
        except (IndexError, ValueError, ArithmeticError, TypeError) as exc:
            await message.answer(f"Проверьте параметры. {exc}")

    async def apply(conn, data, callback):
        actor, action, p = data["actor"], data["action"], data["payload"]
        target = p.get("target")
        key = f"{action}:{callback.id}"
        if action == "review":
            await approve(conn, UUID(p["id"]), actor, p["allow"])
            return "Заявка обработана."
        if action == "grant":
            entry = await grant(conn, target, p["seconds"], "lifetime" if p["seconds"] is None else "manual_grant",
                                actor=actor, reason=p["reason"])
            term = "Навсегда" if p["seconds"] is None else str(timedelta(seconds=p["seconds"]))
            await enqueue(conn, target, f"grant:{entry['id']}", text=f"🎁 Вам выдан бесплатный доступ · {term}.")
            await notify_admins(conn, config.admins, f"grant-admin:{entry['id']}",
                                text=f"ДОСТУП ВЫДАН\nTelegram ID {target}\nСрок · {term}\nТип · бесплатный\nВыручка · 0 Stars",
                                emoji="gift")
        elif action == "access":
            await manage_access(conn, target, p["operation"], actor)
            return "Доступ обновлён."
        elif action == "price":
            await query(conn, "update subscription_plans set price=:p where id=:id", p=p["price"], id=PLAN)
        elif action == "discount":
            await query(conn, """update subscription_plans set discount=:p,discount_until=now()+make_interval(hours=>:h)
                             where id=:id""", p=p["price"], h=p["hours"], id=PLAN)
        elif action == "description":
            await query(conn, "update subscription_plans set description=:d where id=:id", d=p["description"], id=PLAN)
        elif action == "plan":
            if p["mode"] in {"manual", "automatic"}:
                await query(conn, "update subscription_plans set approval=:v where id=:id", v=p["mode"]=="manual", id=PLAN)
            else:
                await query(conn, "update subscription_plans set enabled=:v where id=:id", v=p["mode"]=="on", id=PLAN)
        elif action == "refund":
            purchase = await one(conn, """
                select p.charge,o.user_id from payments p join orders o on o.id=p.order_id
                where o.id=:id and not exists(select 1 from refunds r where r.charge=p.charge)
            """, id=UUID(p["id"]))
            if not purchase:
                raise ValueError("Платёж не найден или уже возвращён")
            await query(conn, "select id from users where id=:u for update", u=purchase["user_id"])
            try:
                await callback.bot.refund_star_payment(purchase["user_id"], purchase["charge"])
            except TelegramBadRequest as exc:
                if "CHARGE_ALREADY_REFUNDED" not in exc.message:
                    raise ValueError("Telegram отклонил возврат") from exc
            await record_refund(conn, purchase["charge"], purchase["user_id"], actor)
            return "Возврат зарегистрирован."
        elif action == "dm":
            await enqueue(conn, target, key, **{k: v for k,v in p.items() if k != "target"})
        elif action in {"reply", "close_ticket"}:
            tid = p["ticket"]
            ticket = await one(conn, "select * from support_tickets where id=:id for update", id=tid)
            if not ticket or ticket["status"] == "closed":
                raise ValueError("Обращение закрыто или отсутствует")
            target = ticket["user_id"]
            status = "closed" if action == "close_ticket" else "waiting_user"
            await query(conn, "update support_tickets set status=:s,unread_admin=0,unread_user=unread_user+1 where id=:id", s=status, id=tid)
            if action == "reply":
                await save_message(conn, tid, actor, p.get("text"), p)
                await enqueue(conn, target, key, text=f"Поддержка #{tid}\n{p['text']}"[:1024] if p.get("media")
                              else f"Поддержка #{tid}\n{p['text']}", ticket=tid,
                              **{k:v for k,v in p.items() if k in {"media","type"}})
            else:
                await enqueue(conn, target, key, text=f"Обращение #{tid} закрыто.")
        elif action == "emojiset":
            await query(conn, """
                insert into custom_emojis values(:key,:id)
                on conflict(category) do update set emoji_id=excluded.emoji_id
            """, **p)
        elif action in {"invite", "promocreate"}:
            if action == "invite":
                p = dict(code=secrets.token_urlsafe(12).upper(), kind="days", value=p["days"], days=7,
                         limit=1, individual=1, new=False)
            await query(conn, """
                insert into promo_codes(code,kind,value,expires_at,max_uses,per_user,new_only,actor)
                values(:code,:kind,:value,now()+make_interval(days=>:days),:limit,:individual,:new,:actor)
                on conflict(code) do nothing
            """, **p, actor=actor)
            await audit(conn, actor, action, p["code"])
            if action == "invite":
                username = (await callback.bot.get_me()).username
                return f"https://t.me/{username}?start=gift_{p['code']}"
            return "Промокод создан."
        elif action == "promotoggle":
            await query(conn, "update promo_codes set enabled=:enabled where code=:code", **p)
        elif action == "broadcast":
            target = await scalar(conn, """insert into broadcasts(actor,segment,payload,status,run_at)
                       values(:actor,:s,cast(:p as jsonb),'queued',now()+make_interval(mins=>:m)) returning id""",
                                  actor=actor, s=p["segment"], p=json.dumps(p["data"]), m=p["minute"])
        elif action == "bcancel":
            await query(conn, "update broadcasts set status='cancelled' where id=:id and status='queued'", **p)
            await query(conn, "update notification_deliveries set status='skipped' where dedupe=:d and status='queued'",
                        d=f"broadcast:{p['id']}")
        elif action == "fees":
            await query(conn, """insert into marketplace_fees(market,fees) values(:m,cast(:d as jsonb))
                 on conflict(market) do update set fees=excluded.fees,updated_at=now()""",
                        m=p["market"], d=json.dumps(p["data"]))
        elif action == "rate":
            await query(conn, """insert into currency_rates values(:base,:quote,now(),cast(cast(:rate as text) as numeric),:source)""",
                        **p)
        elif action in {"legal", "report"}:
            await query(conn, """insert into system_settings values(:key,cast(:data as jsonb))
                               on conflict(key) do update set value=excluded.value""", key=action, data=json.dumps(p))
        else:
            raise ValueError("Неизвестное действие")
        await audit(conn, actor, action, target, **{k: v for k, v in p.items()
                    if k not in {"target", "text", "data", "media"}})
        return "Настройки сохранены." if action != "broadcast" else f"Рассылка #{target} запланирована."

    @router.callback_query()
    async def callback(call: CallbackQuery):
        if not isinstance(call.message, Message) or call.message.chat.type != "private":
            await call.answer()
            return
        uid, data = call.from_user.id, call.data or ""
        await call.answer()
        try:
            if data == "buy":
                await create_order(call.message, call.from_user)
            elif data == "profile":
                await show_profile(call.message, call.from_user)
            elif data == "about":
                await call.message.answer("GOAT Gifts AI анализирует рынок подарков. В терминале доступны демо, "
                                          "виртуальный портфель и калькулятор. С GOAT PRO открываются актуальные сигналы, "
                                          "если подключены источники рыночных данных.", reply_markup=user_menu(config))
            elif data == "support":
                await support_menu(call.message)
            elif data.startswith(("category:", "ticket:", "toggle:")):
                async with db.transaction() as conn:
                    await touch(conn, call.from_user)
                    if data.startswith("category:"):
                        if (await profile(conn, uid))["blocked"]:
                            raise ValueError("Создание обращений недоступно")
                        category = CATEGORIES[int(data.split(":")[1])]
                        tid = await scalar(conn, """insert into support_tickets(user_id,category)
                                    values(:uid,:cat) returning id""", uid=uid, cat=category)
                        await select_ticket(conn, uid, tid)
                        await notify_admins(conn, config.admins, f"new-ticket:{tid}",
                                            text=f"Новое обращение #{tid} · {category}\nTelegram ID {uid}",
                                            ticket=tid, emoji="support")
                        result = f"Обращение #{tid} открыто. Отправьте текст или файл в этот чат."
                    elif data.startswith("toggle:"):
                        key = data.split(":")[1]
                        if key not in {"signals", "marketing", "reminders"}:
                            raise ValueError("Настройка недоступна")
                        prefs = Preferences.model_validate((await profile(conn, uid))["prefs"])
                        enabled = not getattr(prefs, key)
                        await save_prefs(conn, uid, prefs.model_copy(update={key: enabled}))
                        result = "Уведомления включены." if enabled else "Уведомления отключены."
                    else:
                        tid = int(data.split(":")[1])
                        ticket = await one(conn, "select * from support_tickets where id=:id", id=tid)
                        if not ticket or (uid not in config.admins and ticket["user_id"] != uid):
                            raise ValueError("Обращение недоступно")
                        if uid in config.admins:
                            await query(conn, "update support_tickets set unread_admin=0 where id=:id", id=tid)
                            messages = await rows(conn, """select id,body,media_type,file_id,actor from support_messages
                                     where ticket_id=:id order by id desc limit 10""", id=tid)
                            result = (f"#{tid} · {ticket['category']} · {ticket['status']}\nTelegram ID {ticket['user_id']}\n"
                                + "\n".join(f"{m['actor']} · {m['body'] or m['media_type']}" for m in reversed(messages))
                                + f"\n/reply {tid} ТЕКСТ\n/user {ticket['user_id']}\n/close_ticket {tid}\n/access {ticket['user_id']} block")
                            from goat.jobs import deliver
                            for item in reversed(messages):
                                if item["file_id"]:
                                    await deliver(call.bot, conn, {"user_id": uid, "payload": {
                                        "media": item["file_id"], "type": item["media_type"],
                                        "text": f"Обращение #{tid} · сообщение {item['id']}"}}, config.app_url)
                        else:
                            if ticket["status"] == "closed":
                                raise ValueError("Обращение закрыто. Откройте новое через /support.")
                            await select_ticket(conn, uid, tid)
                            await query(conn, "update support_tickets set unread_user=0 where id=:id", id=tid)
                            result = f"Активное обращение #{tid}. Отправьте новое сообщение."
                if data.startswith("ticket:") and uid in config.admins:
                    await call.message.answer(result[:4000], reply_markup=buttons(
                        ({"text":"Ответить","callback_data":f"ticketreply:{tid}"},
                         {"text":"Профиль","callback_data":f"ticketuser:{ticket['user_id']}"}),
                        ({"text":"Закрыть","callback_data":f"ticketclose:{tid}"},
                         {"text":"Блокировать","callback_data":f"ticketblock:{ticket['user_id']}"})))
                else:
                    await call.message.answer(result[:4000])
            elif uid in config.admins:
                if data.startswith("owner:"):
                    label = list(ADMIN)[int(data.split(":")[1])]
                    direct = {"Статистика": ("stats", "1"), "Продажи": ("stats", "30"),
                              "Заявки": ("orders", ""), "Поддержка": ("tickets", ""), "Сервер": ("server", "")}
                    if label in direct:
                        cmd, args = direct[label]
                        await owner(call.message.model_copy(update={"from_user":call.from_user}),
                                    CommandObject(command=cmd, args=args))
                    elif label == "Выдать доступ":
                        async with db.transaction() as conn:
                            await set_admin_input(conn, uid, {"action":"grant_user"})
                        await call.message.answer("Введите Telegram ID или @username. Для приглашения используйте /invite ДНИ.")
                    else:
                        await call.message.answer(ADMIN[label])
                elif data.startswith("granttime:"):
                    _, target, seconds = data.split(":")
                    async with db.transaction() as conn:
                        await set_admin_input(conn, uid, {"action":"grant_reason","target":int(target),
                                                           "seconds":int(seconds) if seconds != "forever" else None})
                    await call.message.answer("Введите причину или отправьте «-».")
                elif data.startswith(("ticketreply:", "ticketuser:", "ticketclose:", "ticketblock:")):
                    action, target = data.split(":")
                    if action == "ticketreply":
                        async with db.transaction() as conn:
                            await set_admin_input(conn, uid, {"action":"reply_ticket","ticket":int(target)})
                        await call.message.answer(f"Отправьте ответ для обращения #{target}.")
                    else:
                        cmd = {"ticketuser":"user","ticketclose":"close_ticket","ticketblock":"access"}[action]
                        args = target+" block" if cmd=="access" else target
                        await owner(call.message.model_copy(update={"from_user":call.from_user}),
                                    CommandObject(command=cmd,args=args))
                elif data.startswith("review:"):
                    _, decision, oid = data.split(":")
                    await confirm(call.message, uid, "review", {"allow": decision == "y", "id": str(UUID(oid))},
                                  "Подтвердить решение по заявке?")
                elif data.startswith(("confirm:", "cancel:")):
                    action, token = data.split(":", 1)
                    async with db.transaction() as conn:
                        pending = await one(conn, "select value from system_settings where key=:k for update",
                                            k=f"confirm:{token}")
                        if not pending or pending["value"]["actor"] != uid:
                            raise ValueError("Действие уже обработано")
                        stored = pending["value"]
                        result = "Действие отменено."
                        if stored["expires"] < datetime.now(UTC).timestamp():
                            result = "Подтверждение истекло."
                        elif action == "confirm":
                            result = await apply(conn, stored, call)
                        await query(conn, "delete from system_settings where key=:k", k=f"confirm:{token}")
                    await call.message.answer(result)
        except (ValueError, IndexError, ArithmeticError) as exc:
            await call.message.answer(str(exc))

    @router.message()
    async def inbox(message: Message):
        uid = message.from_user.id
        media = media_of(message)
        body = message.text or message.caption
        if not body and not media:
            return
        if uid in config.admins:
            async with db.transaction() as conn:
                pending = await scalar(conn, "select value from system_settings where key=:k for update",
                                       k=f"admin-input:{uid}")
                if pending:
                    await query(conn, "delete from system_settings where key=:k", k=f"admin-input:{uid}")
            if pending and pending["expires"] >= datetime.now(UTC).timestamp():
                try:
                    if pending["action"] == "grant_user":
                        async with db.transaction() as conn:
                            target = await resolve(conn, (body or "").strip())
                        terms = (("24 часа",3600*24),("3 дня",3*DAY),("7 дней",7*DAY),("14 дней",14*DAY),
                                 ("30 дней",30*DAY),("90 дней",90*DAY),("Навсегда","forever"))
                        await message.answer(f"Срок доступа для {target}", reply_markup=buttons(*[
                            ({"text":label,"callback_data":f"granttime:{target}:{seconds}"},) for label,seconds in terms]))
                        await message.answer(f"Для своего срока используйте /grant {target} 45d [причина].")
                    elif pending["action"] == "grant_reason":
                        p = {"target":pending["target"],"seconds":pending["seconds"],
                             "reason":"" if body == "-" else (body or "")[:500]}
                        await confirm(message, uid, "grant", p, f"Выдать бесплатный доступ пользователю {p['target']}?")
                    elif pending["action"] == "reply_ticket":
                        p = {"ticket":pending["ticket"], "text":body or "", **media}
                        await confirm(message, uid, "reply", p, f"Отправить ответ в обращение #{p['ticket']}?")
                except ValueError as exc:
                    await message.answer(str(exc))
                return
        async with db.transaction() as conn:
            user = await touch(conn, message.from_user)
            if user["blocked"]:
                return
            selected = await scalar(conn, "select value from system_settings where key=:k", k=f"ticket:{uid}")
            ticket = await one(conn, """select * from support_tickets where id=:id and user_id=:uid
                                      and status<>'closed' for update""", id=selected, uid=uid) if selected else None
            if not ticket:
                await message.answer("Откройте меню через /start или создайте обращение через /support.")
                return
            tid = ticket["id"]
            mid = await save_message(conn, tid, uid, body, media)
            await query(conn, "update support_tickets set status='waiting_admin',unread_admin=unread_admin+1 where id=:id", id=tid)
            await notify_admins(conn, config.admins, f"ticket-message:{mid}",
                                text=f"Обращение #{tid} · новое сообщение\nTelegram ID {uid}", ticket=tid,
                                emoji="support")
        await message.answer(f"Сообщение добавлено в обращение #{tid}.")

    return dp


def media_of(message):
    for kind, item in (("photo", message.photo[-1] if message.photo else None), ("video", message.video),
                       ("document", message.document), ("voice", message.voice)):
        if item:
            return {"type": kind, "media": item.file_id}
    return {}


async def save_message(conn, tid, actor, body, media):
    return await scalar(conn, """
        insert into support_messages(ticket_id,actor,body,media_type,file_id)
        values(:id,:actor,:body,:kind,:file) returning id
    """, id=tid, actor=actor, body=body, kind=media.get("type"), file=media.get("media"))


async def select_ticket(conn, uid, tid):
    await query(conn, """
        insert into system_settings values(:k,to_jsonb(cast(:id as bigint)))
        on conflict(key) do update set value=excluded.value
    """, k=f"ticket:{uid}", id=tid)


async def set_admin_input(conn, uid, payload):
    payload["expires"] = datetime.now(UTC).timestamp() + 600
    await query(conn, """
        insert into system_settings(key,value) values(:key,cast(:v as jsonb))
        on conflict(key) do update set value=excluded.value
    """, key=f"admin-input:{uid}", v=json.dumps(payload))
