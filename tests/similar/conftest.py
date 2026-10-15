import pytest

from app.db import init_db, make_engine, make_sessionmaker


@pytest.fixture
def db():
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_sessionmaker(engine)() as session:
        yield session
    engine.dispose()
