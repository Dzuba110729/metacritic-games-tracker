from datetime import date, timedelta

from freezegun import freeze_time

from app.models import DailyState, Game
from app.scraper.selection import ListingSource, pick_batch, today_in


class FakeSource:
    """Заглушка списков: New Releases — nr-0..19, страница N browse — p<N>-0..23."""

    def __init__(self, empty_after: int | None = None):
        self.calls: list[str | int] = []
        self.empty_after = empty_after

    def new_releases(self) -> list[str]:
        self.calls.append("nr")
        return [f"nr-{i}" for i in range(20)]

    def browse_page(self, page: int) -> list[str]:
        self.calls.append(page)
        if self.empty_after is not None and page > self.empty_after:
            return []
        return [f"p{page}-{i}" for i in range(24)]

    def listing(self) -> ListingSource:
        return ListingSource(new_releases=self.new_releases, browse_page=self.browse_page)


def mark_processed(db, slugs, day):
    for slug in slugs:
        db.add(Game(slug=slug, title=slug, last_processed_date=day))
    db.commit()


DAY = date(2026, 10, 15)


def test_first_run_of_day_takes_new_releases(db):
    src = FakeSource()
    batch = pick_batch(DAY, db, src.listing())
    assert batch.source == "new_releases"
    assert batch.slugs == [f"nr-{i}" for i in range(20)]
    assert src.calls == ["nr"]
    state = db.get(DailyState, DAY)
    assert state.new_releases_done
    assert state.next_browse_page == 1


def test_next_runs_walk_browse_pages(db):
    src = FakeSource()
    first = pick_batch(DAY, db, src.listing())
    mark_processed(db, first.slugs, DAY)

    second = pick_batch(DAY, db, src.listing())
    assert second.source == "browse"
    assert second.slugs == [f"p1-{i}" for i in range(20)]
    # На странице 1 остались 4 игры — следующий заход начнёт с неё же.
    assert db.get(DailyState, DAY).next_browse_page == 1
    mark_processed(db, second.slugs, DAY)

    third = pick_batch(DAY, db, src.listing())
    assert third.slugs == [f"p1-{i}" for i in range(20, 24)] + [f"p2-{i}" for i in range(16)]
    assert third.pages == [1, 2]
    assert db.get(DailyState, DAY).next_browse_page == 2


def test_skips_processed_and_goes_to_next_page(db):
    # Все игры страниц 1–2 уже обработаны сегодня (например, ручным запуском).
    mark_processed(db, [f"p{p}-{i}" for p in (1, 2) for i in range(24)], DAY)
    db.add(DailyState(date=DAY, new_releases_done=True, next_browse_page=1))
    db.commit()

    src = FakeSource()
    batch = pick_batch(DAY, db, src.listing())
    assert batch.slugs == [f"p3-{i}" for i in range(20)]
    assert src.calls == [1, 2, 3]


def test_stops_after_five_pages(db):
    mark_processed(db, [f"p{p}-{i}" for p in range(1, 10) for i in range(24)], DAY)
    db.add(DailyState(date=DAY, new_releases_done=True, next_browse_page=1))
    db.commit()

    src = FakeSource()
    batch = pick_batch(DAY, db, src.listing())
    assert batch.slugs == []
    assert src.calls == [1, 2, 3, 4, 5]
    assert db.get(DailyState, DAY).next_browse_page == 6


def test_new_releases_short_is_topped_up_from_browse(db):
    mark_processed(db, [f"nr-{i}" for i in range(15)], DAY)
    src = FakeSource()
    batch = pick_batch(DAY, db, src.listing())
    assert batch.slugs == [f"nr-{i}" for i in range(15, 20)] + [f"p1-{i}" for i in range(15)]
    assert batch.source == "new_releases"


def test_end_of_catalog(db):
    db.add(DailyState(date=DAY, new_releases_done=True, next_browse_page=2))
    db.commit()
    src = FakeSource(empty_after=1)
    assert pick_batch(DAY, db, src.listing()).slugs == []
    assert src.calls == [2]


def test_processed_yesterday_is_picked_again(db):
    mark_processed(db, [f"nr-{i}" for i in range(20)], DAY - timedelta(days=1))
    batch = pick_batch(DAY, db, FakeSource().listing())
    assert batch.slugs == [f"nr-{i}" for i in range(20)]


def test_day_change_by_moscow_time_resets_state(db):
    src = FakeSource()
    # 20:59 UTC 14 октября = 23:59 МСК — ещё 14-е.
    with freeze_time("2026-10-14 20:59:00"):
        day1 = today_in("Europe/Moscow")
        assert day1 == date(2026, 10, 14)
        b1 = pick_batch(day1, db, src.listing())
        mark_processed(db, b1.slugs, day1)
        b2 = pick_batch(day1, db, src.listing())
        assert b2.source == "browse"
        mark_processed(db, b2.slugs, day1)

    # 21:00 UTC = 00:00 МСК 15-го: новый день, снова New Releases, старое состояние удалено.
    with freeze_time("2026-10-14 21:00:00"):
        day2 = today_in("Europe/Moscow")
        assert day2 == date(2026, 10, 15)
        b3 = pick_batch(day2, db, src.listing())
    assert b3.source == "new_releases"
    assert b3.slugs == b1.slugs
    assert db.get(DailyState, day1) is None
    assert db.get(DailyState, day2).next_browse_page == 1
