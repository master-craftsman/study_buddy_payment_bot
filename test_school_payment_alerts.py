import os
import unittest
from datetime import date
from unittest.mock import patch

from school_payment_alerts import (
    actual_subscriptions,
    lesson_configuration_alerts,
    sync_missing_attendances,
)


def title(value):
    return {
        "type": "title",
        "title": [{"plain_text": value, "text": {"content": value}}],
    }


def relation(*page_ids):
    return {"type": "relation", "relation": [{"id": page_id} for page_id in page_ids]}


def select(value):
    return {"type": "select", "select": {"name": value} if value else None}


def notion_date(start, end=None):
    return {"type": "date", "date": {"start": start, "end": end}}


def page(page_id, **properties):
    return {"id": page_id, "properties": properties, "created_time": "2026-01-01"}


class FakeClient:
    def __init__(self, collections):
        self.collections = collections
        self.updated = []
        self.created = []

    def query_database(self, database_id):
        return self.collections[database_id]

    def update_page_properties(self, page_id, properties):
        self.updated.append((page_id, properties))

    def create_page(self, database_id, properties):
        self.created.append((database_id, properties))


class SchoolPaymentAlertsTest(unittest.TestCase):
    env = {
        "STUDENTS_DB_ID": "students",
        "SUBSCRIPTIONS_DB_ID": "subscriptions",
        "LESSONS_DB_ID": "lessons",
        "ATTENDANCE_DB_ID": "attendance",
    }

    def subscription(self, page_id, student_id="student-1", manual_status="", period=None):
        properties = {
            "Название": title(page_id),
            "Ученик": relation(*(student_id,) if student_id else ()),
            "Ручной статус": select(manual_status),
            "Статус": select("Активен"),
        }
        if period:
            properties["Дата начала"] = notion_date(period[0])
            if len(period) > 1 and period[1]:
                properties["Дата окончания"] = notion_date(period[1])
        return page(page_id, **properties)

    def lesson(self, subscription_ids=(), lesson_date="2026-07-15"):
        return page(
            "lesson-1",
            **{
                "Название": title("Урок"),
                "Дата и время": notion_date(lesson_date),
                "Статус урока": select("Запланирован"),
                "Абонементы занятия": relation(*subscription_ids),
            },
        )

    @patch.dict(os.environ, env, clear=False)
    def test_sync_corrects_student_but_does_not_restore_cleared_lesson_relation(self):
        subscription = self.subscription("sub-1")
        lesson = self.lesson(subscription_ids=())
        attendance = page(
            "attendance-1",
            **{
                "Абонемент": relation("sub-1"),
                "Занятие": relation("lesson-1"),
                "Ученик": relation("wrong-student"),
            },
        )
        client = FakeClient(
            {
                "subscriptions": [subscription],
                "lessons": [lesson],
                "attendance": [attendance],
            }
        )

        self.assertEqual(sync_missing_attendances(client), 0)
        self.assertEqual(len(client.created), 0)
        self.assertEqual(client.updated[0][0], "attendance-1")
        self.assertEqual(
            client.updated[0][1]["Ученик"]["relation"], [{"id": "student-1"}]
        )
        self.assertNotIn("lesson-1", [page_id for page_id, _ in client.updated])

    @patch.dict(os.environ, env, clear=False)
    def test_sync_skips_closed_and_out_of_period_subscriptions(self):
        subscriptions = [
            self.subscription("closed", manual_status="Закрыт"),
            self.subscription("june", period=("2026-06-01", "2026-06-30")),
            self.subscription("july", period=("2026-07-01", "2026-07-31")),
        ]
        client = FakeClient(
            {
                "subscriptions": subscriptions,
                "lessons": [self.lesson(subscription_ids=("closed", "june", "july"))],
                "attendance": [],
            }
        )

        self.assertEqual(sync_missing_attendances(client), 1)
        self.assertEqual(len(client.created), 1)
        created_properties = client.created[0][1]
        self.assertEqual(
            created_properties["Абонемент"]["relation"], [{"id": "july"}]
        )
        self.assertEqual(
            created_properties["Ученик"]["relation"], [{"id": "student-1"}]
        )

    @patch.dict(os.environ, env, clear=False)
    def test_single_period_date_is_start_without_artificial_end(self):
        subscription = self.subscription("july", period=("2026-07-01", None))
        client = FakeClient(
            {
                "subscriptions": [subscription],
                "lessons": [self.lesson(subscription_ids=("july",))],
                "attendance": [],
            }
        )

        self.assertEqual(sync_missing_attendances(client), 1)

    def test_debt_manual_status_has_same_priority_as_in_calculation(self):
        debt = self.subscription(
            "debt", manual_status="Долг", period=("2026-06-01", "2026-06-30")
        )
        newer = self.subscription("newer", period=("2026-07-01", "2026-07-31"))

        selected = actual_subscriptions([debt, newer], date(2026, 7, 15))

        self.assertEqual([subscription["id"] for subscription in selected], ["debt"])

    @patch.dict(os.environ, env, clear=False)
    def test_invalid_lesson_subscription_produces_alert(self):
        closed = self.subscription("closed", manual_status="Закрыт")
        client = FakeClient(
            {
                "subscriptions": [closed],
                "lessons": [self.lesson(subscription_ids=("closed",))],
            }
        )

        alerts = lesson_configuration_alerts(client, date(2026, 7, 15))

        self.assertEqual(len(alerts), 1)
        self.assertIn("Ошибка состава занятия", alerts[0])
        self.assertIn("абонемент закрыт вручную", alerts[0])


if __name__ == "__main__":
    unittest.main()
