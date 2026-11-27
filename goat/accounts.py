from datetime import UTC, datetime
import hashlib
import hmac
import json
import re
from urllib.parse import parse_qsl

from goat.db import one, query, scalar


def validate_init_data(raw: str, token: str, now: int, max_age: int = 3600):
    if not token or len(raw) > 12000:
        raise ValueError("Авторизация недоступна")
    pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=24)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("Повторяющиеся поля")
    signature = values.pop("hash", "")
    body = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
    expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", signature) or not hmac.compare_digest(signature, expected):
        raise ValueError("Неверная подпись")
    if not -30 <= now - int(values.get("auth_date", "0")) <= max_age:
        raise ValueError("Сессия истекла. Откройте терминал заново")
    user = json.loads(values.get("user", "{}"))
    if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**52:
        raise ValueError("Пользователь отсутствует")
    user["signed_at"] = int(values["auth_date"])
    return user


async def register(conn, uid, username, name, photo=None, source=None, bot=False):
    if source and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source):
        await query(conn, "insert into referral_sources(code) values(:code) on conflict do nothing", code=source)
    else:
        source = None
    if username:
        await query(conn, "update users set username=null where lower(username)=lower(:name) and id<>:id",
                    name=username, id=uid)
    return await one(conn, """
        insert into users(id,username,name,photo,source) values(:id,:username,:name,:photo,:source)
        on conflict(id) do update set username=excluded.username,name=excluded.name,
        photo=coalesce(excluded.photo,users.photo),seen_at=now(),erased=false,registered=true,
        created_at=case when users.registered then users.created_at else now() end,
        unreachable=case when :bot then false else users.unreachable end returning *
    """, id=uid, username=username, name=name, photo=photo, source=source, bot=bot)


async def audit(conn, actor, action, target=None, **details):
    await query(conn, """
        insert into admin_audit_logs(actor,action,target,details) values(:a,:action,:target,cast(:d as jsonb))
    """, a=actor, action=action, target=str(target) if target is not None else None, d=json.dumps(details))


async def profile(conn, uid):
    return await one(conn, """
        select u.id,u.username,u.name,u.photo,u.created_at,u.blocked,s.frozen_at,
        coalesce(x.active,false) and not u.blocked and s.frozen_at is null as active,
        coalesce(x.lifetime,false) as lifetime,x.starts_at,x.ends_at,
        coalesce(p.prefs,'{}') as prefs,
        (select count(*) from orders where user_id=u.id and status='activated') as purchases
        from users u left join subscriptions s on s.user_id=u.id
        left join user_settings p on p.user_id=u.id
        left join lateral (
          select bool_or((g.ends_at is null or g.ends_at>now()) and g.starts_at<=now()) active,
          bool_or(g.ends_at is null and g.starts_at<=now()) lifetime,
          min(g.starts_at) starts_at,max(g.ends_at) ends_at
          from access_grants g where g.user_id=u.id and g.revoked_at is null
        ) x on true where u.id=:uid and not u.erased
    """, uid=uid)


async def grant(conn, uid, seconds, kind, *, actor=None, reason=None, order=None):
    user = await scalar(conn, "select id from users where id=:uid and not erased for update", uid=uid)
    if user is None:
        raise ValueError("Пользователь не найден")
    await query(conn, "insert into subscriptions(user_id) values(:uid) on conflict do nothing", uid=uid)
    anchor = await scalar(conn, "select coalesce(frozen_at, now()) from subscriptions where user_id=:uid", uid=uid)
    return await one(conn, """
        with term as (
          select greatest(:anchor,coalesce(max(ends_at),:anchor)) as start
          from access_grants where user_id=:uid and revoked_at is null and ends_at is not null
        )
        insert into access_grants(user_id,kind,order_id,actor,reason,starts_at,ends_at)
        select :uid,:kind,:order,:actor,:reason,case when cast(:seconds as double precision) is null then :anchor else start end,
          start + make_interval(secs=>cast(:seconds as double precision)) from term returning *
    """, uid=uid, anchor=anchor, seconds=seconds, kind=kind, actor=actor, reason=reason, order=order)


async def revoke_grant(conn, row):
    await query(conn, "update access_grants set revoked_at=now() where id=:id", id=row["id"])
    if row["ends_at"] is not None:
        await query(conn, """
            with period as (
              select greatest(g.starts_at,coalesce(s.frozen_at,now())) as start,g.ends_at
              from access_grants g join subscriptions s using(user_id) where g.id=:id
            )
            update access_grants g set starts_at=g.starts_at-(p.ends_at-p.start),
              ends_at=g.ends_at-(p.ends_at-p.start) from period p
            where g.user_id=:uid and g.revoked_at is null and g.starts_at>=p.ends_at
              and p.ends_at>p.start
        """, uid=row["user_id"], id=row["id"])


async def manage_access(conn, uid, action, actor):
    if await scalar(conn, "select id from users where id=:uid and not erased for update", uid=uid) is None:
        raise ValueError("Пользователь не найден")
    if action in {"block", "unblock"}:
        await query(conn, "update users set blocked=:blocked where id=:uid", uid=uid, blocked=action == "block")
    elif action == "revoke":
        await query(conn, "update access_grants set revoked_at=now() where user_id=:uid and revoked_at is null", uid=uid)
    elif action == "freeze":
        await query(conn, """
            insert into subscriptions(user_id,frozen_at) values(:uid,now())
            on conflict(user_id) do update set frozen_at=coalesce(subscriptions.frozen_at,now())
        """, uid=uid)
    elif action == "unfreeze":
        await query(conn, """
            update access_grants g set starts_at=starts_at+case when starts_at>=s.frozen_at
                then now()-s.frozen_at else interval '0' end,ends_at=ends_at+(now()-s.frozen_at)
            from subscriptions s where s.user_id=:uid and g.user_id=s.user_id and s.frozen_at is not null
              and g.revoked_at is null and (ends_at is null or ends_at>s.frozen_at)
        """, uid=uid)
        await query(conn, "update subscriptions set frozen_at=null where user_id=:uid", uid=uid)
    else:
        raise ValueError("Неизвестное действие")
    await audit(conn, actor, action, uid)


async def enqueue(conn, uid, dedupe, **payload):
    return await scalar(conn, """
        insert into notification_deliveries(user_id,dedupe,payload) values(:uid,:key,cast(:payload as jsonb))
        on conflict(user_id,dedupe) do nothing returning id
    """, uid=uid, key=dedupe, payload=json.dumps(payload, ensure_ascii=False, default=str))


async def notify_admins(conn, admins, key, **payload):
    for uid in admins:
        await query(conn, "insert into users(id,name,registered) values(:uid,'Администратор',false) on conflict do nothing", uid=uid)
        await enqueue(conn, uid, key, **payload)


async def erase(conn, uid):
    await query(conn, "select id from users where id=:uid for update", uid=uid)
    for statement in (
        "delete from portfolios where user_id=:uid",
        "delete from user_settings where user_id=:uid",
        "delete from support_tickets where user_id=:uid",
        "delete from notification_deliveries where user_id=:uid",
        "delete from rate_limits where identity=cast(cast(:uid as bigint) as text)",
        "delete from system_settings where key='ticket:'||cast(cast(:uid as bigint) as text)",
        "update orders set status='expired' where user_id=:uid and status in ('pending_approval','approved','awaiting_payment') and checkout_at is null",
        "update access_grants set revoked_at=now() where user_id=:uid and revoked_at is null",
        "update users set username=null,photo=null,name='Удалённый профиль',source=null,erased=true,erased_at=now() where id=:uid",
    ):
        await query(conn, statement, uid=uid)
