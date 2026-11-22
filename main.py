import logging
from typing import List, Dict
from collections import defaultdict
from sqlalchemy import select

from abstract_customer import (
    create_or_update_customer,
    create_or_update_lead,
    dict_compare_merge,
)
from amocrm import Customer, Lead, COMPANIES, CONTACTS, amocrm_get
from models import AbstractCustomer, db

logger = logging.getLogger(__name__)



def lead_action(lead: Lead):
    """Находит запись для сделки и объединяет дубликаты."""

    abc_customer = db.session.query(AbstractCustomer).filter(
        AbstractCustomer.amo_leads.op("@>")(
            [{"general": {"id": lead.id}}]
        )
    ).first()

    login = lead.get_field("Логин")

    if login:
        customer_by_login = get_abccustomer_by_amocustomer_login(login)

        if abc_customer is None:
            abc_customer = customer_by_login

        elif (customer_by_login is not None
              and customer_by_login.id != abc_customer.id):

            # Объединяем данные двух записей
            for field in ("amo_customers", "amo_leads",
                          "amo_contacts", "amo_companies"):
                old = getattr(abc_customer, field) or []
                new = getattr(customer_by_login, field) or []

                setattr(
                    abc_customer,
                    field,
                    dict_compare_merge(old, new)
                )

            db.session.delete(customer_by_login)

    return create_or_update_lead(abc_customer, lead)

def build_indexes(rows):
    """Создаёт словари для быстрого поиска покупателей и сделок."""

    customer_by_id = {}
    lead_by_id = {}
    customer_by_login = defaultdict(set)
    lead_by_login = defaultdict(set)

    for row in rows:
        # Индекс покупателей
        for customer in row.amo_customers or []:
            info = customer.get("general") or {}

            if info.get("id") is not None:
                customer_by_id[info["id"]] = row

            if info.get("login"):
                customer_by_login[info["login"]].add(row)

        # Индекс сделок
        for lead in row.amo_leads or []:
            info = lead.get("general") or {}

            if info.get("id") is not None:
                lead_by_id[info["id"]] = row

            if info.get("login"):
                lead_by_login[info["login"]].add(row)

    return customer_by_id, lead_by_id, customer_by_login, lead_by_login

def find_unique(index, login):
    """Возвращает запись, только если логин однозначный."""
    matches = index.get(login, set())

    if len(matches) == 1:
        return next(iter(matches))

    return None


def collect_leads_customers(is_full=True):
    """Синхронизирует покупателей и сделки. При полном проходе удаляет устаревшее."""
    session = db.session
    rows = session.scalars(select(AbstractCustomer)).all()

    customer_by_id, lead_by_id, customer_by_login, lead_by_login = build_indexes(rows)

    redirects = {}
    seen = {}

    def actual(row):
        """Возвращает запись, оставшуюся после объединения."""
        while row in redirects:
            row = redirects[row]
        return row

    def find_by_login(index, login, field):
        """Ищет запись по логину, если совпадение единственное."""
        if not login:
            return None

        matches = set()
        for row in index.get(login, set()):
            row = actual(row)
            if any(
                (item.get("general") or {}).get("login") == login
                for item in getattr(row, field) or []
            ):
                matches.add(row)

        if len(matches) > 1:
            logger.warning("Неоднозначный логин: %s", login)

        return next(iter(matches)) if len(matches) == 1 else None

    def index_row(row):
        """Обновляет словари после изменения записи."""
        for field, ids, logins in (
            ("amo_customers", customer_by_id, customer_by_login),
            ("amo_leads", lead_by_id, lead_by_login),
        ):
            for item in getattr(row, field) or []:
                info = item.get("general") or {}

                if info.get("id") is not None:
                    ids[info["id"]] = row

                if info.get("login"):
                    logins[info["login"]].add(row)

    def remember(row, kind, crm_id, contacts, companies=()):
        """Запоминает сущности текущей выгрузки для очистки."""
        ids = seen.setdefault(row, {
            "customers": set(),
            "leads": set(),
            "contacts": set(),
            "companies": set(),
        })

        ids[kind].add(crm_id)
        ids["contacts"].update(
            c["id"] for c in contacts or []
            if c.get("id") is not None
        )
        ids["companies"].update(
            c["id"] for c in companies or []
            if c.get("id") is not None
        )

    try:
        # Обрабатываем покупателей
        logger.info("Сбор покупателей: is_full=%s", is_full)

        for customer in amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}"):
            if not is_full and customer.status_id == 111349:
                continue

            login = customer.get_field("Логин")
            if not login:
                continue

            row = customer_by_id.get(customer.id)

            if row is not None:
                row = actual(row)
            else:
                row = find_by_login(lead_by_login, login, "amo_leads")

            is_new = row is None
            row = create_or_update_customer(row, customer)

            if is_new:
                rows.append(row)

            index_row(row)

            if is_full:
                remember(
                    row, "customers", customer.id,
                    customer.contacts, customer.companies
                )

        # Обрабатываем сделки
        logger.info("Сбор сделок: is_full=%s", is_full)

        for lead in amocrm_get(Lead, _with=f"{CONTACTS}"):
            if not is_full and lead.status_id == 143:
                continue

            row = lead_by_id.get(lead.id)
            if row is not None:
                row = actual(row)

            login = lead.get_field("Логин")
            customer_row = find_by_login(
                customer_by_login, login, "amo_customers"
            )

            if row is not None and customer_row is not None and row is not customer_row:
                # Объединяем в запись, найденную по ID сделки
                for field in (
                    "amo_customers", "amo_leads",
                    "amo_contacts", "amo_companies"
                ):
                    merged = dict_compare_merge(
                        getattr(row, field) or [],
                        getattr(customer_row, field) or []
                    )
                    setattr(row, field, merged)

                old_seen = seen.pop(customer_row, None)

                if old_seen:
                    current = seen.setdefault(row, {
                        "customers": set(),
                        "leads": set(),
                        "contacts": set(),
                        "companies": set(),
                    })
                    for kind, ids in old_seen.items():
                        current[kind].update(ids)

                redirects[customer_row] = row

                if customer_row in session.new:
                    session.expunge(customer_row)
                else:
                    session.delete(customer_row)

                index_row(row)

            elif row is None:
                row = customer_row

            is_new = row is None
            row = create_or_update_lead(row, lead)

            if is_new:
                rows.append(row)

            index_row(row)

            if is_full:
                remember(row, "leads", lead.id, lead.contacts)

        # Очистка выполняется только после полного сбора
        if is_full:
            logger.info("Удаление устаревших данных")

            for row in rows:
                if actual(row) is not row:
                    continue

                ids = seen.get(row)

                if ids is None:
                    session.delete(row)
                    continue

                for field, kind in (
                    ("amo_customers", "customers"),
                    ("amo_leads", "leads"),
                    ("amo_contacts", "contacts"),
                    ("amo_companies", "companies"),
                ):
                    items = [
                        item for item in getattr(row, field) or []
                        if (item.get("general") or {}).get("id") in ids[kind]
                        and "удалить" not in str(
                            (item.get("general") or {}).get("name") or ""
                        ).lower()
                    ]
                    setattr(row, field, items)

                if not row.amo_customers and not row.amo_leads:
                    if row in session.new:
                        session.expunge(row)
                    else:
                        session.delete(row)

        session.commit()

    except Exception:
        session.rollback()
        logger.exception("Ошибка синхронизации")
        raise

def filter_merge_abstract_customer(abc_customer: AbstractCustomer, attr: str,
                                   check_ids: List[int]):
    entity = abc_customer.__getattribute__(attr)
    for l in entity:
        entity = [c for c in entity if
                  'удалить' not in l['general'].get('name').lower() and l['general'].get('id') in check_ids]
        setattr(abc_customer, attr, entity)
        flag_modified(abc_customer, attr)
        db.session.commit()


def filter_abc_customer(abc_customer: AbstractCustomer, ids_map: Dict):
    if abc_customer.amo_leads:
        try:
            filter_merge_abstract_customer(abc_customer=abc_customer,
                                           attr="amo_leads",
                                           check_ids=ids_map[abc_customer.id]["leads"])
        except Exception as e:
            logger.exception(e)
    if abc_customer.amo_customers:
        try:
            filter_merge_abstract_customer(abc_customer=abc_customer,
                                           attr="amo_customers",
                                           check_ids=ids_map[abc_customer.id]["customers"])
        except Exception as e:
            logger.exception(e)
    if abc_customer.amo_contacts:
        try:
            filter_merge_abstract_customer(abc_customer=abc_customer,
                                           attr="amo_contacts",
                                           check_ids=ids_map[abc_customer.id]["contacts"])
        except Exception as e:
            logger.exception(e)
    if abc_customer.amo_companies:
        try:
            filter_merge_abstract_customer(abc_customer=abc_customer,
                                           attr="amo_companies",
                                           check_ids=ids_map[abc_customer.id]["companies"])
        except Exception as e:
            logger.exception(e)
    if not abc_customer.amo_leads and not abc_customer.amo_customers:
        db.session.flush()
        db.session.delete(abc_customer)
    db.session.commit()
