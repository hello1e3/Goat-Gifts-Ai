from datetime import UTC, datetime
from uuid import uuid4

from goat.accounts import audit, enqueue, grant, notify_admins, revoke_grant
from goat.db import one, query, rows, scalar

PLAN = "GOAT PRO"
DAY = 86400
PLAN_TERMS = {
    30: (PLAN, 1),
    180: ("GOAT PRO 180", 5),
    365: ("GOAT PRO 365", 9),
}


async def init_plan(conn, config):
    await query(conn, """
        insert into subscription_plans(id,description,price,approval)
        values(:id,'30 дней доступа к GOAT Gifts AI',:price,:approval) on conflict do nothing
    """, id=PLAN, price=config.default_subscription_price_xtr, approval=config.purchase_approval_required)
    base = await scalar(conn, 'select price from subscription_plans where id=:id', id=PLAN)
    for days, (plan_id, multiplier) in PLAN_TERMS.items():
        if days == 30:
            continue
        await query(conn, """
            insert into subscription_plans(id,description,price,approval)
            values(:id,:description,:price,:approval) on conflict do nothing
        """, id=plan_id, description=f"{days} дней доступа к GOAT Gifts AI",
             price=base * multiplier if base else None, approval=config.purchase_approval_required)


async def promo_for_update(conn, uid, code):
    promo = await one(conn, """
        select * from promo_codes where code=:code and enabled and expires_at>now() for update
    """, code=code.upper())
    if promo is None:
        raise ValueError("Промокод недействителен")
    counts = await one(conn, """
        select count(*) total,count(*) filter(where r.user_id=:uid) personal
        from promo_redemptions r left join orders o on o.id=r.order_id
        where r.code=:code and (r.order_id is null or o.status not in ('rejected','expired'))
    """, uid=uid, code=promo["code"])
    if counts["total"] >= promo["max_uses"] or counts["personal"] >= promo["per_user"]:
        raise ValueError("Лимит активаций исчерпан")
    if promo["new_only"] and await scalar(conn, """
        select exists(select 1 from orders where user_id=:uid and status in ('paid','activated','refunded'))
    """, uid=uid):
        raise ValueError("Промокод доступен для первой покупки")
    return promo


async def order(conn, uid, admins, code=None, term_days=30):
    user = await one(conn, "select blocked,erased from users where id=:uid for update", uid=uid)
    if user is None or user["blocked"] or user["erased"]:
        raise ValueError("Покупка недоступна")
    if term_days not in PLAN_TERMS:
        raise ValueError("Срок подписки недоступен")
    plan_id, _ = PLAN_TERMS[term_days]
    existing = await one(conn, """
        select * from orders where user_id=:uid and status in ('pending_approval','awaiting_payment')
          and plan_id=:plan
          and (expires_at>now() or checkout_at is not null) order by created_at desc limit 1
    """, uid=uid, plan=plan_id)
    if existing:
        return existing
    plan = await one(conn, "select * from subscription_plans where id=:id", id=plan_id)
    if not plan or not plan["enabled"] or not plan["price"]:
        raise ValueError("Продажи сейчас закрыты")
    price = plan["price"]
    now = datetime.now(UTC)
    if plan["discount_until"] and plan["discount_until"] > now:
        price = min(price, plan["discount"] or price)
    promo = await promo_for_update(conn, uid, code) if code else None
    if promo:
        if promo["kind"] == "days":
            raise ValueError("Для бесплатного кода используйте /promo")
        cut = price * promo["value"] // 100 if promo["kind"] == "percent" else promo["value"]
        price = max(1, price - cut)
    status = "pending_approval" if plan["approval"] else "awaiting_payment"
    result = await one(conn, """
        insert into orders(id,user_id,plan_id,description,amount,status,promo,duration_seconds)
        values(:id,:uid,:plan,:description,:price,:status,:promo,:duration) returning *
    """, id=uuid4(), uid=uid, plan=plan_id, description=plan["description"], price=price, status=status,
         duration=term_days * DAY,
         promo=promo["code"] if promo else None)
    if promo:
        await query(conn, "insert into promo_redemptions(code,user_id,order_id) values(:c,:u,:o)",
                    c=promo["code"], u=uid, o=result["id"])
    if plan["approval"]:
        await notify_admins(conn, admins, f"approval:{result['id']}",
                            text=f"⏳ Заявка GOAT PRO\nTelegram ID {uid}\n{price} ⭐",
                            approval=str(result["id"]))
    else:
        await enqueue(conn, uid, f"invoice:{result['id']}", invoice=str(result["id"]))
    return result


async def approve(conn, order_id, actor, allowed):
    result = await one(conn, """
        update orders set status=:status, expires_at=now()+interval '1 day'
        where id=:id and status='pending_approval' and expires_at>now() returning *
    """, id=order_id, status="awaiting_payment" if allowed else "rejected")
    if not result:
        raise ValueError("Заявка уже обработана или истекла")
    await audit(conn, actor, "approve" if allowed else "reject", order_id)
    payload = {"invoice": str(order_id)} if allowed else {"text": "Заявка отклонена. Вопросы принимает /support."}
    await enqueue(conn, result["user_id"], f"decision:{order_id}", **payload)
    return result


async def card_settings(conn):
    return await scalar(conn, "select value from system_settings where key='card_payment'")


async def card_request(conn, uid, amount=None, duration_seconds=30 * DAY):
    user = await one(conn, "select blocked,erased from users where id=:uid for update", uid=uid)
    if user is None or user["blocked"] or user["erased"]:
        raise ValueError("Покупка недоступна")
    settings = await card_settings(conn)
    if not settings or not settings.get("enabled") or not settings.get("amount"):
        raise ValueError("Оплата переводом сейчас недоступна")
    existing = await one(conn, """
        select * from manual_payments where user_id=:uid and status='pending'
        and duration_seconds=:duration
        order by created_at desc limit 1
    """, uid=uid,duration=duration_seconds)
    if existing:
        return existing
    return await one(conn, """
        insert into manual_payments(id,user_id,amount,currency,duration_seconds)
        values(:id,:uid,:amount,'RUB',:duration) returning *
    """, id=uuid4(), uid=uid, amount=amount or settings["amount"], duration=duration_seconds)


async def review_card(conn, request_id, actor, allowed):
    request = await one(conn, """
        update manual_payments set status=:status,reviewed_at=now(),actor=:actor
        where id=:id and status='pending' returning *
    """, id=request_id, actor=actor, status="approved" if allowed else "rejected")
    if not request:
        raise ValueError("Заявка уже обработана")
    await audit(conn, actor, "card_approve" if allowed else "card_reject", request_id)
    if allowed:
        entry = await grant(conn, request["user_id"], request["duration_seconds"], "paid", actor=actor,
                            reason=f"Перевод на карту · заявка {request_id}")
        await query(conn, "update manual_payments set grant_id=:grant where id=:id",
                    grant=entry["id"], id=request_id)
        await enqueue(conn, request["user_id"], f"card-approved:{request_id}",
                      text=f"Перевод подтверждён. GOAT PRO активирован на {request['duration_seconds'] // DAY} дней.")
    else:
        await enqueue(conn, request["user_id"], f"card-rejected:{request_id}",
                      text="Перевод не подтверждён. Проверьте реквизиты или обратитесь в /support.")
    return request


async def checkout(conn, oid, uid, amount, currency):
    return bool(await scalar(conn, """
        update orders o set checkout_at=now() from users u
        where o.id=:id and o.user_id=:uid and u.id=o.user_id and not u.blocked and not u.erased
          and o.amount=:amount and :currency='XTR' and o.status='awaiting_payment' and o.expires_at>now()
          and (o.checkout_at is null or o.checkout_at<now()-interval '10 minutes')
        returning o.id
    """, id=oid, uid=uid, amount=amount, currency=currency))


async def payment(conn, oid, uid, amount, currency, charge, paid_at, admins):
    user = await one(conn, "select * from users where id=:uid for update", uid=uid)
    purchase = await one(conn, "select * from orders where id=:id for update", id=oid)
    if not user or not purchase or purchase["user_id"] != uid or purchase["amount"] != amount or currency != "XTR":
        raise ValueError("Платёж не соответствует счёту")
    existing = await one(conn, "select * from payments where charge=:charge", charge=charge)
    if existing:
        if existing["order_id"] != oid:
            raise ValueError("Платёж привязан к другому заказу")
        return False
    if purchase["status"] != "awaiting_payment" or purchase["checkout_at"] is None:
        raise ValueError("Заказ не ожидает оплату")
    await query(conn, """
        insert into payments(charge,order_id,amount,currency,paid_at) values(:charge,:id,:amount,'XTR',:paid)
    """, charge=charge, id=oid, amount=amount, paid=paid_at)
    await query(conn, "update orders set status='paid' where id=:id", id=oid)
    if user["erased"]:
        await notify_admins(conn, admins, f"erased-payment:{oid}",
                            text=f"Платёж после удаления профиля. Проверьте возврат заказа {oid}.")
        return True
    await grant(conn, uid, purchase["duration_seconds"], "paid", order=oid)
    await query(conn, "update orders set status='activated' where id=:id", id=oid)
    days = purchase["duration_seconds"] // DAY
    await enqueue(conn, uid, f"payment:{oid}", text=f"GOAT PRO активирован на {days} дней.")
    totals = await sales(conn, 1)
    await notify_admins(conn, admins, f"sale:{oid}", text=(
        f"💎 НОВАЯ ПРОДАЖА\n👤 {user['username'] or user['name']}\n🆔 {uid}\n"
        f"GOAT PRO · {days} дней\nОплачено {amount} Stars\n"
        f"📊 Продаж сегодня {totals['sales']}\n💵 Выручка сегодня {totals['net']} Stars"))
    return True


async def record_refund(conn, charge, uid, actor=None):
    await query(conn, "select id from users where id=:uid for update", uid=uid)
    purchase = await one(conn, """
        select o.id,o.user_id from payments p join orders o on o.id=p.order_id
        where p.charge=:charge and o.user_id=:uid for update of o
    """, charge=charge, uid=uid)
    if not purchase:
        raise ValueError("Платёж не найден")
    if await scalar(conn, """
        insert into refunds(charge) values(:charge) on conflict do nothing returning charge
    """, charge=charge) is None:
        return False
    row = await one(conn, "select * from access_grants where order_id=:id and revoked_at is null",
                    id=purchase["id"])
    if row:
        await revoke_grant(conn, row)
    await query(conn, "update orders set status='refunded' where id=:id", id=purchase["id"])
    if actor:
        await audit(conn, actor, "refund", purchase["id"])
    await enqueue(conn, uid, f"refund:{purchase['id']}", text="Возврат Stars зарегистрирован.")
    return True


async def sales(conn, days):
    start = "date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'"
    return await one(conn, f"""
        with start as (select case when :days=0 then '-infinity'::timestamptz
            else ({start})-make_interval(days=>:days-1) end as since),
        history as (
          select p.*,o.user_id,row_number() over(partition by o.user_id order by p.paid_at,p.charge) as sequence
          from payments p join orders o on o.id=p.order_id
        ), totals as (
          select count(*) sales,coalesce(sum(amount),0) gross,coalesce(round(avg(amount),2),0) average,
            count(distinct user_id) buyers,count(*) filter(where sequence=1) first_purchases,
            count(*) filter(where sequence>1) renewals from history,start where paid_at>=since
        ), returned as (
          select coalesce(sum(p.amount),0) refunds,count(*) refund_count from refunds r
          join payments p using(charge) cross join start where r.refunded_at>=since
        )
        select totals.*,returned.*,gross-refunds as net,
          (select count(*) from orders where status='pending_approval' and expires_at>now()) pending
        from totals cross join returned
    """, days=days)


async def statistics(conn, days):
    result = await sales(conn, days)
    result.update(await one(conn, """
        with active as (
          select g.user_id,bool_or(g.kind='paid') paid
          from access_grants g join users u on g.user_id=u.id join subscriptions s on s.user_id=u.id
          where not u.blocked and not u.erased and s.frozen_at is null and g.revoked_at is null
            and g.starts_at<=now() and (g.ends_at is null or g.ends_at>now()) group by g.user_id
        ), utc as (select date_trunc('day',now() at time zone 'UTC') at time zone 'UTC' today)
        select count(*) users,count(*) filter(where created_at>=today) new_today,
          count(*) filter(where created_at>=today-interval '6 days') new_week,
          count(*) filter(where created_at>=today-interval '29 days') new_month,
          count(*) filter(where seen_at>=today) active_today,
          count(*) filter(where seen_at>=today-interval '6 days') active_week,
          count(*) filter(where blocked) blocked,count(a.user_id) subscribed,
          count(*) filter(where a.paid) paid_subscribers,count(*) filter(where a.paid=false) free_subscribers,
          count(*) filter(where a.user_id is null and exists(select 1 from access_grants g
            where g.user_id=u.id and g.revoked_at is null and g.ends_at<=now())
            and not exists(select 1 from access_grants g where g.user_id=u.id
            and g.revoked_at is null and (g.ends_at is null or g.ends_at>now()))) expired,
          (select count(*) from access_grants where kind<>'paid') free_grants,
          round(100.0*count(*) filter(where exists(select 1 from orders o join payments p
            on p.order_id=o.id where o.user_id=u.id))/nullif(count(*),0),2) conversion
        from users u left join active a on a.user_id=u.id cross join utc where not u.erased and u.registered
    """))
    return result


async def redeem(conn, uid, code):
    await query(conn, "select id from users where id=:uid and not blocked and not erased for update", uid=uid)
    if not await scalar(conn, "select id from users where id=:uid and not blocked and not erased", uid=uid):
        raise ValueError("Активация недоступна")
    promo = await promo_for_update(conn, uid, code)
    if promo["kind"] != "days":
        raise ValueError("Код скидки применяется командой /buy КОД")
    access = await grant(conn, uid, promo["value"] * DAY, "promo", actor=promo["actor"], reason=promo["code"])
    await query(conn, "insert into promo_redemptions(code,user_id) values(:code,:uid)", code=promo["code"], uid=uid)
    return access


async def history(conn, uid):
    return await rows(conn, """
        select o.id,o.description,o.amount,o.status,o.created_at,p.paid_at
        from orders o left join payments p on p.order_id=o.id where o.user_id=:uid
        order by o.created_at desc limit 100
    """, uid=uid)

