"""Ranking tests (FR-5): dimensions, aspect ratio, engines, deck diversity."""

from services.image_search.models import DeckResolutionContext, ImageSearchCandidate
from services.image_search.ranking import rank_candidates


def _candidate(
    url="https://cdn.example.org/a.jpg",
    *,
    engine=None,
    width=None,
    height=None,
    title="",
):
    return ImageSearchCandidate(
        title=title,
        image_url=url,
        engine=engine,
        width=width,
        height=height,
    )


def test_known_dimensions_rank_above_unknown():
    unknown = _candidate("https://a.org/unknown.jpg", engine="pexels")
    known = _candidate("https://a.org/known.jpg", engine="pexels", width=800, height=600)
    ranked = rank_candidates([unknown, known])
    assert ranked[0] is known


def test_minimum_usable_size_preferred():
    small = _candidate(
        "https://a.org/small.jpg", engine="pexels", width=50, height=50
    )
    usable = _candidate(
        "https://a.org/usable.jpg",
        engine="pexels",
        width=1200,
        height=800,
    )
    ranked = rank_candidates([small, usable])
    assert ranked[0] is usable


def test_aspect_ratio_closeness_ranking():
    wide = _candidate(
        "https://a.org/wide.jpg", engine="pexels", width=1600, height=400
    )  # 4.0
    target = _candidate(
        "https://a.org/target.jpg", engine="pexels", width=1600, height=900
    )  # 1.78
    tall = _candidate(
        "https://a.org/tall.jpg", engine="pexels", width=600, height=1200
    )  # 0.5
    ranked = rank_candidates([wide, tall, target], aspect_ratio=16 / 9)
    assert ranked[0] is target


def test_engine_preference_order_ranking():
    bing = _candidate("https://a.org/b.jpg", engine="bing images")
    flickr = _candidate("https://a.org/f.jpg", engine="flickr")
    unknown_engine = _candidate("https://a.org/u.jpg", engine="some new engine")
    ranked = rank_candidates([unknown_engine, flickr, bing])
    assert ranked[0] is bing
    assert ranked[1] is flickr
    assert ranked[2] is unknown_engine


def test_chart_like_titles_are_deprioritized():
    chart = _candidate(
        "https://a.org/chart.jpg",
        engine="pexels",
        width=1920,
        height=1080,
        title="Market growth chart 2024 infographic",
    )
    photo = _candidate(
        "https://a.org/photo.jpg",
        engine="flickr",
        width=1920,
        height=1080,
        title="Solar farm at sunrise",
    )
    ranked = rank_candidates([chart, photo])
    assert ranked[0] is photo


def test_deck_context_excludes_already_selected_urls():
    context = DeckResolutionContext()
    context.record("https://a.org/used.jpg", "a.org")
    fresh = _candidate("https://a.org/fresh.jpg", engine="pexels")
    ranked = rank_candidates(
        [_candidate("https://a.org/used.jpg", engine="bing images"), fresh],
        context=context,
    )
    assert ranked == [fresh]


def test_deck_context_penalizes_repeated_domains():
    context = DeckResolutionContext()
    context.record("https://pexels.com/one.jpg", "pexels.com")
    same_domain = _candidate(
        "https://pexels.com/two.jpg", engine="pexels", width=800, height=600
    )
    other_domain = _candidate(
        "https://flickr.example/three.jpg",
        engine="wikicommons.images",
        width=800,
        height=600,
    )
    ranked = rank_candidates([same_domain, other_domain], context=context)
    assert ranked[0] is other_domain


def test_ranking_is_stable_for_equal_scores():
    first = _candidate("https://a.org/1.jpg", engine="pexels")
    second = _candidate("https://a.org/2.jpg", engine="pexels")
    assert rank_candidates([first, second]) == [first, second]
    assert rank_candidates([second, first]) == [second, first]