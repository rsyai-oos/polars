import polars as pl
from polars.testing import assert_frame_equal


def test_right_join_schemas() -> None:
    a = pl.DataFrame({"a": [1, 2, 3], "b": [1, 2, 3]})

    b = pl.DataFrame({"a": [1, 3], "b": [1, 3], "c": [1, 3]})

    # coalesces the join key, so the key of the right table remains
    assert a.join(
        b, on="a", how="right", coalesce=True, maintain_order="right"
    ).to_dict(as_series=False) == {
        "b": [1, 3],
        "a": [1, 3],
        "b_right": [1, 3],
        "c": [1, 3],
    }
    # doesn't coalesce the join key, so all columns remain
    assert a.join(b, on="a", how="right", coalesce=False).columns == [
        "a",
        "b",
        "a_right",
        "b_right",
        "c",
    ]

    # coalesces the join key, so the key of the right table remains
    assert_frame_equal(
        b.join(a, on="a", how="right", coalesce=True),
        pl.DataFrame(
            {
                "b": [1, None, 3],
                "c": [1, None, 3],
                "a": [1, 2, 3],
                "b_right": [1, 2, 3],
            }
        ),
        check_row_order=False,
    )
    assert b.join(a, on="a", how="right", coalesce=False).columns == [
        "a",
        "b",
        "c",
        "a_right",
        "b_right",
    ]

    a_ = a.lazy()
    b_ = b.lazy()
    assert list(
        a_.join(b_, on="a", how="right", coalesce=True).collect_schema().keys()
    ) == ["b", "a", "b_right", "c"]
    assert list(
        a_.join(b_, on="a", how="right", coalesce=False).collect_schema().keys()
    ) == ["a", "b", "a_right", "b_right", "c"]
    assert list(
        b_.join(a_, on="a", how="right", coalesce=True).collect_schema().keys()
    ) == ["b", "c", "a", "b_right"]
    assert list(
        b_.join(a_, on="a", how="right", coalesce=False).collect_schema().keys()
    ) == ["a", "b", "c", "a_right", "b_right"]


def test_right_join_schemas_multikey() -> None:
    a = pl.DataFrame({"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]})

    b = pl.DataFrame({"a": [1, 3], "b": [1, 3], "c": [1, 3]})
    assert a.join(b, on=["a", "b"], how="right", coalesce=False).columns == [
        "a",
        "b",
        "c",
        "a_right",
        "b_right",
        "c_right",
    ]
    assert_frame_equal(
        a.join(b, on=["a", "b"], how="right", coalesce=True),
        pl.DataFrame({"c": [1, 3], "a": [1, 3], "b": [1, 3], "c_right": [1, 3]}),
        check_row_order=False,
    )
    assert_frame_equal(
        b.join(a, on=["a", "b"], how="right", coalesce=True),
        pl.DataFrame(
            {"c": [1, None, 3], "a": [1, 2, 3], "b": [1, 2, 3], "c_right": [1, 2, 3]}
        ),
        check_row_order=False,
    )


def test_join_right_different_key() -> None:
    df = pl.DataFrame(
        {
            "foo": [1, 2, 3],
            "bar": [6.0, 7.0, 8.0],
            "ham1": ["a", "b", "c"],
        }
    )
    other_df = pl.DataFrame(
        {
            "apple": ["x", "y", "z"],
            "ham2": ["a", "b", "d"],
        }
    )
    assert df.join(
        other_df, left_on="ham1", right_on="ham2", how="right", maintain_order="right"
    ).to_dict(as_series=False) == {
        "foo": [1, 2, None],
        "bar": [6.0, 7.0, None],
        "apple": ["x", "y", "z"],
        "ham2": ["a", "b", "d"],
    }


def test_join_right_different_multikey() -> None:
    left = pl.LazyFrame({"a": [1, 2], "b": [1, 2]})
    right = pl.LazyFrame({"c": [1, 2], "d": [1, 2]})
    result = left.join(right, left_on=["a", "b"], right_on=["c", "d"], how="right")
    expected = pl.DataFrame({"c": [1, 2], "d": [1, 2]})
    assert_frame_equal(result.collect(), expected, check_row_order=False)
    assert result.collect_schema() == expected.schema
