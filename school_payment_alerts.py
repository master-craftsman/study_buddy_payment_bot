import html
import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime

from notion_client import NotionClient, load_env_file


P_SUB_NAME = "Название"
P_SUB_STATUS = "Статус"
P_SUB_PAID = "Оплачено занятий"
P_SUB_END = "Дата окончания"
P_SUB_LAST_ALERT = "TG ключ последнего алерта"

P_ATT_SUBSCRIPTION = "Абонемент"
P_ATT_STATUS = "Статус участия"
P_ATT_DATE = "Дата урока"

P_LESSON_NAME = "Название"
P_LESSON_DATE = "Дата и время"
P_LESSON_TEACHER = "Преподаватель"
P_LESSON_ROOM = "Кабинет"
P_LESSON_STATUS = "Статус урока"

ACTIVE_SUB_STATUSES = {"Активен", "Заканчивается", "Долг", ""}
CHARGED_ATTENDANCE_STATUSES = {"Был", "Сгорело"}
PLANNED_ATTENDANCE_STATUSES = {"Запланировано"}
OPEN_LESSON_STATUSES = {"Запланирован", ""}


def prop(page, name):
    return page.get("properties", {}).get(name, {})


def title_value(page, name):
    p = prop(page, name)
    if p.get("type") == "title":
        return "".join(part.get("plain_text", "") for part in p.get("title", []))
    if p.get("type") == "formula" and p.get("formula", {}).get("type") == "string":
        return p["formula"].get("string") or ""
    return ""


def rich_text_value(page, name):
    p = prop(page, name)
    if p.get("type") == "rich_text":
        return "".join(part.get("plain_text", "") for part in p.get("rich_text", []))
    return ""


def number_value(page, name):
    p = prop(page, name)
    if p.get("type") == "number":
        return p.get("number") or 0
    if p.get("type") == "formula":
        f = p.get("formula", {})
        if f.get("type") == "number":
            return f.get("number") or 0
    if p.get("type") == "rollup":
        r = p.get("rollup", {})
        if r.get("type") == "number":
            return r.get("number") or 0
    return 0


def select_value(page, name):
    p = prop(page, name)
    if p.get("type") == "select" and p.get("select"):
        return p["select"]["name"]
    if p.get("type") == "status" and p.get("status"):
        return p["status"]["name"]
    if p.get("type") == "formula":
        f = p.get("formula", {})
        if f.get("type") == "string":
            return f.get("string") or ""
    return ""


def relation_ids(page, name):
    p = prop(page, name)
    if p.get("type") == "relation":
        return [item["id"] for item in p.get("relation", [])]
    return []


def parse_iso_date(value):
    if not value:
        return None
    if "T" in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.combine(date.fromisoformat(value), datetime.min.time())


def date_range_value(page, name):
    p = prop(page, name)
    if p.get("type") == "date" and p.get("date"):
        data = p["date"]
        start = parse_iso_date(data.get("start"))
        end = parse_iso_date(data.get("end")) or start
        return start, end
    if p.get("type") == "formula":
        f = p.get("formula", {})
        if f.get("type") == "date" and f.get("date"):
            data = f["date"]
            start = parse_iso_date(data.get("start"))
            end = parse_iso_date(data.get("end")) or start
            return start, end
    if p.get("type") == "rollup":
        r = p.get("rollup", {})
        if r.get("type") == "date" and r.get("date"):
            data = r["date"]
            start = parse_iso_date(data.get("start"))
            end = parse_iso_date(data.get("end")) or start
            return start, end
        if r.get("type") == "array":
            for item in r.get("array", []):
                if item.get("type") == "date" and item.get("date"):
                    data = item["date"]
                    start = parse_iso_date(data.get("start"))
                    end = parse_iso_date(data.get("end")) or start
                    return start, end
    return None, None


def date_value(page, name):
    start, _ = date_range_value(page, name)
    return start.date() if start else None


def send_telegram_message(text):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    request = urllib.request.Request(
        url=f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API {exc.code}: {body}") from exc


def update_alert_key(client, page_id, value):
    client.update_page_properties(
        page_id,
        {
            P_SUB_LAST_ALERT: {
                "rich_text": [{"type": "text", "text": {"content": value}}]
            }
        },
    )


def payment_alerts(client, today):
    subscriptions = client.query_database(os.environ["SUBSCRIPTIONS_DB_ID"])
    attendances = client.query_database(os.environ["ATTENDANCE_DB_ID"])
    low_remaining = int(os.getenv("LOW_REMAINING_THRESHOLD", "2"))
    days_before_end = int(os.getenv("DAYS_BEFORE_END_ALERT", "5"))

    attendance_by_subscription = defaultdict(list)
    for att in attendances:
        for sub_id in relation_ids(att, P_ATT_SUBSCRIPTION):
            attendance_by_subscription[sub_id].append(att)

    alerts = []
    alert_updates = []
    for sub in subscriptions:
        sub_id = sub["id"]
        sub_name = title_value(sub, P_SUB_NAME) or sub.get("url", "Абонемент")
        sub_status = select_value(sub, P_SUB_STATUS)
        if sub_status not in ACTIVE_SUB_STATUSES:
            continue

        paid_lessons = int(number_value(sub, P_SUB_PAID))
        end_date = date_value(sub, P_SUB_END)
        previous_alert_key = rich_text_value(sub, P_SUB_LAST_ALERT)
        related_attendances = attendance_by_subscription.get(sub_id, [])

        charged_count = 0
        planned_future_count = 0
        for att in related_attendances:
            att_status = select_value(att, P_ATT_STATUS)
            att_date = date_value(att, P_ATT_DATE)
            if att_status in CHARGED_ATTENDANCE_STATUSES:
                charged_count += 1
            if (
                att_status in PLANNED_ATTENDANCE_STATUSES
                and att_date is not None
                and att_date >= today
            ):
                planned_future_count += 1

        remaining_fact = paid_lessons - charged_count
        remaining_after_schedule = remaining_fact - planned_future_count

        reasons = []
        level = "yellow"
        if remaining_fact <= low_remaining:
            reasons.append(f"осталось {remaining_fact} занятий")
        if remaining_after_schedule == 0:
            reasons.append("все оплаченные занятия уже расписаны")
            level = "orange"
        if remaining_after_schedule < 0:
            reasons.append(
                f"расписано на {-remaining_after_schedule} занятий больше, чем оплачено"
            )
            level = "red"
        if end_date is not None:
            days_left = (end_date - today).days
            if 0 <= days_left <= days_before_end:
                reasons.append(f"абонемент заканчивается через {days_left} дн.")
                if level == "yellow":
                    level = "orange"

        if not reasons:
            continue

        alert_key = (
            f"level={level};remaining={remaining_fact};"
            f"after_schedule={remaining_after_schedule};charged={charged_count};"
            f"planned={planned_future_count};end={end_date}"
        )
        if alert_key == previous_alert_key:
            continue

        icon = {"yellow": "⚠️", "orange": "🟠", "red": "🚨"}[level]
        end_text = end_date.isoformat() if end_date else "не указана"
        alerts.append(
            "\n".join(
                [
                    f"{icon} <b>{html.escape(sub_name)}</b>",
                    f"Причина: {html.escape('; '.join(reasons))}",
                    f"Оплачено: {paid_lessons}",
                    f"Списано: {charged_count}",
                    f"Запланировано вперед: {planned_future_count}",
                    f"Остаток факт: {remaining_fact}",
                    f"Остаток после расписания: {remaining_after_schedule}",
                    f"Дата окончания: {html.escape(end_text)}",
                ]
            )
        )
        alert_updates.append((sub_id, alert_key))
    return alerts, alert_updates


def ranges_overlap(first_start, first_end, second_start, second_end):
    if not first_start or not first_end or not second_start or not second_end:
        return False
    return first_start < second_end and second_start < first_end


def conflict_alerts(client):
    lessons_db_id = os.getenv("LESSONS_DB_ID")
    if not lessons_db_id:
        return []

    lessons = client.query_database(lessons_db_id)
    buckets = defaultdict(list)
    for lesson in lessons:
        if select_value(lesson, P_LESSON_STATUS) not in OPEN_LESSON_STATUSES:
            continue
        start, end = date_range_value(lesson, P_LESSON_DATE)
        if not start or not end:
            continue
        for teacher_id in relation_ids(lesson, P_LESSON_TEACHER):
            buckets[("teacher", teacher_id)].append((start, end, lesson))
        for room_id in relation_ids(lesson, P_LESSON_ROOM):
            buckets[("room", room_id)].append((start, end, lesson))

    alerts = []
    for (kind, _), items in buckets.items():
        items.sort(key=lambda item: item[0])
        for previous, current in zip(items, items[1:]):
            if not ranges_overlap(previous[0], previous[1], current[0], current[1]):
                continue
            kind_text = "преподавателя" if kind == "teacher" else "кабинета"
            previous_name = title_value(previous[2], P_LESSON_NAME) or previous[2].get("url", "")
            current_name = title_value(current[2], P_LESSON_NAME) or current[2].get("url", "")
            alerts.append(
                "\n".join(
                    [
                        f"🚨 <b>Конфликт {kind_text}</b>",
                        html.escape(previous_name),
                        html.escape(current_name),
                        f"{previous[0].isoformat()} - {previous[1].isoformat()}",
                        f"{current[0].isoformat()} - {current[1].isoformat()}",
                    ]
                )
            )
    return alerts


def main():
    load_env_file()
    client = NotionClient()
    today = date.today()
    alerts, alert_updates = payment_alerts(client, today)
    alerts.extend(conflict_alerts(client))

    if alerts:
        message = "🔔 <b>Контроль школы</b>\n\n" + "\n\n".join(alerts)
        if (
            os.getenv("TELEGRAM_DRY_RUN") == "1"
            or not os.getenv("TELEGRAM_BOT_TOKEN")
            or not os.getenv("TELEGRAM_CHAT_ID")
        ):
            print("Telegram credentials are missing; previewing message without sending.")
            print(message)
            print(f"Prepared {len(alerts)} alert(s).")
            return

        send_telegram_message(message)
        for page_id, alert_key in alert_updates:
            update_alert_key(client, page_id, alert_key)
        print(f"Sent {len(alerts)} alert(s).")
    else:
        print("No alerts.")


if __name__ == "__main__":
    main()
