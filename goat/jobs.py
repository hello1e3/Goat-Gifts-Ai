import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import logging
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, WebAppInfo

from goat.accounts import enqueue, profile
from goat.db import one, query, rows, scalar
from goat.terminal import Preferences, signals

log = logging.getLogger(__name__)


def buttons(*groups):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**item) for item in group]
                                                  for group in groups])


EMOJIS = dict(zip(
    "logo rocket money ton stars gift chart profit loss users premium settings support success warning loading crown shield clock fire".split(),
    "◈ 🚀 💰 💎 ⭐ 🎁 📊 📈 📉 👥 💎 ⚙️ 🎧 ✅ ⚠️ ⏳ 👑 🛡️ 🕒 🔥".split(),
    strict=True,
))


class EmojiManager:
    @staticmethod
    async def send(bot, conn, uid, text, category, **kwargs):
        emoji = EMOJIS[category]
        configured = await one(conn, "select emoji_id from custom_emojis where category=:category", category=category)
        if configured:
            from aiogram.types import MessageEntity

            entity = MessageEntity(type="custom_emoji", offset=0, length=len(emoji.encode("utf-16-le")) // 2,
                                   custom_emoji_id=configured["emoji_id"])
            try:
                return await bot.send_message(uid, emoji + " " + text, entities=[entity], **kwargs)
            except TelegramBadRequest:
                pass
        return await bot.send_message(uid, emoji + " " + text, **kwargs)


async def deliver(bot, conn, entry, app_url):
    uid, data = entry["user_id"], entry["payload"]
    if data.get("invoice"):
        order = await one(conn, """
            select * from orders where id=:id and user_id=:uid and status='awaiting_payment' and expires_at>now()
        """, id=UUID(data["invoice"]), uid=uid)
        if order:
            await bot.send_invoice(uid, title=order["plan_id"], description=order["description"],
                                   payload=str(order["id"]), currency="XTR", provider_token="",
                                   prices=[LabeledPrice(label=order["plan_id"], amount=order["amount"])],
                                   start_parameter="goat-pro")
        return bool(order)
    markup = None
    if data.get("approval"):
        oid = data["approval"]
        markup = buttons(({"text": "Одобрить", "callback_data": f"review:y:{oid}"},
                          {"text": "Отклонить", "callback_data": f"review:n:{oid}"}))
    if data.get("card_request"):
        request_id = data["card_request"]
        markup = buttons(({"text": "✅ Подтвердить перевод", "callback_data": f"cardreview:y:{request_id}"},
                          {"text": "Отклонить", "callback_data": f"cardreview:n:{request_id}"}))
    if data.get("ticket"):
        tid = data["ticket"]
        markup = buttons(({"text": "Открыть обращение", "callback_data": f"ticket:{tid}"},))
    if data.get("broadcast"):
        broadcast = await one(conn, """
            select b.status,coalesce((s.prefs->>'marketing')::boolean,false) as consent
            from broadcasts b left join user_settings s on s.user_id=:uid where b.id=:id
        """, uid=uid, id=data["broadcast"])
        if not broadcast or broadcast["status"] != "queued" or not broadcast["consent"]:
            return False
        if data.get("link") and data.get("label"):
            markup = buttons(({"text": data["label"], "url": data["link"]},))
    if data.get("signal"):
        access = await profile(conn, uid)
        signal = await one(conn, """
            select t.* from trading_signals t join system_health h on h.component=t.listing_market
            join system_health e on e.component=t.details->>'exit_market'
            where t.id=:id and h.status='online' and e.status='online'
              and t.state in ('potential','active_offer','risky') and t.observed_at>now()-interval '3 minutes'
        """, id=data["signal"])
        if not access or not access["active"] or not access["prefs"].get("signals", True) or not signal:
            return False
        d = signal["details"]
        if signal['listing_market']=='Portals':
            checked = d.get('comparison_checked_at')
            if not checked or (datetime.now(UTC)-datetime.fromisoformat(checked)).total_seconds()>180:
                return False
        text = (f"🔎 ПОТЕНЦИАЛЬНО ВЫГОДНЫЙ ПОДАРОК\n\n"
                f"🎁 {d['title']} #{d['number']}\n"
                f"🎨 {d['model']} · {d['backdrop']} · {d['symbol']}\n\n"
                f"🛒 Купить: {Decimal(d['purchase']):.2f} {d['currency']}\n"
                f"📊 Ориентир выхода: {Decimal(d['sale']):.2f} {d['currency']}\n"
                f"💳 Полная себестоимость: {Decimal(d['cost']):.2f} {d['currency']}\n"
                f"💰 Чистая выручка: {Decimal(d['proceeds']):.2f} {d['currency']}\n"
                f"📈 Расчётная прибыль: +{Decimal(d['profit']):.2f} {d['currency']}\n"
                f"🚀 ROI: +{Decimal(d['roi']):.2f}%\n"
                f"🛡 Консервативный сценарий: {Decimal(d['conservative_profit']):+.2f} {d['currency']}\n\n"
                f"⚠️ {d['risk']}")
        data = {"text": text, "emoji": "gift"}
        if isinstance(d.get("image"), str) and d["image"].startswith("https://"):
            data.update(media=d["image"], type="photo")
        actions = []
        if d.get("url"):
            actions.append({"text": "🛒 Купить в Portals", "url": d["url"]})
        if app_url:
            actions.append({"text": "📊 Открыть аналитику",
                            "web_app": WebAppInfo(url=f"{app_url.rstrip('/')}/?signal={signal['id']}")})
        if actions:
            markup = buttons(tuple(actions))
    custom = data.get("entities")
    if data.get("media"):
        send = getattr(bot, "send_" + data["type"])
        if custom:
            try:
                await send(uid, data["media"], caption=data.get("text"), caption_entities=custom, reply_markup=markup)
                return True
            except TelegramBadRequest:
                pass
        try:
            await send(uid, data["media"], caption=data.get("text"), reply_markup=markup)
        except TelegramBadRequest:
            await bot.send_message(uid, data["text"], reply_markup=markup)
    elif custom:
        try:
            await bot.send_message(uid, data["text"], entities=custom, reply_markup=markup)
        except TelegramBadRequest:
            await bot.send_message(uid, data["text"], reply_markup=markup)
    elif data.get("emoji") in EMOJIS:
        await EmojiManager.send(bot, conn, uid, data["text"], data["emoji"], reply_markup=markup)
    else:
        await bot.send_message(uid, data["text"], reply_markup=markup)
    return True


async def outbox(db, bot, app_url):
    async with db.transaction() as conn:
        on_hold = await scalar(conn, """
            select (value->>'until')::timestamptz>now() from system_settings where key='telegram_flood'
        """)
        if on_hold:
            return False
        entry = await one(conn, """
            select d.*,u.unreachable,u.blocked,u.erased from notification_deliveries d join users u on u.id=d.user_id
            where d.status='queued' and d.run_at<=now() order by d.run_at,d.id for update of d skip locked limit 1
        """)
        if not entry:
            return False
        status, delay = "sent", 0
        if entry["unreachable"] or entry["erased"] or (entry["blocked"] and not entry["payload"].get("ticket")
                                                        and not entry["dedupe"].startswith("refund:")):
            status = "skipped"
        else:
            try:
                if not await deliver(bot, conn, entry, app_url):
                    status = "skipped"
            except TelegramRetryAfter as exc:
                status, delay = "queued", exc.retry_after + 1
                await query(conn, """
                    insert into system_settings(key,value) values('telegram_flood',
                        jsonb_build_object('until',now()+make_interval(secs=>:delay)))
                    on conflict(key) do update set value=excluded.value
                    where (system_settings.value->>'until')::timestamptz<(excluded.value->>'until')::timestamptz
                """, delay=delay)
            except TelegramForbiddenError:
                status = "failed"
                await query(conn, "update users set unreachable=true where id=:uid", uid=entry["user_id"])
            except TelegramBadRequest:
                status = "failed"
            except Exception:
                log.exception("Delivery failed for outbox ID %s", entry["id"])
                status = "failed" if entry["attempts"] >= 5 else "queued"
                delay = 2 ** entry["attempts"]
        await query(conn, """
            update notification_deliveries set status=:status,attempts=attempts+1,
            run_at=case when :status='queued' then now()+make_interval(secs=>:delay) else run_at end where id=:id
        """, status=status, delay=delay, id=entry["id"])
        return True


async def fanout(conn):
    await query(conn, """
        update orders set status='expired' where status in ('pending_approval','approved','awaiting_payment')
          and expires_at<=now() and (checkout_at is null or checkout_at<now()-interval '1 day')
    """)
    await query(conn, """
        with terms as (
          select g.user_id,max(g.ends_at) ends_at from access_grants g join users u on u.id=g.user_id
          join subscriptions s on s.user_id=u.id left join user_settings us on us.user_id=u.id
          where not u.blocked and not u.erased and not u.unreachable and s.frozen_at is null
          and coalesce((us.prefs->>'reminders')::boolean,true) and g.revoked_at is null
          group by g.user_id having not bool_or(g.ends_at is null)
        ), due as (
          select *,case when ends_at<=now() then 'expired' when ends_at<=now()+interval '1 day' then '1d'
            when ends_at<=now()+interval '3 days' then '3d' end as stage from terms
          where ends_at>=now()-interval '1 day'
        )
        insert into notification_deliveries(user_id,dedupe,payload)
        select user_id,'reminder:'||extract(epoch from ends_at)::text||':'||stage,
           jsonb_build_object('text',case stage when 'expired' then 'GOAT PRO истёк. Продление доступно через /buy.'
             when '1d' then 'До окончания GOAT PRO меньше суток. Продление доступно через /buy.'
             else 'До окончания GOAT PRO меньше трёх дней. Продление доступно через /buy.' end)
        from due where stage is not null on conflict do nothing
    """)
    await query(conn, """
        with active as (
          select g.user_id,bool_or(g.kind='paid') paid from access_grants g join subscriptions s using(user_id)
          where g.revoked_at is null and s.frozen_at is null and g.starts_at<=now()
          and (g.ends_at is null or g.ends_at>now()) group by g.user_id
        ), inserted as (
          insert into notification_deliveries(user_id,dedupe,payload)
          select u.id,'broadcast:'||b.id,b.payload||jsonb_build_object('broadcast',b.id)
          from broadcasts b cross join users u join user_settings p on p.user_id=u.id
          left join active a on a.user_id=u.id where b.status='queued' and b.run_at<=now()
          and not u.blocked and not u.erased and not u.unreachable and (p.prefs->>'marketing')::boolean
          and case b.segment when 'active' then a.user_id is not null when 'paid' then a.paid
            when 'free' then not a.paid when 'none' then a.user_id is null when 'new' then u.created_at>now()-interval '7 days'
            when 'expired' then a.user_id is null and exists(select 1 from access_grants g where g.user_id=u.id
              and g.ends_at<now() and g.revoked_at is null) else true end
          on conflict do nothing returning id,dedupe
        )
        insert into broadcast_deliveries(broadcast_id,delivery_id)
        select split_part(dedupe,':',2)::bigint,id from inserted
    """)
    await query(conn, """
        update broadcasts b set status='completed' where status='queued' and run_at<=now()
        and not exists(select 1 from broadcast_deliveries bd join notification_deliveries d on d.id=bd.delivery_id
                       where bd.broadcast_id=b.id and d.status='queued')
    """)
    await query(conn, """
        delete from system_settings where (key like 'confirm:%' or key like 'admin-input:%')
          and (value->>'expires')::double precision<extract(epoch from now())
    """)
    now = datetime.now(UTC)
    subscribers = await rows(conn, """
        select u.id,coalesce(p.prefs,jsonb_build_object('signals',true)) prefs
        from users u left join user_settings p on p.user_id=u.id
        join subscriptions s on s.user_id=u.id where not u.blocked and not u.unreachable and not u.erased
          and s.frozen_at is null and coalesce((p.prefs->>'signals')::boolean,true) and exists (
          select 1 from access_grants g where g.user_id=u.id and g.revoked_at is null and g.starts_at<=now()
            and (g.ends_at is null or g.ends_at>now()))
    """)
    for user in subscribers:
        prefs = Preferences.model_validate(user["prefs"])
        start, end = prefs.quiet_start, prefs.quiet_end
        if start is not None and end is not None:
            hour = now.hour
            if (start <= hour < end) if start <= end else (hour >= start or hour < end):
                continue
        recent = await one(conn, """
            select id from notification_deliveries where user_id=:uid and dedupe like 'signal:%'
              and run_at>now()-make_interval(mins=>:mins) limit 1
        """, uid=user["id"], mins=prefs.frequency_minutes)
        if recent:
            continue
        for signal in await signals(conn, prefs):
            if await enqueue(conn, user["id"], f"signal:{signal['id']}", signal=signal["id"]):
                break


async def loop(delay, operation):
    while True:
        try:
            interval = await operation()
        except Exception:
            log.exception("Background job failed")
            interval = None
        await asyncio.sleep(delay if interval is None else interval)

