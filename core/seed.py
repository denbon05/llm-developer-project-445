"""Сид демо-данных в Postgres: пользователи, тарифы, абоненты, услуги,
списания, база знаний (статьи + векторы из снапшота).

    python -m core.seed

Пересоздаёт доменные таблицы целиком (идемпотентно): удобно и для
первого запуска, и для `make seed` в любой момент.
"""

from __future__ import annotations

import json

import yaml

from core import db
from core.config import ROOT

DEMO_FILE = ROOT / "data" / "demo.yaml"
SNAPSHOT_FILE = ROOT / "kb" / "vectors.json"


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{component:.6f}" for component in vector) + "]"


def seed() -> dict:
    demo = yaml.safe_load(DEMO_FILE.read_text(encoding="utf-8"))

    with db.connect() as conn, conn.cursor() as cur:
        # домен оператора: чистая перезаливка; контурные таблицы сессий,
        # карточек и идемпотентности тоже сбрасываются — reset = исходное демо
        for table in (
            "billing_events",
            "tariff_change_log",
            "subscriber_services",
            "subscribers",
            "tariffs",
            "tickets",
            "kb_chunks",
            "idempotency",
            "pending_cards",
            "sessions",
            "users",
        ):
            cur.execute(f"delete from {table}")

        for tariff in demo["tariffs"]:
            cur.execute(
                "insert into tariffs(name, price_rub, data_gb, description, available)"
                " values (%s, %s, %s, %s, %s)",
                (
                    tariff["name"],
                    tariff["price_rub"],
                    tariff["data_gb"],
                    tariff["description"],
                    tariff["available"],
                ),
            )

        login_by_sub: dict[str, str] = {}
        # все пользователи контура (включая оператора без абонентской записи)
        for user in _users():
            cur.execute(
                "insert into users(login, sub, name, role, scopes)"
                " values (%s, %s, %s, %s, %s)"
                " on conflict (login) do update set sub = excluded.sub,"
                " role = excluded.role, scopes = excluded.scopes",
                (
                    user["login"],
                    user["sub"],
                    user["name"],
                    user["role"],
                    user["scopes"],
                ),
            )
        for subscriber in demo["subscribers"]:
            cur.execute(
                "insert into subscribers(sub, login, name, phone, email, tariff, balance_rub)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    subscriber["sub"],
                    subscriber["login"],
                    subscriber["name"],
                    subscriber["phone"],
                    subscriber.get("email"),
                    subscriber["tariff"],
                    subscriber["balance_rub"],
                ),
            )
            login_by_sub[subscriber["sub"]] = subscriber["login"]
            for svc in subscriber.get("services", []):
                price = next(
                    (
                        catalog_item["price_rub"]
                        for catalog_item in demo["service_catalog"]
                        if catalog_item["name"] == svc["name"]
                    ),
                    0,
                )
                cur.execute(
                    "insert into subscriber_services(sub, service, enabled, price_rub)"
                    " values (%s, %s, %s, %s)",
                    (subscriber["sub"], svc["name"], svc["enabled"], price),
                )

        for charge in demo["charges"]:
            cur.execute(
                "insert into billing_events(sub, month, label, amount_rub)"
                " values (%s, %s, %s, %s)",
                (
                    charge["sub"],
                    charge["month"],
                    charge["label"],
                    charge["amount_rub"],
                ),
            )

        # база знаний: статьи + векторы из снапшота
        articles = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        for article in articles:
            cur.execute(
                "insert into kb_chunks(source, title, body, embedding)"
                " values (%s, %s, %s, %s::vector)",
                (
                    article["source"],
                    article["title"],
                    article["body"],
                    _vector_literal(article["vector"]),
                ),
            )

        conn.commit()

    return {
        "users": len(demo["subscribers"]),
        "tariffs": len(demo["tariffs"]),
        "kb_chunks": len(articles),
    }


def _users() -> list[dict]:
    from core.config import USERS

    return [
        {
            "login": user.login,
            "sub": user.sub,
            "name": user.name,
            "role": user.role,
            "scopes": user.scopes,
        }
        for user in USERS.values()
    ]


if __name__ == "__main__":
    print("seed:", seed())
