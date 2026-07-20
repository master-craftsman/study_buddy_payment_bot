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
P_SUB_MANUAL_STATUS = "Ручной статус"
P_SUB_PAID = "Оплачено занятий"
P_SUB_PAYMENT_DATE = "Дата оплаты"
P_SUB_PERIOD = "Дата начала"
P_SUB_END = "Дата окончания"
P_SUB_STUDENT = "Ученик"

P_ATT_SUBSCRIPTION = "Абонемент"
P_ATT_LESSON = "Занятие"
P_ATT_NAME = "Название"
P_ATT_STUDENT = "Ученик"
P_ATT_STATUS = "Статус участия"
P_ATT_DATE = "Дата урока"

P_LESSON_NAME = "Название"
P_LESSON_DATE = "Дата и время"
P_LESSON_TEACHER = "Преподаватель"
P_LESSON_ROOM = "Кабинет"
P_LESSON_STATUS = "Статус урока"
P_LESSON_SUBSCRIPTIONS = "Абонементы занятия"

P_TG_CHAT_NAME = "Название"
P_TG_CHAT_TARGET = "Chat ID или @username"
P_TG_CHAT_THREAD_ID = "Thread ID"
P_TG_CHAT_ACTIVE = "Активен?"

ACTIVE_SUB_STATUSES = {"Активен", "Заканчивается", "Долг", "Исчерпан", ""}
CLOSED_MANUAL_SUB_STATUSES = {"Закрыт"}
INCLUDED_MANUAL_SUB_STATUSES = {"В расчете", "Долг"}
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


def first_title_value(page, names):
    for name in names:
        value = title_value(page, name)
        if value:
            return value
    return ""


def rich_text_value(page, name):
    p = prop(page, name)
    if p.get("type") == "rich_text":
        return "".join(part.get("plain_text", "") for part in p.get("rich_text", []))
    return ""


def checkbox_value(page, name):
    p = prop(page, name)
    if p.get("type") == "checkbox":
        return bool(p.get("checkbox"))
    return False


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


def subscription_period(page):
    return date_value(page, P_SUB_PERIOD), date_value(page, P_SUB_END)


def subscription_is_open(page):
    manual_status = select_value(page, P_SUB_MANUAL_STATUS)
    if manual_status in CLOSED_MANUAL_SUB_STATUSES:
        return False
    if manual_status in INCLUDED_MANUAL_SUB_STATUSES:
        return True
    return select_value(page, P_SUB_STATUS) in ACTIVE_SUB_STATUSES


def subscription_sort_key(page, today):
    manually_included = (
        select_value(page, P_SUB_MANUAL_STATUS) in INCLUDED_MANUAL_SUB_STATUSES
    )
    period_start, period_end = subscription_period(page)
    end_date = date_value(page, P_SUB_END)
    payment_date = date_value(page, P_SUB_PAYMENT_DATE)
    contains_today = bool(
        period_start
        and period_start <= today
        and (period_end is None or today <= period_end)
    )
    latest_date = period_end or period_start or end_date or payment_date or date.min
    return manually_included, contains_today, latest_date, page.get("created_time", "")


def actual_subscriptions(subscriptions, today):
    by_student = defaultdict(list)
    without_student = []

    for subscription in subscriptions:
        if not subscription_is_open(subscription):
            continue
        student_ids = relation_ids(subscription, P_SUB_STUDENT)
        if not student_ids:
            without_student.append(subscription)
            continue
        for student_id in student_ids:
            by_student[student_id].append(subscription)

    selected = {}
    for student_subscriptions in by_student.values():
        subscription = max(
            student_subscriptions,
            key=lambda item: subscription_sort_key(item, today),
        )
        selected[subscription["id"]] = subscription

    for subscription in without_student:
        selected[subscription["id"]] = subscription

    return list(selected.values())


def subscription_lesson_issue(subscription, lesson_date):
    manual_status = select_value(subscription, P_SUB_MANUAL_STATUS)
    if manual_status in CLOSED_MANUAL_SUB_STATUSES:
        return "абонемент закрыт вручную"

    student_ids = relation_ids(subscription, P_SUB_STUDENT)
    if len(student_ids) != 1:
        return f"в абонементе должно быть ровно одно поле Ученик; сейчас: {len(student_ids)}"

    period_start, period_end = subscription_period(subscription)
    if lesson_date is not None:
        if period_start is not None and lesson_date < period_start:
            return "дата занятия раньше периода абонемента"
        if period_end is not None and lesson_date > period_end:
            return "дата занятия позже периода абонемента"
    return ""


def lesson_count_text(count):
    abs_count = abs(count)
    if abs_count % 10 == 1 and abs_count % 100 != 11:
        word = "занятие"
    elif abs_count % 10 in {2, 3, 4} and abs_count % 100 not in {12, 13, 14}:
        word = "занятия"
    else:
        word = "занятий"
    return f"{count} {word}"


def clean_text_value(value):
    return (value or "").strip()


def has_whitespace(value):
    return any(char.isspace() for char in value)


def send_telegram_message(text, chat_id, message_thread_id=None):
    bot_token = clean_text_value(os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = clean_text_value(chat_id)
    message_thread_id = clean_text_value(message_thread_id)
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and Telegram chat target must be set")
    if has_whitespace(bot_token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN must not contain spaces or line breaks")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id:
        payload["message_thread_id"] = int(message_thread_id)
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


def telegram_recipients(client):
    recipients_db_id = os.getenv("TELEGRAM_RECIPIENTS_DB_ID")
    if not recipients_db_id:
        fallback_chat_id = clean_text_value(os.getenv("TELEGRAM_CHAT_ID"))
        fallback_thread_id = clean_text_value(os.getenv("TELEGRAM_MESSAGE_THREAD_ID"))
        return [("TELEGRAM_CHAT_ID", fallback_chat_id, fallback_thread_id)] if fallback_chat_id else []

    recipients = []
    for page in client.query_database(recipients_db_id):
        if not checkbox_value(page, P_TG_CHAT_ACTIVE):
            continue
        target = clean_text_value(rich_text_value(page, P_TG_CHAT_TARGET))
        if not target:
            continue
        name = first_title_value(page, (P_TG_CHAT_NAME, "Name")) or target
        thread_id = clean_text_value(rich_text_value(page, P_TG_CHAT_THREAD_ID))
        recipients.append((name, target, thread_id))
    return recipients


def sync_missing_attendances(client):
    lessons_db_id = os.getenv("LESSONS_DB_ID")
    attendance_db_id = os.getenv("ATTENDANCE_DB_ID")
    subscriptions_db_id = os.getenv("SUBSCRIPTIONS_DB_ID")
    if not lessons_db_id or not attendance_db_id or not subscriptions_db_id:
        return 0

    lessons = client.query_database(lessons_db_id)
    attendances = client.query_database(attendance_db_id)
    subscriptions = client.query_database(subscriptions_db_id)
    subscriptions_by_id = {subscription["id"]: subscription for subscription in subscriptions}
    student_by_subscription = {}
    for subscription in subscriptions:
        student_ids = relation_ids(subscription, P_SUB_STUDENT)
        if len(student_ids) == 1:
            student_by_subscription[subscription["id"]] = student_ids[0]

    existing_pairs = set()
    for attendance in attendances:
        subscription_ids = relation_ids(attendance, P_ATT_SUBSCRIPTION)
        lesson_ids = relation_ids(attendance, P_ATT_LESSON)
        if len(subscription_ids) == 1 and len(lesson_ids) == 1:
            existing_pairs.add((lesson_ids[0], subscription_ids[0]))
            student_id = student_by_subscription.get(subscription_ids[0])
            if student_id and relation_ids(attendance, P_ATT_STUDENT) != [student_id]:
                client.update_page_properties(
                    attendance["id"],
                    {P_ATT_STUDENT: {"relation": [{"id": student_id}]}},
                )

    created = 0
    for lesson in lessons:
        lesson_subscription_ids = relation_ids(lesson, P_LESSON_SUBSCRIPTIONS)
        if select_value(lesson, P_LESSON_STATUS) not in OPEN_LESSON_STATUSES:
            continue
        lesson_name = title_value(lesson, P_LESSON_NAME) or "Участие"
        lesson_date = date_value(lesson, P_LESSON_DATE)
        for subscription_id in lesson_subscription_ids:
            subscription = subscriptions_by_id.get(subscription_id)
            if subscription is None or subscription_lesson_issue(subscription, lesson_date):
                continue
            pair = (lesson["id"], subscription_id)
            if pair in existing_pairs:
                continue
            properties = {
                    P_ATT_NAME: {
                        "title": [
                            {"type": "text", "text": {"content": lesson_name}}
                        ]
                    },
                    P_ATT_STATUS: {"select": {"name": "Запланировано"}},
                    P_ATT_SUBSCRIPTION: {
                        "relation": [{"id": subscription_id}]
                    },
                    P_ATT_LESSON: {"relation": [{"id": lesson["id"]}]},
                }
            student_id = student_by_subscription.get(subscription_id)
            if student_id:
                properties[P_ATT_STUDENT] = {"relation": [{"id": student_id}]}
            client.create_page(attendance_db_id, properties)
            existing_pairs.add(pair)
            created += 1
    return created


def lesson_configuration_alerts(client, today):
    lessons_db_id = os.getenv("LESSONS_DB_ID")
    subscriptions_db_id = os.getenv("SUBSCRIPTIONS_DB_ID")
    if not lessons_db_id or not subscriptions_db_id:
        return []

    lessons = client.query_database(lessons_db_id)
    subscriptions = {
        subscription["id"]: subscription
        for subscription in client.query_database(subscriptions_db_id)
    }
    issues = defaultdict(list)
    for lesson in lessons:
        if select_value(lesson, P_LESSON_STATUS) not in OPEN_LESSON_STATUSES:
            continue
        lesson_name = title_value(lesson, P_LESSON_NAME) or lesson.get(
            "url", "Занятие"
        )
        lesson_date = date_value(lesson, P_LESSON_DATE)
        if lesson_date is not None and lesson_date < today:
            continue
        for subscription_id in relation_ids(lesson, P_LESSON_SUBSCRIPTIONS):
            subscription = subscriptions.get(subscription_id)
            if subscription is None:
                issue = "абонемент не найден или недоступен интеграции"
                subscription_name = subscription_id
            else:
                issue = subscription_lesson_issue(subscription, lesson_date)
                subscription_name = title_value(subscription, P_SUB_NAME) or subscription.get(
                    "url", "Абонемент"
                )
            if not issue:
                continue
            date_text = lesson_date.isoformat() if lesson_date else "не указана"
            issues[(subscription_name, issue)].append(
                f"{html.escape(lesson_name)} ({date_text})"
            )
    alerts = []
    for (subscription_name, issue), lessons_with_dates in issues.items():
        alerts.append(
            "\n".join(
                [
                    "🚨 <b>Ошибка состава занятия</b>",
                    f"Абонемент: {html.escape(subscription_name)}",
                    f"Причина: {html.escape(issue)}.",
                    f"Будущих занятий: {len(lessons_with_dates)}",
                    "Занятия: " + "; ".join(lessons_with_dates),
                ]
            )
        )
    return alerts


def payment_alerts(client, today):
    subscriptions = client.query_database(os.environ["SUBSCRIPTIONS_DB_ID"])
    attendances = client.query_database(os.environ["ATTENDANCE_DB_ID"])
    low_remaining = int(os.getenv("LOW_REMAINING_THRESHOLD", "2"))
    days_before_end = int(os.getenv("DAYS_BEFORE_END_ALERT", "5"))

    attendance_by_subscription = defaultdict(list)
    data_quality_alerts = []
    for att in attendances:
        subscription_ids = relation_ids(att, P_ATT_SUBSCRIPTION)
        lesson_ids = relation_ids(att, P_ATT_LESSON)
        attendance_name = title_value(att, P_ATT_NAME) or att.get("url", "Участие")
        if len(subscription_ids) != 1 or len(lesson_ids) != 1:
            data_quality_alerts.append(
                "\n".join(
                    [
                        "🚨 <b>Ошибка в посещаемости</b>",
                        f"Участие: {html.escape(attendance_name)}",
                        "Нужно выбрать ровно один абонемент и одно занятие.",
                        f"Абонементов: {len(subscription_ids)}; занятий: {len(lesson_ids)}",
                    ]
                )
            )
            continue
        attendance_by_subscription[subscription_ids[0]].append(att)

    alerts = []
    for sub in actual_subscriptions(subscriptions, today):
        sub_id = sub["id"]
        sub_name = title_value(sub, P_SUB_NAME) or sub.get("url", "Абонемент")
        paid_lessons = int(number_value(sub, P_SUB_PAID))
        end_date = date_value(sub, P_SUB_END)
        related_attendances = attendance_by_subscription.get(sub_id, [])
        if paid_lessons <= 0 and not related_attendances:
            continue

        charged_count = 0
        planned_future_count = 0
        for att in related_attendances:
            att_status = select_value(att, P_ATT_STATUS)
            att_date = date_value(att, P_ATT_DATE)
            if att_status in CHARGED_ATTENDANCE_STATUSES:
                charged_count += 1
            if (
                (att_status in PLANNED_ATTENDANCE_STATUSES or not att_status)
                and att_date is not None
                and att_date >= today
            ):
                planned_future_count += 1
            if (
                (not att_status or att_status in PLANNED_ATTENDANCE_STATUSES)
                and att_date is not None
                and att_date < today
            ):
                attendance_name = title_value(att, P_ATT_NAME) or att.get(
                    "url", "Участие"
                )
                data_quality_alerts.append(
                    "\n".join(
                        [
                            "⚠️ <b>Не отмечена посещаемость</b>",
                            f"Участие: {html.escape(attendance_name)}",
                            f"Дата: {att_date.isoformat()}",
                            "Выберите статус: Был, Сгорело, Отмена без списания или Перенос.",
                        ]
                    )
                )

        remaining_fact = paid_lessons - charged_count
        remaining_after_schedule = remaining_fact - planned_future_count

        reasons = []
        notes = []
        level = "yellow"
        if remaining_fact <= low_remaining:
            reasons.append(f"осталось {lesson_count_text(remaining_fact)}")
        if remaining_fact == 1:
            notes.append(
                "Напишите преподавателю: на последнем уроке нужно сделать "
                "финальный тест, проревьюить все успехи и оценить качество "
                "усвоения материала учеником."
            )
        if remaining_fact == 0:
            notes.append(
                "Напишите преподавателю, что нужно выслать отчет по ученику. "
                "Напишите клиенту и спросите, будут ли они продлевать абонемент."
            )
        if remaining_fact <= low_remaining and remaining_after_schedule == 0:
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

        icon = {"yellow": "⚠️", "orange": "🟠", "red": "🚨"}[level]
        end_text = end_date.isoformat() if end_date else "не указана"
        alert_lines = [
            f"{icon} <b>Имя абонемента:</b> {html.escape(sub_name)}",
            f"Причина: {html.escape('; '.join(reasons))}",
            f"Оплачено: {paid_lessons}",
            f"Списано: {charged_count}",
            f"Запланировано вперед: {planned_future_count}",
            f"Остаток факт: {remaining_fact}",
            f"Остаток после расписания: {remaining_after_schedule}",
            f"Дата окончания: {html.escape(end_text)}",
        ]
        if notes:
            alert_lines.append("Notes:")
            alert_lines.extend(html.escape(note) for note in notes)
        alerts.append(
            "\n".join(alert_lines)
        )
    return data_quality_alerts + alerts


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
    created_attendances = 0
    if os.getenv("TELEGRAM_DRY_RUN") != "1":
        created_attendances = sync_missing_attendances(client)
    alerts = payment_alerts(client, today)
    alerts.extend(lesson_configuration_alerts(client, today))
    alerts.extend(conflict_alerts(client))

    if alerts:
        message = "🔔 <b>Контроль школы</b>\n\n" + "\n\n".join(alerts)
        recipients = telegram_recipients(client)
        if (
            os.getenv("TELEGRAM_DRY_RUN") == "1"
            or not os.getenv("TELEGRAM_BOT_TOKEN")
            or not recipients
        ):
            print("Telegram credentials are missing; previewing message without sending.")
            print(message)
            if recipients:
                print("Recipients:")
                for name, target, thread_id in recipients:
                    thread_text = f" thread={thread_id}" if thread_id else ""
                    print(f"- {name}: {target}{thread_text}")
            print(f"Prepared {len(alerts)} alert(s).")
            return

        for _, chat_id, thread_id in recipients:
            send_telegram_message(message, chat_id, thread_id)
        if created_attendances:
            print(f"Created {created_attendances} attendance row(s).")
        print(f"Sent {len(alerts)} alert(s) to {len(recipients)} chat(s).")
    else:
        if created_attendances:
            print(f"Created {created_attendances} attendance row(s).")
        print("No alerts.")


if __name__ == "__main__":
    main()
