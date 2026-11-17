"""Citation checks around the editor. No services or model are needed."""

from app.editor import (
    NO_REPORT,
    build_editor_input,
    build_repair_input,
    has_citation,
    remove_unknown_citations,
)
from app.llm import AgentRun, ToolCall
from app.tools import ToolName


def test_empty_report_is_marked_without_raw_tool_results():
    """The editor learns a report is missing and never sees the raw article."""
    run = AgentRun("", [ToolCall(ToolName.SEARCH_KB, "raw article text")], 1.0)

    editor_input = build_editor_input("вопрос", [run])

    assert NO_REPORT in editor_input
    assert "raw article text" not in editor_input


def test_finds_a_citation():
    """A reply with a knowledge base citation needs no repair."""
    assert has_citation(
        "Плата списывается 1-го числа (источник: charge_rules)."
    )


def test_reply_without_citation_is_detected():
    """Parentheses alone are not a citation."""
    assert not has_citation("Списано 149 рублей (за услугу Музыка).")


def test_removes_a_citation_of_an_article_nobody_found():
    """Small models invent sources, and the subscriber must never see them."""
    text = "Услуга Музыка (источник: 1). Плата 1-го числа (источник: charge_rules)."

    cleaned = remove_unknown_citations(text, ["charge_rules"])

    assert (
        cleaned == "Услуга Музыка. Плата 1-го числа (источник: charge_rules)."
    )


def test_without_found_articles_every_citation_goes():
    """With no search results, any citation in the reply is invented."""
    assert remove_unknown_citations("Тариф (источник: нет).", []) == "Тариф."


def test_repair_input_names_the_articles_found():
    """The model gets the exact citations, so it has nothing to invent."""
    repair_input = build_repair_input(
        "original input", "draft", ["charge_rules", "roaming"]
    )

    assert repair_input.startswith("original input")
    assert "draft" in repair_input
    assert "(источник: charge_rules), (источник: roaming)" in repair_input
