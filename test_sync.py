import unittest
from types import SimpleNamespace
from unittest.mock import patch

from main import build_indexes, collect_leads_customers
from models import AbstractCustomer
from abstract_customer import (
    dict_compare_merge,
    create_or_update_customer,
    create_or_update_lead,
)

def make_entity(entity_id, name, login):
    """Создаёт тестового покупателя или сделку."""
    return SimpleNamespace(
        id=entity_id,
        name=name,
        custom_fields_values={"Логин": {"value": login}},
        as_dict=lambda: {"id": entity_id},
    )

class TestSync(unittest.TestCase):

    def test_search_by_id(self):
        """Проверяем поиск покупателя по CRM ID."""
        row = AbstractCustomer(
            amo_customers=[
                {"general": {"id": 123, "login": "user1"}}
            ]
        )

        customers, _, _, _ = build_indexes([row])

        self.assertIs(customers[123], row)

    def test_search_by_login(self):
        """Проверяем поиск сделки по логину."""
        row = AbstractCustomer(
            amo_leads=[
                {"general": {"id": 456, "login": "user1"}}
            ]
        )

        _, _, _, leads = build_indexes([row])

        self.assertIn(row, leads["user1"])

    def test_duplicate_login(self):
        """Неоднозначный логин содержит две записи."""
        rows = [
            AbstractCustomer(
                amo_customers=[
                    {"general": {"id": 1, "login": "user1"}}
                ]
            ),
            AbstractCustomer(
                amo_customers=[
                    {"general": {"id": 2, "login": "user1"}}
                ]
            )
        ]

        _, _, logins, _ = build_indexes(rows)

        self.assertEqual(len(logins["user1"]), 2)

    def test_merge(self):
        """При объединении не теряются разные покупатели."""
        first = [{"general": {"id": 1}}]
        second = [{"general": {"id": 2}}]

        result = dict_compare_merge(first, second)

        self.assertEqual(len(result), 2)


    def test_create_customer(self):
        """Новый покупатель добавляется в сессию."""
        with patch("abstract_customer.db") as mock_db:
            customer = make_entity(1, "Иван", "ivan")
            row = create_or_update_customer(None, customer)

            mock_db.session.add.assert_called_once_with(row)
            self.assertEqual(row.amo_customers[0]["general"]["id"], 1)

    def test_update_customer(self):
        """Повторная загрузка обновляет покупателя."""
        customer = make_entity(1, "Иван новый", "ivan")
        row = AbstractCustomer(
            amo_customers=[{"general": {"id": 1, "name": "Иван"}}]
        )

        create_or_update_customer(row, customer)

        self.assertEqual(len(row.amo_customers), 1)
        self.assertEqual(row.amo_customers[0]["general"]["name"], "Иван новый")

    def test_create_lead(self):
        """Новая сделка добавляется в сессию."""
        with patch("abstract_customer.db") as mock_db:
            lead = make_entity(10, "Сделка", "ivan")
            row = create_or_update_lead(None, lead)

            mock_db.session.add.assert_called_once_with(row)
            self.assertEqual(row.amo_leads[0]["general"]["id"], 10)

    def test_update_lead(self):
        """Повторная загрузка обновляет сделку."""
        lead = make_entity(10, "Новая сделка", "ivan")
        row = AbstractCustomer(
            amo_leads=[{"general": {"id": 10, "name": "Сделка"}}]
        )

        create_or_update_lead(row, lead)

        self.assertEqual(len(row.amo_leads), 1)
        self.assertEqual(row.amo_leads[0]["general"]["name"], "Новая сделка")

    def test_rollback_on_error(self):
        """При ошибке CRM данные не удаляются."""
        with patch("main.db") as mock_db, \
             patch("main.amocrm_get", side_effect=RuntimeError("CRM error")):

            mock_db.session.scalars.return_value.all.return_value = []

            with self.assertRaises(RuntimeError):
                collect_leads_customers(is_full=True)

            mock_db.session.rollback.assert_called_once()
            mock_db.session.delete.assert_not_called()
            mock_db.session.commit.assert_not_called()

    def test_cleanup_after_success(self):
        """После полной выгрузки устаревшая запись удаляется."""
        old_row = AbstractCustomer(
            amo_customers=[
                {"general": {"id": 1, "login": "old_user"}}
            ]
        )

        with patch("main.db") as mock_db, \
             patch("main.amocrm_get", return_value=iter([])):

            mock_db.session.scalars.return_value.all.return_value = [old_row]

            collect_leads_customers(is_full=True)

            mock_db.session.delete.assert_called_once_with(old_row)
            mock_db.session.commit.assert_called_once()

    def test_merge_customer_and_lead(self):
        """Объединяет покупателя и сделку с одним логином."""

        customer_row = AbstractCustomer(
            id=1,
            amo_customers=[
                {"general": {"id": 11, "login": "user1"}}
            ]
        )

        lead_row = AbstractCustomer(
            id=2,
            amo_leads=[
                {"general": {"id": 22, "login": "user1"}}
            ]
        )

        customer = make_entity(11, "Покупатель", "user1")
        lead = make_entity(22, "Сделка", "user1")

        # Добавляем поля, необходимые для синхронизации
        for entity in (customer, lead):
            entity.get_field = lambda name: "user1"
            entity.contacts = []

        customer.companies = []

        with patch("main.db") as mock_db, \
             patch("main.amocrm_get", side_effect=[[customer], [lead]]):

            mock_db.session.scalars.return_value.all.return_value = [
                customer_row, lead_row
            ]
            mock_db.session.new = set()

            collect_leads_customers(is_full=True)

            # Обе сущности находятся в записи сделки
            self.assertEqual(len(lead_row.amo_customers), 1)
            self.assertEqual(len(lead_row.amo_leads), 1)

            # Старая запись покупателя удаляется
            mock_db.session.delete.assert_called_once_with(customer_row)
            mock_db.session.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
