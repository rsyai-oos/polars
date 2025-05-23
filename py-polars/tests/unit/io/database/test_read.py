from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import suppress
from datetime import date
from pathlib import Path
from types import GeneratorType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pyarrow as pa
import pytest
import sqlalchemy
from sqlalchemy import Integer, MetaData, Table, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import cast as alchemy_cast

import polars as pl
from polars._utils.various import parse_version
from polars.exceptions import DuplicateError, UnsuitableSQLError
from polars.io.database._arrow_registry import ARROW_DRIVER_REGISTRY
from polars.testing import assert_frame_equal, assert_series_equal

if TYPE_CHECKING:
    from polars._typing import (
        ConnectionOrCursor,
        DbReadEngine,
        SchemaDefinition,
        SchemaDict,
    )


def adbc_sqlite_connect(*args: Any, **kwargs: Any) -> Any:
    with suppress(ModuleNotFoundError):  # not available on windows
        from adbc_driver_sqlite.dbapi import connect

        args = tuple(str(a) if isinstance(a, Path) else a for a in args)
        return connect(*args, **kwargs)


class MockConnection:
    """Mock connection class for databases we can't test in CI."""

    def __init__(
        self,
        driver: str,
        batch_size: int | None,
        exact_batch_size: bool,
        test_data: pa.Table,
        repeat_batch_calls: bool,
    ) -> None:
        self.__class__.__module__ = driver
        self._cursor = MockCursor(
            repeat_batch_calls=repeat_batch_calls,
            exact_batch_size=exact_batch_size,
            batched=(batch_size is not None),
            test_data=test_data,
        )

    def close(self) -> None:
        pass

    def cursor(self) -> Any:
        return self._cursor


class MockCursor:
    """Mock cursor class for databases we can't test in CI."""

    def __init__(
        self,
        batched: bool,
        exact_batch_size: bool,
        test_data: pa.Table,
        repeat_batch_calls: bool,
    ) -> None:
        self.resultset = MockResultSet(
            test_data=test_data,
            batched=batched,
            exact_batch_size=exact_batch_size,
            repeat_batch_calls=repeat_batch_calls,
        )
        self.exact_batch_size = exact_batch_size
        self.called: list[str] = []
        self.batched = batched
        self.n_calls = 1

    def __getattr__(self, name: str) -> Any:
        if "fetch" in name:
            self.called.append(name)
            return self.resultset
        super().__getattr__(name)  # type: ignore[misc]

    def close(self) -> Any:
        pass

    def execute(self, query: str) -> Any:
        return self


class MockResultSet:
    """Mock resultset class for databases we can't test in CI."""

    def __init__(
        self,
        test_data: pa.Table,
        batched: bool,
        exact_batch_size: bool,
        repeat_batch_calls: bool = False,
    ) -> None:
        self.test_data = test_data
        self.repeat_batched_calls = repeat_batch_calls
        self.exact_batch_size = exact_batch_size
        self.batched = batched
        self.n_calls = 1

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.exact_batch_size:
            assert len(args) == 0
        if self.repeat_batched_calls:
            res = self.test_data[: None if self.n_calls else 0]
            self.n_calls -= 1
        else:
            res = iter((self.test_data,))
        return res


class DatabaseReadTestParams(NamedTuple):
    """Clarify read test params."""

    read_method: str
    connect_using: Any
    expected_dtypes: SchemaDefinition
    expected_dates: list[date | str]
    schema_overrides: SchemaDict | None = None
    batch_size: int | None = None


class ExceptionTestParams(NamedTuple):
    """Clarify exception test params."""

    read_method: str
    query: str | list[str]
    protocol: Any
    errclass: type[Exception]
    errmsg: str
    engine: str | None = None
    execute_options: dict[str, Any] | None = None
    kwargs: dict[str, Any] | None = None


@pytest.mark.write_disk
@pytest.mark.parametrize(
    (
        "read_method",
        "connect_using",
        "expected_dtypes",
        "expected_dates",
        "schema_overrides",
        "batch_size",
    ),
    [
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database_uri",
                connect_using="connectorx",
                expected_dtypes={
                    "id": pl.UInt8,
                    "name": pl.String,
                    "value": pl.Float64,
                    "date": pl.Date,
                },
                expected_dates=[date(2020, 1, 1), date(2021, 12, 31)],
                schema_overrides={"id": pl.UInt8},
            ),
            id="uri: connectorx",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database_uri",
                connect_using="adbc",
                expected_dtypes={
                    "id": pl.UInt8,
                    "name": pl.String,
                    "value": pl.Float64,
                    "date": pl.String,
                },
                expected_dates=["2020-01-01", "2021-12-31"],
                schema_overrides={"id": pl.UInt8},
            ),
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="adbc_driver_sqlite not available on Windows",
            ),
            id="uri: adbc",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database",
                connect_using=lambda path: sqlite3.connect(path, detect_types=True),
                expected_dtypes={
                    "id": pl.UInt8,
                    "name": pl.String,
                    "value": pl.Float32,
                    "date": pl.Date,
                },
                expected_dates=[date(2020, 1, 1), date(2021, 12, 31)],
                schema_overrides={"id": pl.UInt8, "value": pl.Float32},
            ),
            id="conn: sqlite3",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database",
                connect_using=lambda path: sqlite3.connect(path, detect_types=True),
                expected_dtypes={
                    "id": pl.Int32,
                    "name": pl.String,
                    "value": pl.Float32,
                    "date": pl.Date,
                },
                expected_dates=[date(2020, 1, 1), date(2021, 12, 31)],
                schema_overrides={"id": pl.Int32, "value": pl.Float32},
                batch_size=1,
            ),
            id="conn: sqlite3",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database",
                connect_using=lambda path: create_engine(
                    f"sqlite:///{path}",
                    connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
                ).connect(),
                expected_dtypes={
                    "id": pl.Int64,
                    "name": pl.String,
                    "value": pl.Float64,
                    "date": pl.Date,
                },
                expected_dates=[date(2020, 1, 1), date(2021, 12, 31)],
            ),
            id="conn: sqlalchemy",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database",
                connect_using=adbc_sqlite_connect,
                expected_dtypes={
                    "id": pl.Int64,
                    "name": pl.String,
                    "value": pl.Float64,
                    "date": pl.String,
                },
                expected_dates=["2020-01-01", "2021-12-31"],
            ),
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="adbc_driver_sqlite not available on Windows",
            ),
            id="conn: adbc (fetchall)",
        ),
        pytest.param(
            *DatabaseReadTestParams(
                read_method="read_database",
                connect_using=adbc_sqlite_connect,
                expected_dtypes={
                    "id": pl.Int64,
                    "name": pl.String,
                    "value": pl.Float64,
                    "date": pl.String,
                },
                expected_dates=["2020-01-01", "2021-12-31"],
                batch_size=1,
            ),
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="adbc_driver_sqlite not available on Windows",
            ),
            id="conn: adbc (batched)",
        ),
    ],
)
def test_read_database(
    read_method: str,
    connect_using: Any,
    expected_dtypes: dict[str, pl.DataType],
    expected_dates: list[date | str],
    schema_overrides: SchemaDict | None,
    batch_size: int | None,
    tmp_sqlite_db: Path,
) -> None:
    if read_method == "read_database_uri":
        connect_using = cast("DbReadEngine", connect_using)
        # instantiate the connection ourselves, using connectorx/adbc
        df = pl.read_database_uri(
            uri=f"sqlite:///{tmp_sqlite_db}",
            query="SELECT * FROM test_data",
            engine=connect_using,
            schema_overrides=schema_overrides,
        )
        df_empty = pl.read_database_uri(
            uri=f"sqlite:///{tmp_sqlite_db}",
            query="SELECT * FROM test_data WHERE name LIKE '%polars%'",
            engine=connect_using,
            schema_overrides=schema_overrides,
        )
    elif "adbc" in os.environ["PYTEST_CURRENT_TEST"]:
        # externally instantiated adbc connections
        with connect_using(tmp_sqlite_db) as conn, conn.cursor():
            df = pl.read_database(
                connection=conn,
                query="SELECT * FROM test_data",
                schema_overrides=schema_overrides,
                batch_size=batch_size,
            )
            df_empty = pl.read_database(
                connection=conn,
                query="SELECT * FROM test_data WHERE name LIKE '%polars%'",
                schema_overrides=schema_overrides,
                batch_size=batch_size,
            )
    else:
        # other user-supplied connections
        df = pl.read_database(
            connection=connect_using(tmp_sqlite_db),
            query="SELECT * FROM test_data WHERE name NOT LIKE '%polars%'",
            schema_overrides=schema_overrides,
            batch_size=batch_size,
        )
        df_empty = pl.read_database(
            connection=connect_using(tmp_sqlite_db),
            query="SELECT * FROM test_data WHERE name LIKE '%polars%'",
            schema_overrides=schema_overrides,
            batch_size=batch_size,
        )

    # validate the expected query return (data and schema)
    assert df.schema == expected_dtypes
    assert df.shape == (2, 4)
    assert df["date"].to_list() == expected_dates

    # note: 'cursor.description' is not reliable when no query
    # data is returned, so no point comparing expected dtypes
    assert df_empty.columns == ["id", "name", "value", "date"]
    assert df_empty.shape == (0, 4)
    assert df_empty["date"].to_list() == []


def test_read_database_alchemy_selectable(tmp_sqlite_db: Path) -> None:
    # various flavours of alchemy connection
    alchemy_engine = create_engine(f"sqlite:///{tmp_sqlite_db}")
    alchemy_session: ConnectionOrCursor = sessionmaker(bind=alchemy_engine)()
    alchemy_conn: ConnectionOrCursor = alchemy_engine.connect()

    t = Table("test_data", MetaData(), autoload_with=alchemy_engine)

    # establish sqlalchemy "selectable" and validate usage
    selectable_query = select(
        alchemy_cast(func.strftime("%Y", t.c.date), Integer).label("year"),
        t.c.name,
        t.c.value,
    ).where(t.c.value < 0)

    expected = pl.DataFrame({"year": [2021], "name": ["other"], "value": [-99.5]})

    for conn in (alchemy_session, alchemy_engine, alchemy_conn):
        assert_frame_equal(
            pl.read_database(selectable_query, connection=conn),
            expected,
        )

    batches = list(
        pl.read_database(
            selectable_query,
            connection=conn,
            iter_batches=True,
            batch_size=1,
        )
    )
    assert len(batches) == 1
    assert_frame_equal(batches[0], expected)


def test_read_database_alchemy_textclause(tmp_sqlite_db: Path) -> None:
    # various flavours of alchemy connection
    alchemy_engine = create_engine(f"sqlite:///{tmp_sqlite_db}")
    alchemy_session: ConnectionOrCursor = sessionmaker(bind=alchemy_engine)()
    alchemy_conn: ConnectionOrCursor = alchemy_engine.connect()

    # establish sqlalchemy "textclause" and validate usage
    textclause_query = text(
        """
                SELECT CAST(STRFTIME('%Y',"date") AS INT) as "year", name, value
                FROM test_data
                WHERE value < 0
            """
    )

    expected = pl.DataFrame({"year": [2021], "name": ["other"], "value": [-99.5]})

    for conn in (alchemy_session, alchemy_engine, alchemy_conn):
        assert_frame_equal(
            pl.read_database(textclause_query, connection=conn),
            expected,
        )

    batches = list(
        pl.read_database(
            textclause_query,
            connection=conn,
            iter_batches=True,
            batch_size=1,
        )
    )
    assert len(batches) == 1
    assert_frame_equal(batches[0], expected)


def test_read_database_parameterised(tmp_sqlite_db: Path) -> None:
    # raw cursor "execute" only takes positional params, alchemy cursor takes kwargs
    alchemy_engine = create_engine(f"sqlite:///{tmp_sqlite_db}")
    alchemy_conn: ConnectionOrCursor = alchemy_engine.connect()
    alchemy_session: ConnectionOrCursor = sessionmaker(bind=alchemy_engine)()
    raw_conn: ConnectionOrCursor = sqlite3.connect(tmp_sqlite_db)

    # establish parameterised queries and validate usage
    query = """
        SELECT CAST(STRFTIME('%Y',"date") AS INT) as "year", name, value
        FROM test_data
        WHERE value < {n}
    """
    expected_frame = pl.DataFrame({"year": [2021], "name": ["other"], "value": [-99.5]})

    for param, param_value in (
        (":n", {"n": 0}),
        ("?", (0,)),
        ("?", [0]),
    ):
        for conn in (alchemy_session, alchemy_engine, alchemy_conn, raw_conn):
            if alchemy_session is conn and param == "?":
                continue  # alchemy session.execute() doesn't support positional params
            if parse_version(sqlalchemy.__version__) < (2, 0) and param == ":n":
                continue  # skip for older sqlalchemy versions

            assert_frame_equal(
                expected_frame,
                pl.read_database(
                    query.format(n=param),
                    connection=conn,
                    execute_options={"parameters": param_value},
                ),
            )


@pytest.mark.parametrize(
    ("param", "param_value"),
    [
        (":n", {"n": 0}),
        ("?", (0,)),
        ("?", [0]),
    ],
)
@pytest.mark.skipif(
    sys.platform == "win32", reason="adbc_driver_sqlite not available on Windows"
)
def test_read_database_parameterised_uri(
    param: str, param_value: Any, tmp_sqlite_db: Path
) -> None:
    alchemy_engine = create_engine(f"sqlite:///{tmp_sqlite_db}")
    uri = alchemy_engine.url.render_as_string(hide_password=False)
    query = """
        SELECT CAST(STRFTIME('%Y',"date") AS INT) as "year", name, value
        FROM test_data
        WHERE value < {n}
    """
    expected_frame = pl.DataFrame({"year": [2021], "name": ["other"], "value": [-99.5]})

    for param, param_value in (
        (":n", pa.Table.from_pydict({"n": [0]})),
        ("?", (0,)),
        ("?", [0]),
    ):
        # test URI read method (adbc only)
        assert_frame_equal(
            expected_frame,
            pl.read_database_uri(
                query.format(n=param),
                uri=uri,
                engine="adbc",
                execute_options={"parameters": param_value},
            ),
        )

    #  no connectorx support for execute_options
    with pytest.raises(
        ValueError,
        match="connectorx.*does not support.*execute_options",
    ):
        pl.read_database_uri(
            query.format(n=":n"),
            uri=uri,
            engine="connectorx",
            execute_options={"parameters": (":n", {"n": 0})},
        )


@pytest.mark.parametrize(
    ("driver", "batch_size", "iter_batches", "expected_call"),
    [
        ("snowflake", None, False, "fetch_arrow_all"),
        ("snowflake", 10_000, False, "fetch_arrow_all"),
        ("snowflake", 10_000, True, "fetch_arrow_batches"),
        ("databricks", None, False, "fetchall_arrow"),
        ("databricks", 25_000, False, "fetchall_arrow"),
        ("databricks", 25_000, True, "fetchmany_arrow"),
        ("turbodbc", None, False, "fetchallarrow"),
        ("turbodbc", 50_000, False, "fetchallarrow"),
        ("turbodbc", 50_000, True, "fetcharrowbatches"),
        ("adbc_driver_postgresql", None, False, "fetch_arrow_table"),
        ("adbc_driver_postgresql", 75_000, False, "fetch_arrow_table"),
        ("adbc_driver_postgresql", 75_000, True, "fetch_arrow_table"),
    ],
)
def test_read_database_mocked(
    driver: str, batch_size: int | None, iter_batches: bool, expected_call: str
) -> None:
    # since we don't have access to snowflake/databricks/etc from CI we
    # mock them so we can check that we're calling the expected methods
    arrow = pl.DataFrame({"x": [1, 2, 3], "y": ["aa", "bb", "cc"]}).to_arrow()

    reg = ARROW_DRIVER_REGISTRY.get(driver, {})  # type: ignore[var-annotated]
    exact_batch_size = reg.get("exact_batch_size", False)
    repeat_batch_calls = reg.get("repeat_batch_calls", False)

    mc = MockConnection(
        driver,
        batch_size,
        test_data=arrow,
        repeat_batch_calls=repeat_batch_calls,
        exact_batch_size=exact_batch_size,  # type: ignore[arg-type]
    )
    res = pl.read_database(
        query="SELECT * FROM test_data",
        connection=mc,
        iter_batches=iter_batches,
        batch_size=batch_size,
    )
    if iter_batches:
        assert isinstance(res, GeneratorType)
        res = pl.concat(res)

    res = cast(pl.DataFrame, res)
    assert expected_call in mc.cursor().called
    assert res.rows() == [(1, "aa"), (2, "bb"), (3, "cc")]


@pytest.mark.parametrize(
    (
        "read_method",
        "query",
        "protocol",
        "errclass",
        "errmsg",
        "engine",
        "execute_options",
        "kwargs",
    ),
    [
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database_uri",
                query="SELECT * FROM test_data",
                protocol="sqlite",
                errclass=ValueError,
                errmsg="engine must be one of {'connectorx', 'adbc'}, got 'not_an_engine'",
                engine="not_an_engine",
            ),
            id="Not an available sql engine",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database_uri",
                query=["SELECT * FROM test_data", "SELECT * FROM test_data"],
                protocol="sqlite",
                errclass=ValueError,
                errmsg="only a single SQL query string is accepted for adbc",
                engine="adbc",
            ),
            id="Unavailable list of queries for adbc",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database_uri",
                query="SELECT * FROM test_data",
                protocol="mysql",
                errclass=ModuleNotFoundError,
                errmsg="ADBC 'adbc_driver_mysql.dbapi' driver not detected.",
                engine="adbc",
            ),
            id="Unavailable adbc driver",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database_uri",
                query="SELECT * FROM test_data",
                protocol=sqlite3.connect(":memory:"),
                errclass=TypeError,
                errmsg="expected connection to be a URI string",
                engine="adbc",
            ),
            id="Invalid connection URI",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                query="SELECT * FROM imaginary_table",
                protocol=sqlite3.connect(":memory:"),
                errclass=sqlite3.OperationalError,
                errmsg="no such table: imaginary_table",
            ),
            id="Invalid query (unrecognised table name)",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                query="SELECT * FROM imaginary_table",
                protocol=sys.getsizeof,  # not a connection
                errclass=TypeError,
                errmsg="Unrecognised connection .* no 'execute' or 'cursor' method",
            ),
            id="Invalid read DB kwargs",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                query="/* tag: misc */ INSERT INTO xyz VALUES ('polars')",
                protocol=sqlite3.connect(":memory:"),
                errclass=UnsuitableSQLError,
                errmsg="INSERT statements are not valid 'read' queries",
            ),
            id="Invalid statement type",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                query="DELETE FROM xyz WHERE id = 'polars'",
                protocol=sqlite3.connect(":memory:"),
                errclass=UnsuitableSQLError,
                errmsg="DELETE statements are not valid 'read' queries",
            ),
            id="Invalid statement type",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                query="SELECT * FROM sqlite_master",
                protocol=sqlite3.connect(":memory:"),
                errclass=ValueError,
                kwargs={"iter_batches": True},
                errmsg="Cannot set `iter_batches` without also setting a non-zero `batch_size`",
            ),
            id="Invalid batch_size",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                engine="adbc",
                query="SELECT * FROM test_data",
                protocol=sqlite3.connect(":memory:"),
                errclass=TypeError,
                errmsg=r"unexpected keyword argument 'partition_on'",
                kwargs={"partition_on": "id"},
            ),
            id="Invalid kwargs",
        ),
        pytest.param(
            *ExceptionTestParams(
                read_method="read_database",
                engine="adbc",
                query="SELECT * FROM test_data",
                protocol="{not:a, valid:odbc_string}",
                errclass=ValueError,
                errmsg=r"unable to identify string connection as valid ODBC",
            ),
            id="Invalid ODBC string",
        ),
    ],
)
def test_read_database_exceptions(
    read_method: str,
    query: str,
    protocol: Any,
    errclass: type[Exception],
    errmsg: str,
    engine: DbReadEngine | None,
    execute_options: dict[str, Any] | None,
    kwargs: dict[str, Any] | None,
) -> None:
    if read_method == "read_database_uri":
        conn = f"{protocol}://test" if isinstance(protocol, str) else protocol
        params = {"uri": conn, "query": query, "engine": engine}
    else:
        params = {"connection": protocol, "query": query}
        if execute_options:
            params["execute_options"] = execute_options
        if kwargs is not None:
            params.update(kwargs)

    read_database = getattr(pl, read_method)
    with pytest.raises(errclass, match=errmsg):
        read_database(**params)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1, 1 FROM test_data",
        'SELECT 1 AS "n", 2 AS "n" FROM test_data',
        'SELECT name, value AS "name" FROM test_data',
    ],
)
def test_read_database_duplicate_column_error(tmp_sqlite_db: Path, query: str) -> None:
    alchemy_conn = create_engine(f"sqlite:///{tmp_sqlite_db}").connect()
    with pytest.raises(
        DuplicateError,
        match="column .+ appears more than once in the query/result cursor",
    ):
        pl.read_database(query, connection=alchemy_conn)


@pytest.mark.parametrize(
    "uri",
    [
        "fakedb://123:456@account/database/schema?warehouse=warehouse&role=role",
        "fakedb://my#%us3r:p433w0rd@not_a_real_host:9999/database",
    ],
)
def test_read_database_cx_credentials(uri: str) -> None:
    with pytest.raises(RuntimeError, match=r"Source.*not supported"):
        pl.read_database_uri("SELECT * FROM data", uri=uri, engine="connectorx")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="kuzu segfaults on windows: https://github.com/pola-rs/polars/actions/runs/12502055945/job/34880479875?pr=20462",
)
@pytest.mark.write_disk
def test_read_kuzu_graph_database(tmp_path: Path, io_files_path: Path) -> None:
    import kuzu

    tmp_path.mkdir(exist_ok=True)
    if (kuzu_test_db := (tmp_path / "kuzu_test.db")).exists():
        kuzu_test_db.unlink()

    test_db = str(kuzu_test_db).replace("\\", "/")

    db = kuzu.Database(test_db)
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE User(name STRING, age UINT64, PRIMARY KEY (name))")
    conn.execute("CREATE REL TABLE Follows(FROM User TO User, since INT64)")

    users = str(io_files_path / "graph-data" / "user.csv").replace("\\", "/")
    follows = str(io_files_path / "graph-data" / "follows.csv").replace("\\", "/")

    conn.execute(f'COPY User FROM "{users}"')
    conn.execute(f'COPY Follows FROM "{follows}"')

    # basic: single relation
    df1 = pl.read_database(
        query="MATCH (u:User) RETURN u.name, u.age",
        connection=conn,
    )
    assert_frame_equal(
        df1,
        pl.DataFrame(
            {
                "u.name": ["Adam", "Karissa", "Zhang", "Noura"],
                "u.age": [30, 40, 50, 25],
            },
            schema={"u.name": pl.Utf8, "u.age": pl.UInt64},
        ),
    )

    # join: connected edges/relations
    df2 = pl.read_database(
        query="MATCH (a:User)-[f:Follows]->(b:User) RETURN a.name, f.since, b.name",
        connection=conn,
        schema_overrides={"f.since": pl.Int16},
    )
    assert_frame_equal(
        df2,
        pl.DataFrame(
            {
                "a.name": ["Adam", "Adam", "Karissa", "Zhang"],
                "f.since": [2020, 2020, 2021, 2022],
                "b.name": ["Karissa", "Zhang", "Zhang", "Noura"],
            },
            schema={"a.name": pl.Utf8, "f.since": pl.Int16, "b.name": pl.Utf8},
        ),
    )

    # empty: no results for the given query
    df3 = pl.read_database(
        query="MATCH (a:User)-[f:Follows]->(b:User) WHERE a.name = '🔎️' RETURN a.name, f.since, b.name",
        connection=conn,
    )
    assert_frame_equal(
        df3,
        pl.DataFrame(
            schema={"a.name": pl.Utf8, "f.since": pl.Int64, "b.name": pl.Utf8}
        ),
    )


def test_sqlalchemy_row_init(tmp_sqlite_db: Path) -> None:
    expected_frame = pl.DataFrame(
        {
            "id": [1, 2],
            "name": ["misc", "other"],
            "value": [100.0, -99.5],
            "date": ["2020-01-01", "2021-12-31"],
        }
    )
    alchemy_engine = create_engine(f"sqlite:///{tmp_sqlite_db}")
    query = text("SELECT * FROM test_data ORDER BY name")

    with alchemy_engine.connect() as conn:
        # note: sqlalchemy `Row` is a NamedTuple-like object; it additionally has
        # a `_mapping` attribute that returns a `RowMapping` dict-like object. we
        # validate frame/series init from each flavour of query result.
        query_result = list(conn.execute(query))
        for df in (
            pl.DataFrame(query_result),
            pl.DataFrame([row._mapping for row in query_result]),
            pl.from_records([row._mapping for row in query_result]),
        ):
            assert_frame_equal(expected_frame, df)

        expected_series = expected_frame.to_struct()
        for s in (
            pl.Series(query_result),
            pl.Series([row._mapping for row in query_result]),
        ):
            assert_series_equal(expected_series, s)
