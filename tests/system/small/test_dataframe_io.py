# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Tuple

import google.api_core.exceptions
import pandas as pd
import pyarrow as pa
import pytest

from tests.system import utils

try:
    import pandas_gbq  # type: ignore
except ImportError:  # pragma: NO COVER
    # TODO(b/332758806): Run system tests without "extras"
    pandas_gbq = None

import typing

import bigframes
import bigframes.dataframe
import bigframes.features
import bigframes.pandas as bpd


def test_to_pandas_w_correct_dtypes(scalars_df_default_index):
    """Verify to_pandas() APIs returns the expected dtypes."""
    actual = scalars_df_default_index.to_pandas().dtypes
    expected = scalars_df_default_index.dtypes

    pd.testing.assert_series_equal(actual, expected)


def test_to_pandas_array_struct_correct_result(session):
    """In future, we should support arrays with arrow types.
    For now we fall back to the current connector behavior of converting
    to Python objects"""
    df = session.read_gbq(
        """SELECT
        [1, 3, 2] AS array_column,
        STRUCT(
            "a" AS string_field,
            1.2 AS float_field) AS struct_column"""
    )

    result = df.to_pandas()
    expected = pd.DataFrame(
        {
            "array_column": pd.Series(
                [[1, 3, 2]],
                dtype=(
                    pd.ArrowDtype(pa.list_(pa.int64()))
                    if bigframes.features.PANDAS_VERSIONS.is_arrow_list_dtype_usable
                    else "object"
                ),
            ),
            "struct_column": pd.Series(
                [{"string_field": "a", "float_field": 1.2}],
                dtype=pd.ArrowDtype(
                    pa.struct(
                        [
                            ("string_field", pa.string()),
                            ("float_field", pa.float64()),
                        ]
                    )
                ),
            ),
        }
    )
    expected.index = expected.index.astype("Int64")
    pd.testing.assert_series_equal(result.dtypes, expected.dtypes)
    pd.testing.assert_series_equal(result["array_column"], expected["array_column"])
    # assert_series_equal not implemented for struct columns yet. Compare
    # values as Python objects, instead.
    pd.testing.assert_series_equal(
        result["struct_column"].astype("O"), expected["struct_column"].astype("O")
    )


def test_load_json(session):
    df = session.read_gbq(
        """SELECT
        JSON_OBJECT('foo', 10, 'bar', TRUE) AS json_column
        """
    )

    result = df.to_pandas()
    expected = pd.DataFrame(
        {
            "json_column": ['{"bar":true,"foo":10}'],
        },
        dtype=pd.StringDtype(storage="pyarrow"),
    )
    expected.index = expected.index.astype("Int64")
    pd.testing.assert_series_equal(result.dtypes, expected.dtypes)
    pd.testing.assert_series_equal(result["json_column"], expected["json_column"])


def test_to_pandas_batches_w_correct_dtypes(scalars_df_default_index):
    """Verify to_pandas_batches() APIs returns the expected dtypes."""
    expected = scalars_df_default_index.dtypes
    for df in scalars_df_default_index.to_pandas_batches():
        actual = df.dtypes
        pd.testing.assert_series_equal(actual, expected)


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
def test_to_csv_index(
    scalars_dfs: Tuple[bigframes.dataframe.DataFrame, pd.DataFrame],
    gcs_folder: str,
    index: bool,
):
    if pd.__version__.startswith("1."):
        pytest.skip("date_format parameter not supported in pandas 1.x.")
    """Test the `to_csv` API with the `index` parameter."""
    scalars_df, scalars_pandas_df = scalars_dfs
    index_col = None
    path = gcs_folder + f"test_index_df_to_csv_index_{index}*.csv"
    if index:
        index_col = typing.cast(str, scalars_df.index.name)

    # TODO(swast): Support "date_format" parameter and make sure our
    # DATETIME/TIMESTAMP column export is the same format as pandas by default.
    scalars_df.to_csv(path, index=index)

    # Pandas dataframes dtypes from read_csv are not fully compatible with
    # BigQuery-backed dataframes, so manually convert the dtypes specifically
    # here.
    dtype = scalars_df.reset_index().dtypes.to_dict()
    dtype.pop("geography_col")
    dtype.pop("rowindex")
    # read_csv will decode into bytes inproperly, convert_pandas_dtypes will encode properly from string
    dtype.pop("bytes_col")
    gcs_df = pd.read_csv(
        utils.get_first_file_from_wildcard(path),
        dtype=dtype,
        date_format={"timestamp_col": "YYYY-MM-DD HH:MM:SS Z"},
        index_col=index_col,
    )
    utils.convert_pandas_dtypes(gcs_df, bytes_col=True)
    gcs_df.index.name = scalars_df.index.name

    scalars_pandas_df = scalars_pandas_df.copy()
    scalars_pandas_df.index = scalars_pandas_df.index.astype("int64")
    # Ordering should be maintained for tables smaller than 1 GB.
    pd.testing.assert_frame_equal(gcs_df, scalars_pandas_df)


def test_to_csv_tabs(
    scalars_dfs: Tuple[bigframes.dataframe.DataFrame, pd.DataFrame],
    gcs_folder: str,
):
    if pd.__version__.startswith("1."):
        pytest.skip("date_format parameter not supported in pandas 1.x.")
    """Test the `to_csv` API with the `sep` parameter."""
    scalars_df, scalars_pandas_df = scalars_dfs
    index_col = typing.cast(str, scalars_df.index.name)
    path = gcs_folder + "test_to_csv_tabs*.csv"

    # TODO(swast): Support "date_format" parameter and make sure our
    # DATETIME/TIMESTAMP column export is the same format as pandas by default.
    scalars_df.to_csv(path, sep="\t", index=True)

    # Pandas dataframes dtypes from read_csv are not fully compatible with
    # BigQuery-backed dataframes, so manually convert the dtypes specifically
    # here.
    dtype = scalars_df.reset_index().dtypes.to_dict()
    dtype.pop("geography_col")
    dtype.pop("rowindex")
    # read_csv will decode into bytes inproperly, convert_pandas_dtypes will encode properly from string
    dtype.pop("bytes_col")
    gcs_df = pd.read_csv(
        utils.get_first_file_from_wildcard(path),
        sep="\t",
        dtype=dtype,
        date_format={"timestamp_col": "YYYY-MM-DD HH:MM:SS Z"},
        index_col=index_col,
    )
    utils.convert_pandas_dtypes(gcs_df, bytes_col=True)
    gcs_df.index.name = scalars_df.index.name

    scalars_pandas_df = scalars_pandas_df.copy()
    scalars_pandas_df.index = scalars_pandas_df.index.astype("int64")

    # Ordering should be maintained for tables smaller than 1 GB.
    pd.testing.assert_frame_equal(gcs_df, scalars_pandas_df)


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
@pytest.mark.skipif(pandas_gbq is None, reason="required by pd.read_gbq")
def test_to_gbq_index(scalars_dfs, dataset_id, index):
    """Test the `to_gbq` API with the `index` parameter."""
    scalars_df, scalars_pandas_df = scalars_dfs
    destination_table = f"{dataset_id}.test_index_df_to_gbq_{index}"
    df_in = scalars_df.copy()
    if index:
        index_col = "index"
        df_in.index.name = index_col
    else:
        index_col = None

    df_in.to_gbq(destination_table, if_exists="replace", index=index)
    df_out = pd.read_gbq(destination_table, index_col=index_col)

    if index:
        df_out = df_out.sort_index()
    else:
        df_out = df_out.sort_values("rowindex_2").reset_index(drop=True)

    utils.convert_pandas_dtypes(df_out, bytes_col=False)
    # pd.read_gbq interpets bytes_col as object, reconvert to pyarrow binary
    df_out["bytes_col"] = df_out["bytes_col"].astype(pd.ArrowDtype(pa.binary()))
    expected = scalars_pandas_df.copy()
    expected.index.name = index_col
    pd.testing.assert_frame_equal(df_out, expected, check_index_type=False)


@pytest.mark.parametrize(
    ("if_exists", "expected_index"),
    [
        pytest.param("replace", 1),
        pytest.param("append", 2),
        pytest.param(
            "fail",
            0,
            marks=pytest.mark.xfail(
                raises=google.api_core.exceptions.Conflict,
            ),
        ),
        pytest.param(
            "unknown",
            0,
            marks=pytest.mark.xfail(
                raises=ValueError,
            ),
        ),
    ],
)
@pytest.mark.skipif(pandas_gbq is None, reason="required by pd.read_gbq")
def test_to_gbq_if_exists(
    scalars_df_default_index,
    scalars_pandas_df_default_index,
    dataset_id,
    if_exists,
    expected_index,
):
    """Test the `to_gbq` API with the `if_exists` parameter."""
    destination_table = f"{dataset_id}.test_to_gbq_if_exists_{if_exists}"

    scalars_df_default_index.to_gbq(destination_table)
    scalars_df_default_index.to_gbq(destination_table, if_exists=if_exists)

    gcs_df = pd.read_gbq(destination_table)
    assert len(gcs_df.index) == expected_index * len(
        scalars_pandas_df_default_index.index
    )
    pd.testing.assert_index_equal(
        gcs_df.columns, scalars_pandas_df_default_index.columns
    )


def test_to_gbq_w_duplicate_column_names(
    scalars_df_index, scalars_pandas_df_index, dataset_id
):
    """Test the `to_gbq` API when dealing with duplicate column names."""
    destination_table = f"{dataset_id}.test_to_gbq_w_duplicate_column_names"

    # Renaming 'int64_too' to 'int64_col', which will result in 'int64_too'
    # becoming 'int64_col_1' after deduplication.
    scalars_df_index = scalars_df_index.rename(columns={"int64_too": "int64_col"})
    scalars_df_index.to_gbq(destination_table, if_exists="replace")

    bf_result = bpd.read_gbq(destination_table, index_col="rowindex").to_pandas()

    pd.testing.assert_series_equal(
        scalars_pandas_df_index["int64_col"], bf_result["int64_col"]
    )
    pd.testing.assert_series_equal(
        scalars_pandas_df_index["int64_too"],
        bf_result["int64_col_1"],
        check_names=False,
    )


def test_to_gbq_w_None_column_names(
    scalars_df_index, scalars_pandas_df_index, dataset_id
):
    """Test the `to_gbq` API with None as a column name."""
    destination_table = f"{dataset_id}.test_to_gbq_w_none_column_names"

    scalars_df_index = scalars_df_index.rename(columns={"int64_too": None})
    scalars_df_index.to_gbq(destination_table, if_exists="replace")

    bf_result = bpd.read_gbq(destination_table, index_col="rowindex").to_pandas()

    pd.testing.assert_series_equal(
        scalars_pandas_df_index["int64_col"], bf_result["int64_col"]
    )
    pd.testing.assert_series_equal(
        scalars_pandas_df_index["int64_too"],
        bf_result["bigframes_unnamed_column"],
        check_names=False,
    )


@pytest.mark.parametrize(
    "clustering_columns",
    [
        pytest.param(["int64_col", "geography_col"]),
        pytest.param(
            ["float64_col"],
            marks=pytest.mark.xfail(raises=google.api_core.exceptions.BadRequest),
        ),
        pytest.param(
            ["int64_col", "int64_col"],
            marks=pytest.mark.xfail(raises=ValueError),
        ),
    ],
)
def test_to_gbq_w_clustering(
    scalars_df_default_index,
    dataset_id,
    bigquery_client,
    clustering_columns,
):
    """Test the `to_gbq` API for creating clustered tables."""
    destination_table = (
        f"{dataset_id}.test_to_gbq_clustering_{'_'.join(clustering_columns)}"
    )

    scalars_df_default_index.to_gbq(
        destination_table, clustering_columns=clustering_columns
    )
    table = bigquery_client.get_table(destination_table)

    assert list(table.clustering_fields) == clustering_columns
    assert table.expires is None


def test_to_gbq_w_clustering_no_destination(
    scalars_df_default_index,
    bigquery_client,
):
    """Test the `to_gbq` API for creating clustered tables without destination."""
    clustering_columns = ["int64_col", "geography_col"]
    destination_table = scalars_df_default_index.to_gbq(
        clustering_columns=clustering_columns
    )
    table = bigquery_client.get_table(destination_table)

    assert list(table.clustering_fields) == clustering_columns
    assert table.expires is not None


def test_to_gbq_w_invalid_destination_table(scalars_df_index):
    with pytest.raises(ValueError):
        scalars_df_index.to_gbq("table_id")


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
def test_to_json_index_invalid_orient(
    scalars_dfs: Tuple[bigframes.dataframe.DataFrame, pd.DataFrame],
    gcs_folder: str,
    index: bool,
):
    scalars_df, _ = scalars_dfs
    path = gcs_folder + f"test_index_df_to_json_index_{index}*.jsonl"
    with pytest.raises(ValueError):
        scalars_df.to_json(path, index=index, lines=True)


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
def test_to_json_index_invalid_lines(
    scalars_dfs: Tuple[bigframes.dataframe.DataFrame, pd.DataFrame],
    gcs_folder: str,
    index: bool,
):
    scalars_df, _ = scalars_dfs
    path = gcs_folder + f"test_index_df_to_json_index_{index}.jsonl"
    with pytest.raises(NotImplementedError):
        scalars_df.to_json(path, index=index)


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
def test_to_json_index_records_orient(
    scalars_dfs: Tuple[bigframes.dataframe.DataFrame, pd.DataFrame],
    gcs_folder: str,
    index: bool,
):
    """Test the `to_json` API with the `index` parameter.

    Uses the scalable options orient='records' and lines=True.
    """
    scalars_df, scalars_pandas_df = scalars_dfs
    path = gcs_folder + f"test_index_df_to_json_index_{index}*.jsonl"

    scalars_df.to_json(path, index=index, orient="records", lines=True)

    gcs_df = pd.read_json(
        utils.get_first_file_from_wildcard(path),
        lines=True,
        convert_dates=["datetime_col"],
    )
    utils.convert_pandas_dtypes(gcs_df, bytes_col=True)
    if index and scalars_df.index.name is not None:
        gcs_df = gcs_df.set_index(scalars_df.index.name)

    assert len(gcs_df.index) == len(scalars_pandas_df.index)
    pd.testing.assert_index_equal(gcs_df.columns, scalars_pandas_df.columns)

    gcs_df.index.name = scalars_df.index.name
    gcs_df.index = gcs_df.index.astype("Int64")
    scalars_pandas_df.index = scalars_pandas_df.index.astype("Int64")

    # Ordering should be maintained for tables smaller than 1 GB.
    pd.testing.assert_frame_equal(gcs_df, scalars_pandas_df)


@pytest.mark.parametrize(
    ("index"),
    [True, False],
)
def test_to_parquet_index(scalars_dfs, gcs_folder, index):
    """Test the `to_parquet` API with the `index` parameter."""
    scalars_df, scalars_pandas_df = scalars_dfs
    scalars_pandas_df = scalars_pandas_df.copy()
    path = gcs_folder + f"test_index_df_to_parquet_{index}*.parquet"

    # TODO(b/268693993): Type GEOGRAPHY is not currently supported for parquet.
    scalars_df = scalars_df.drop(columns="geography_col")
    scalars_pandas_df = scalars_pandas_df.drop(columns="geography_col")

    # TODO(swast): Do a bit more processing on the input DataFrame to ensure
    # the exported results are from the generated query, not just the source
    # table.
    scalars_df.to_parquet(path, index=index)

    gcs_df = pd.read_parquet(utils.get_first_file_from_wildcard(path))
    utils.convert_pandas_dtypes(gcs_df, bytes_col=False)
    if index and scalars_df.index.name is not None:
        gcs_df = gcs_df.set_index(scalars_df.index.name)

    assert len(gcs_df.index) == len(scalars_pandas_df.index)
    pd.testing.assert_index_equal(gcs_df.columns, scalars_pandas_df.columns)

    gcs_df.index.name = scalars_df.index.name
    gcs_df.index = gcs_df.index.astype("Int64")
    scalars_pandas_df.index = scalars_pandas_df.index.astype("Int64")

    # Ordering should be maintained for tables smaller than 1 GB.
    pd.testing.assert_frame_equal(
        gcs_df.drop("bytes_col", axis=1), scalars_pandas_df.drop("bytes_col", axis=1)
    )


def test_to_sql_query_unnamed_index_included(
    session: bigframes.Session,
    scalars_df_default_index: bpd.DataFrame,
    scalars_pandas_df_default_index: pd.DataFrame,
):
    bf_df = scalars_df_default_index.reset_index(drop=True)
    sql, idx_ids, idx_labels = bf_df._to_sql_query(include_index=True)
    assert len(idx_labels) == 1
    assert len(idx_ids) == 1
    assert idx_labels[0] is None
    assert idx_ids[0].startswith("bigframes")

    pd_df = scalars_pandas_df_default_index.reset_index(drop=True)
    roundtrip = session.read_gbq(sql, index_col=idx_ids)
    roundtrip.index.names = [None]
    utils.assert_pandas_df_equal(roundtrip.to_pandas(), pd_df, check_index_type=False)


def test_to_sql_query_named_index_included(
    session: bigframes.Session,
    scalars_df_default_index: bpd.DataFrame,
    scalars_pandas_df_default_index: pd.DataFrame,
):
    bf_df = scalars_df_default_index.set_index("rowindex_2", drop=True)
    sql, idx_ids, idx_labels = bf_df._to_sql_query(include_index=True)
    assert len(idx_labels) == 1
    assert len(idx_ids) == 1
    assert idx_labels[0] == "rowindex_2"
    assert idx_ids[0] == "rowindex_2"

    pd_df = scalars_pandas_df_default_index.set_index("rowindex_2", drop=True)
    roundtrip = session.read_gbq(sql, index_col=idx_ids)
    utils.assert_pandas_df_equal(roundtrip.to_pandas(), pd_df)


def test_to_sql_query_unnamed_index_excluded(
    session: bigframes.Session,
    scalars_df_default_index: bpd.DataFrame,
    scalars_pandas_df_default_index: pd.DataFrame,
):
    bf_df = scalars_df_default_index.reset_index(drop=True)
    sql, idx_ids, idx_labels = bf_df._to_sql_query(include_index=False)
    assert len(idx_labels) == 0
    assert len(idx_ids) == 0

    pd_df = scalars_pandas_df_default_index.reset_index(drop=True)
    roundtrip = session.read_gbq(sql)
    utils.assert_pandas_df_equal(
        roundtrip.to_pandas(), pd_df, check_index_type=False, ignore_order=True
    )


def test_to_sql_query_named_index_excluded(
    session: bigframes.Session,
    scalars_df_default_index: bpd.DataFrame,
    scalars_pandas_df_default_index: pd.DataFrame,
):
    bf_df = scalars_df_default_index.set_index("rowindex_2", drop=True)
    sql, idx_ids, idx_labels = bf_df._to_sql_query(include_index=False)
    assert len(idx_labels) == 0
    assert len(idx_ids) == 0

    pd_df = scalars_pandas_df_default_index.set_index(
        "rowindex_2", drop=True
    ).reset_index(drop=True)
    roundtrip = session.read_gbq(sql)
    utils.assert_pandas_df_equal(
        roundtrip.to_pandas(), pd_df, check_index_type=False, ignore_order=True
    )
