from __future__ import annotations

from tools.analysis.douyin import parse_douyin_share_page


def test_parses_structured_douyin_router_payload():
    html = """
    <html><script>
    window._ROUTER_DATA = {"loaderData":{"video_(id)/page":{"videoInfoRes":{"item_list":[{
      "aweme_id":"7667931266800454975",
      "desc":"test title",
      "create_time":1720000000,
      "author":{"nickname":"creator"},
      "statistics":{"digg_count":42,"play_count":100},
      "video":{"duration":5234,"width":1080,"height":1920,
        "play_addr":{"url_list":["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"]}}
    }]}}}};
    </script></html>
    """
    data = parse_douyin_share_page(html)
    assert data["id"] == "7667931266800454975"
    assert data["title"] == "test title"
    assert data["duration"] == 5.234
    assert data["uploader"] == "creator"
    assert data["resolution"] == "1080x1920"
    assert "/play/" in data["play_url"]
    assert "/playwm/" not in data["play_url"]
