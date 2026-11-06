"""Диалоговые сценарии на живой LLM: матрица успех/отказ/атака/лимит.

    python scripts/run_scenarios.py            # все сценарии
    python scripts/run_scenarios.py 30 80      # по префиксам имён

Сценарий — YAML в data/dialogs: пользователь, реплики и ожидания. Раннер
открывает сессию backend, гонит реплики по SSE, собирает события и
проверяет ожидания; состояние моков (тариф, услуги, тикеты) сверяется
после хода. Перед каждым сценарием моки сбрасываются.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx2
import yaml

ROOT = Path(__file__).resolve().parent.parent

from core import config

DIALOGS = ROOT / "data" / "dialogs"
RESULTS: list[tuple[str, bool, str]] = []


def client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(trust_env=False, timeout=300)


async def chat_turn(
    http: httpx2.AsyncClient, session_id: str, message: str
) -> list[dict]:
    """Один ход по SSE → список событий."""
    events: list[dict] = []
    async with http.stream(
        "POST",
        f"{config.BACKEND_URL}/api/chat",
        json={"session_id": session_id, "message": message},
    ) as response:
        buf = ""
        async for chunk in response.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                line = next(
                    (
                        sse_line
                        for sse_line in raw.split("\n")
                        if sse_line.startswith("data: ")
                    ),
                    None,
                )
                if line:
                    import json

                    events.append(json.loads(line[6:]))
    return [event for event in events if event.get("type") != "done"]


def _get_text(events: list[dict]) -> str:
    replies = [
        event.get("text", "")
        for event in events
        if event.get("type") == "reply"
    ]
    return "\n".join(replies)


async def check_expectations(
    http: httpx2.AsyncClient, scenario: dict, turn_events: list[dict]
) -> tuple[bool, str]:
    """Ожидания реплики — по событиям этого хода; состояние — по мокам сейчас."""
    expect = scenario.get("expect", {}) or {}
    plan_events = [
        event for event in turn_events if event.get("type") == "plan"
    ]
    cards = [event for event in turn_events if event.get("type") == "card"]
    text = _get_text(turn_events)

    for token in expect.get("plan_has", []) or []:
        if not any(
            token in agent
            for event in plan_events
            for agent in event.get("agents", [])
        ):
            return False, f"в плане нет «{token}»"
    if "card" in expect:
        if expect["card"] and not cards:
            return False, "ожидалась карточка — не пришла"
        if not expect["card"] and cards:
            return False, "карточка не ожидалась, но пришла"
    for needle in expect.get("reply_contains", []) or []:
        if needle.lower() not in text.lower():
            return False, f"в ответе нет «{needle}» (ответ: {text[:120]})"
    for needle in expect.get("reply_not_contains", []) or []:
        if needle.lower() in text.lower():
            return False, f"в ответе лишнее «{needle}»"

    state = expect.get("state", {}) or {}
    if "tariff" in state:
        sub = state.get("sub", "u_4f2a")
        response = await http.get(
            f"{config.STAND_URL}/mock/crm/subscriber?sub={sub}"
        )
        actual = response.json().get("tariff")
        if actual != state["tariff"]:
            return False, f"тариф {sub}: {actual}, ожидался {state['tariff']}"
    if "service" in state:
        svc = state["service"]
        response = await http.get(
            f"{config.STAND_URL}/mock/crm/subscriber?sub=u_4f2a"
        )
        row = next(
            (
                subscriber_service
                for subscriber_service in response.json().get("services", [])
                if subscriber_service["service"] == svc["name"]
            ),
            None,
        )
        actual = row["enabled"] if row else False
        if actual != svc["enabled"]:
            return (
                False,
                f"услуга {svc['name']}={actual}, ожидалось {svc['enabled']}",
            )
    if "tickets_min" in state:
        response = await http.get(f"{config.STAND_URL}/mock/tickets?sub=u_4f2a")
        if len(response.json().get("tickets", [])) < state["tickets_min"]:
            return False, f"тикетов меньше {state['tickets_min']}"
    return True, ""


async def run_scenario(http: httpx2.AsyncClient, path: Path) -> None:
    scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
    name = scenario.get("name", path.stem)
    await http.post(f"{config.STAND_URL}/mock/admin/reset")

    response = await http.post(
        f"{config.BACKEND_URL}/api/session", json={"user": scenario["user"]}
    )
    session_id = response.json()["session_id"]

    all_events: list[dict] = []
    for turn in scenario["turns"]:
        events = await chat_turn(http, session_id, turn["say"])
        all_events.extend(events)
        ok, why = await check_expectations(http, turn, events)
        if not ok:
            RESULTS.append(
                (name, False, f"реплика «{turn['say'][:40]}»: {why}")
            )
            return

    RESULTS.append((name, True, ""))


async def main() -> int:
    wanted = sys.argv[1:]
    files = sorted(DIALOGS.glob("*.yaml"))
    async with client() as http:
        for path in files:
            if wanted and not any(
                path.stem.startswith(prefix) for prefix in wanted
            ):
                continue
            try:
                await run_scenario(http, path)
            except Exception as error:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                RESULTS.append((path.stem, False, repr(error)[:160]))

    print("\n──── матрица сценариев ────")
    for name, ok, why in RESULTS:
        print(f"[{'ok ' if ok else 'FAIL'}] {name} {why}")
    failed = [result for result in RESULTS if not result[1]]
    print(f"\nитого: {len(RESULTS) - len(failed)}/{len(RESULTS)} зелёные")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
