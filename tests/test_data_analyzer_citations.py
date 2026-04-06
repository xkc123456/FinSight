from src.agents.data_analyzer.data_analyzer import DataAnalyzer
from src.tools.base import ToolResult
from src.tools.web.base_search import SearchResult
from src.tools.web.web_crawler import ClickResult


def test_convert_deepsearch_citations_rewrites_numbered_references():
    text = (
        "Alpha[1] beta[\u6ce82] gamma[1, 2].\n\n"
        "## References\n"
        "[1] Foo Title - https://example.com/foo\n"
        "[2] Bar Title https://example.com/bar\n"
    )

    assert DataAnalyzer._convert_deepsearch_citations(text) == (
        "Alpha[Source: Foo Title] beta[Source: Bar Title] "
        "gamma[Source: Foo Title][Source: Bar Title]."
    )


def test_collect_and_append_new_web_sources_filters_and_deduplicates():
    collected = [
        SearchResult(
            query="q",
            name="Foo Result",
            description="desc",
            data={},
            link="https://example.com/foo",
            source="serper",
        ),
        ClickResult(
            name="Foo Page",
            description="desc",
            data="body",
            link="https://example.com/foo",
            source="URL: https://example.com/foo",
        ),
        SearchResult(
            query="q",
            name="Bar Result",
            description="desc",
            data={},
            link="https://example.com/bar",
            source="serper",
        ),
        ToolResult(name="API dataset", description="desc", data="x", source="api"),
    ]

    sources = DataAnalyzer._collect_web_sources(collected)

    assert sources == [
        ("Foo Result", "https://example.com/foo"),
        ("Bar Result", "https://example.com/bar"),
    ]
    assert DataAnalyzer._append_new_web_sources(
        "Summary",
        sources,
        {"https://example.com/foo"},
    ) == (
        "Summary\n\n---\n"
        "**Web sources discovered during this search (use `[Source: <title>]` to cite):**\n"
        "- Bar Result: https://example.com/bar\n"
    )
