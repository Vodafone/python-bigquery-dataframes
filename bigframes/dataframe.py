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

"""DataFrame is a two dimensional data structure."""

from __future__ import annotations

import datetime
import inspect
import re
import sys
import textwrap
import typing
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import bigframes_vendored.pandas.core.frame as vendored_pandas_frame
import bigframes_vendored.pandas.pandas._typing as vendored_pandas_typing
import google.api_core.exceptions
import google.cloud.bigquery as bigquery
import numpy
import pandas
import tabulate

import bigframes
import bigframes._config.display_options as display_options
import bigframes.constants as constants
import bigframes.core
from bigframes.core import log_adapter
import bigframes.core.block_transforms as block_ops
import bigframes.core.blocks as blocks
import bigframes.core.convert
import bigframes.core.expression as ex
import bigframes.core.groupby as groupby
import bigframes.core.guid
import bigframes.core.indexers as indexers
import bigframes.core.indexes as indexes
import bigframes.core.ordering as order
import bigframes.core.utils as utils
import bigframes.core.window
import bigframes.dtypes
import bigframes.formatting_helpers as formatter
import bigframes.operations as ops
import bigframes.operations.aggregations as agg_ops
import bigframes.operations.plotting as plotting
import bigframes.series
import bigframes.series as bf_series
import bigframes.session._io.bigquery

if typing.TYPE_CHECKING:
    import bigframes.session

    SingleItemValue = Union[bigframes.series.Series, int, float, Callable]

LevelType = typing.Hashable
LevelsType = typing.Union[LevelType, typing.Sequence[LevelType]]

ERROR_IO_ONLY_GS_PATHS = f"Only Google Cloud Storage (gs://...) paths are supported. {constants.FEEDBACK_LINK}"
ERROR_IO_REQUIRES_WILDCARD = (
    "Google Cloud Storage path must contain a wildcard '*' character. See: "
    "https://cloud.google.com/bigquery/docs/reference/standard-sql/other-statements#export_data_statement"
    f"{constants.FEEDBACK_LINK}"
)


# Inherits from pandas DataFrame so that we can use the same docstrings.
@log_adapter.class_logger
class DataFrame(vendored_pandas_frame.DataFrame):
    __doc__ = vendored_pandas_frame.DataFrame.__doc__

    def __init__(
        self,
        data=None,
        index: vendored_pandas_typing.Axes | None = None,
        columns: vendored_pandas_typing.Axes | None = None,
        dtype: typing.Optional[
            bigframes.dtypes.DtypeString | bigframes.dtypes.Dtype
        ] = None,
        copy: typing.Optional[bool] = None,
        *,
        session: typing.Optional[bigframes.session.Session] = None,
    ):
        if copy is not None and not copy:
            raise ValueError(
                f"DataFrame constructor only supports copy=True. {constants.FEEDBACK_LINK}"
            )

        # Check to see if constructing from BigQuery-backed objects before
        # falling back to pandas constructor
        block = None
        if isinstance(data, blocks.Block):
            block = data

        elif isinstance(data, DataFrame):
            block = data._get_block()

        # Dict of Series
        elif (
            utils.is_dict_like(data)
            and len(data) >= 1
            and any(isinstance(data[key], bf_series.Series) for key in data.keys())
        ):
            if not all(isinstance(data[key], bf_series.Series) for key in data.keys()):
                # TODO(tbergeron): Support local list/series data by converting to memtable.
                raise NotImplementedError(
                    f"Cannot mix Series with other types. {constants.FEEDBACK_LINK}"
                )
            keys = list(data.keys())
            first_label, first_series = keys[0], data[keys[0]]
            block = (
                typing.cast(bf_series.Series, first_series)
                ._get_block()
                .with_column_labels([first_label])
            )

            for key in keys[1:]:
                other = typing.cast(bf_series.Series, data[key])
                other_block = other._block.with_column_labels([key])
                # Pandas will keep original sorting if all indices are aligned.
                # We cannot detect this easily however, and so always sort on index
                block, _ = block.join(  # type:ignore
                    other_block, how="outer", sort=True
                )

        if block:
            if index is not None:
                bf_index = indexes.Index(index)
                idx_block = bf_index._block
                idx_cols = idx_block.index_columns
                block, (_, r_mapping) = block.reset_index().join(
                    bf_index._block.reset_index(), how="inner"
                )
                block = block.set_index([r_mapping[idx_col] for idx_col in idx_cols])
            if columns:
                block = block.select_columns(list(columns))  # type:ignore
            if dtype:
                block = block.multi_apply_unary_op(
                    block.value_columns, ops.AsTypeOp(to_type=dtype)
                )
            self._block = block

        else:
            import bigframes.pandas

            pd_dataframe = pandas.DataFrame(
                data=data,
                index=index,  # type:ignore
                columns=columns,  # type:ignore
                dtype=dtype,  # type:ignore
            )
            if session:
                self._block = session.read_pandas(pd_dataframe)._get_block()
            else:
                self._block = bigframes.pandas.read_pandas(pd_dataframe)._get_block()
        self._query_job: Optional[bigquery.QueryJob] = None

    def __dir__(self):
        return dir(type(self)) + [
            label
            for label in self._block.column_labels
            if label and isinstance(label, str)
        ]

    def _ipython_key_completions_(self) -> List[str]:
        return list(
            [
                label
                for label in self._block.column_labels
                if label and isinstance(label, str)
            ]
        )

    def _find_indices(
        self,
        columns: Union[blocks.Label, Sequence[blocks.Label]],
        tolerance: bool = False,
    ) -> Sequence[int]:
        """Find corresponding indices in df._block.column_labels for column name(s).
        Order is kept the same as input names order.

        Args:
            columns: column name(s)
            tolerance: True to pass through columns not found. False to raise
                ValueError.
        """
        col_ids = self._sql_names(columns, tolerance)
        return [self._block.value_columns.index(col_id) for col_id in col_ids]

    def _resolve_label_exact(self, label) -> Optional[str]:
        """Returns the column id matching the label if there is exactly
        one such column. If there are multiple columns with the same name,
        raises an error. If there is no such column, returns None."""
        matches = self._block.label_to_col_id.get(label, [])
        if len(matches) > 1:
            raise ValueError(
                f"Multiple columns matching id {label} were found. {constants.FEEDBACK_LINK}"
            )
        return matches[0] if len(matches) != 0 else None

    def _sql_names(
        self,
        columns: Union[blocks.Label, Sequence[blocks.Label], pandas.Index],
        tolerance: bool = False,
    ) -> Sequence[str]:
        """Retrieve sql name (column name in BQ schema) of column(s)."""
        labels = (
            columns
            if utils.is_list_like(columns) and not isinstance(columns, tuple)
            else [columns]
        )  # type:ignore
        results: Sequence[str] = []
        for label in labels:
            col_ids = self._block.label_to_col_id.get(label, [])
            if not tolerance and len(col_ids) == 0:
                raise ValueError(f"Column name {label} doesn't exist")
            results = (*results, *col_ids)
        return results

    @property
    def index(
        self,
    ) -> indexes.Index:
        return indexes.Index.from_frame(self)

    @index.setter
    def index(self, value):
        # TODO: Handle assigning MultiIndex
        result = self._assign_single_item("_new_bf_index", value).set_index(
            "_new_bf_index"
        )
        self._set_block(result._get_block())
        self.index.name = value.name if hasattr(value, "name") else None

    @property
    def loc(self) -> indexers.LocDataFrameIndexer:
        return indexers.LocDataFrameIndexer(self)

    @property
    def iloc(self) -> indexers.ILocDataFrameIndexer:
        return indexers.ILocDataFrameIndexer(self)

    @property
    def iat(self) -> indexers.IatDataFrameIndexer:
        return indexers.IatDataFrameIndexer(self)

    @property
    def at(self) -> indexers.AtDataFrameIndexer:
        return indexers.AtDataFrameIndexer(self)

    @property
    def dtypes(self) -> pandas.Series:
        return pandas.Series(data=self._block.dtypes, index=self._block.column_labels)

    @property
    def columns(self) -> pandas.Index:
        return self.dtypes.index

    @columns.setter
    def columns(self, labels: pandas.Index):
        new_block = self._block.with_column_labels(labels)
        self._set_block(new_block)

    @property
    def shape(self) -> Tuple[int, int]:
        return self._block.shape

    @property
    def size(self) -> int:
        rows, cols = self.shape
        return rows * cols

    @property
    def ndim(self) -> int:
        return 2

    @property
    def empty(self) -> bool:
        return self.size == 0

    @property
    def values(self) -> numpy.ndarray:
        return self.to_numpy()

    @property
    def bqclient(self) -> bigframes.Session:
        """BigQuery REST API Client the DataFrame uses for operations."""
        return self._session.bqclient

    @property
    def _session(self) -> bigframes.Session:
        return self._get_block().expr.session

    def __len__(self):
        rows, _ = self.shape
        return rows

    __len__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__len__)

    def __iter__(self):
        return iter(self.columns)

    def astype(
        self,
        dtype: Union[bigframes.dtypes.DtypeString, bigframes.dtypes.Dtype],
    ) -> DataFrame:
        return self._apply_unary_op(ops.AsTypeOp(to_type=dtype))

    def _to_sql_query(
        self, include_index: bool
    ) -> Tuple[str, list[str], list[blocks.Label]]:
        """Compiles this DataFrame's expression tree to SQL, optionally
        including index columns.

        Args:
            include_index (bool):
                whether to include index columns.

        Returns:
            a tuple of (sql_string, index_column_id_list, index_column_label_list).
                If include_index is set to False, index_column_id_list and index_column_label_list
                return empty lists.
        """
        return self._block.to_sql_query(include_index)

    @property
    def sql(self) -> str:
        """Compiles this DataFrame's expression tree to SQL."""
        sql, _, _ = self._to_sql_query(include_index=False)
        return sql

    @property
    def query_job(self) -> Optional[bigquery.QueryJob]:
        """BigQuery job metadata for the most recent query.

        Returns:
            The most recent `QueryJob
            <https://cloud.google.com/python/docs/reference/bigquery/latest/google.cloud.bigquery.job.QueryJob>`_.
        """
        if self._query_job is None:
            self._set_internal_query_job(self._compute_dry_run())
        return self._query_job

    def memory_usage(self, index: bool = True):
        n_rows, _ = self.shape
        # like pandas, treat all variable-size objects as just 8-byte pointers, ignoring actual object
        column_sizes = self.dtypes.map(
            lambda dtype: bigframes.dtypes.DTYPE_BYTE_SIZES.get(dtype, 8) * n_rows
        )
        if index:
            index_size = pandas.Series([self.index._memory_usage()], index=["Index"])
            column_sizes = pandas.concat([index_size, column_sizes])
        return column_sizes

    def info(
        self,
        verbose: Optional[bool] = None,
        buf=None,
        max_cols: Optional[int] = None,
        memory_usage: Optional[bool] = None,
        show_counts: Optional[bool] = None,
    ):
        obuf = buf or sys.stdout

        n_rows, n_columns = self.shape

        max_cols = (
            max_cols
            if max_cols is not None
            else bigframes.options.display.max_info_columns
        )

        show_all_columns = verbose if verbose is not None else (n_columns < max_cols)

        obuf.write(f"{type(self)}\n")

        index_type = "MultiIndex" if self.index.nlevels > 1 else "Index"

        # These accessses are kind of expensive, maybe should try to skip?
        first_indice = self.index[0]
        last_indice = self.index[-1]
        obuf.write(f"{index_type}: {n_rows} entries, {first_indice} to {last_indice}\n")

        dtype_strings = self.dtypes.astype("string")
        if show_all_columns:
            obuf.write(f"Data columns (total {n_columns} columns):\n")
            column_info = self.columns.to_frame(name="Column")

            max_rows = bigframes.options.display.max_info_rows
            too_many_rows = n_rows > max_rows if max_rows is not None else False

            if show_counts if show_counts is not None else (not too_many_rows):
                non_null_counts = self.count().to_pandas()
                column_info["Non-Null Count"] = non_null_counts.map(
                    lambda x: f"{int(x)} non-null"
                )

            column_info["Dtype"] = dtype_strings

            column_info = column_info.reset_index(drop=True)
            column_info.index.name = "#"

            column_info_formatted = tabulate.tabulate(column_info, headers="keys")  # type: ignore
            obuf.write(column_info_formatted)
            obuf.write("\n")

        else:  # Just number of columns and first, last
            obuf.write(
                f"Columns: {n_columns} entries, {self.columns[0]} to {self.columns[-1]}\n"
            )
        dtype_counts = dtype_strings.value_counts().sort_index(ascending=True).items()
        dtype_counts_formatted = ", ".join(
            f"{dtype}({count})" for dtype, count in dtype_counts
        )
        obuf.write(f"dtypes: {dtype_counts_formatted}\n")

        show_memory = (
            memory_usage
            if memory_usage is not None
            else bigframes.options.display.memory_usage
        )
        if show_memory:
            # TODO: Convert to different units (kb, mb, etc.)
            obuf.write(f"memory usage: {self.memory_usage().sum()} bytes\n")

    def select_dtypes(self, include=None, exclude=None) -> DataFrame:
        # Create empty pandas dataframe with same schema and then leverage actual pandas implementation
        as_pandas = pandas.DataFrame(
            {
                col_id: pandas.Series([], dtype=dtype)
                for col_id, dtype in zip(self._block.value_columns, self._block.dtypes)
            }
        )
        selected_columns = tuple(
            as_pandas.select_dtypes(include=include, exclude=exclude).columns
        )
        return DataFrame(self._block.select_columns(selected_columns))

    def _set_internal_query_job(self, query_job: bigquery.QueryJob):
        self._query_job = query_job

    def __getitem__(
        self,
        key: Union[
            blocks.Label,
            Sequence[blocks.Label],
            # Index of column labels can be treated the same as a sequence of column labels.
            pandas.Index,
            bigframes.series.Series,
        ],
    ):  # No return type annotations (like pandas) as type cannot always be determined statically
        # NOTE: This implements the operations described in
        # https://pandas.pydata.org/docs/getting_started/intro_tutorials/03_subset_data.html

        if isinstance(key, bigframes.series.Series):
            return self._getitem_bool_series(key)

        if isinstance(key, typing.Hashable):
            return self._getitem_label(key)
        # Select a subset of columns or re-order columns.
        # In Ibis after you apply a projection, any column objects from the
        # table before the projection can't be combined with column objects
        # from the table after the projection. This is because the table after
        # a projection is considered a totally separate table expression.
        #
        # This is unexpected behavior for a pandas user, who expects their old
        # Series objects to still work with the new / mutated DataFrame. We
        # avoid applying a projection in Ibis until it's absolutely necessary
        # to provide pandas-like semantics.
        # TODO(swast): Do we need to apply implicit join when doing a
        # projection?

        # Select a number of columns as DF.
        key = key if utils.is_list_like(key) else [key]  # type:ignore

        selected_ids: Tuple[str, ...] = ()
        for label in key:
            col_ids = self._block.label_to_col_id[label]
            selected_ids = (*selected_ids, *col_ids)

        return DataFrame(self._block.select_columns(selected_ids))

    __getitem__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__getitem__)

    def _getitem_label(self, key: blocks.Label):
        col_ids = self._block.cols_matching_label(key)
        if len(col_ids) == 0:
            raise KeyError(key)
        block = self._block.select_columns(col_ids)
        if isinstance(self.columns, pandas.MultiIndex):
            # Multiindex should drop-level if not selecting entire
            key_levels = len(key) if isinstance(key, tuple) else 1
            index_levels = self.columns.nlevels
            if key_levels < index_levels:
                block = block.with_column_labels(
                    block.column_labels.droplevel(list(range(key_levels)))
                )
                # Force return DataFrame in this case, even if only single column
                return DataFrame(block)

        if len(col_ids) == 1:
            return bigframes.series.Series(block)
        return DataFrame(block)

    # Bool Series selects rows
    def _getitem_bool_series(self, key: bigframes.series.Series) -> DataFrame:
        if not key.dtype == pandas.BooleanDtype():
            raise NotImplementedError(
                f"Only boolean series currently supported for indexing. {constants.FEEDBACK_LINK}"
            )
            # TODO: enforce stricter alignment
        combined_index, (
            get_column_left,
            get_column_right,
        ) = self._block.join(key._block, how="left")
        block = combined_index
        filter_col_id = get_column_right[key._value_column]
        block = block.filter_by_id(filter_col_id)
        block = block.drop_columns([filter_col_id])
        return DataFrame(block)

    def __getattr__(self, key: str):
        if key in self._block.column_labels:
            return self.__getitem__(key)
        elif hasattr(pandas.DataFrame, key):
            raise AttributeError(
                textwrap.dedent(
                    f"""
                    BigQuery DataFrames has not yet implemented an equivalent to
                    'pandas.DataFrame.{key}'. {constants.FEEDBACK_LINK}
                    """
                )
            )
        else:
            raise AttributeError(key)

    def __setattr__(self, key: str, value):
        if key in ["_block", "_query_job"]:
            object.__setattr__(self, key, value)
            return
        # Can this be removed???
        try:
            # boring attributes go through boring old path
            object.__getattribute__(self, key)
            return object.__setattr__(self, key, value)
        except AttributeError:
            pass

        # if this fails, go on to more involved attribute setting
        # (note that this matches __getattr__, above).
        try:
            if key in self.columns:
                self[key] = value
            else:
                object.__setattr__(self, key, value)
        # Can this be removed?
        except (AttributeError, TypeError):
            object.__setattr__(self, key, value)

    def __repr__(self) -> str:
        """Converts a DataFrame to a string. Calls to_pandas.

        Only represents the first `bigframes.options.display.max_rows`.
        """
        opts = bigframes.options.display
        max_results = opts.max_rows
        if opts.repr_mode == "deferred":
            return formatter.repr_query_job(self.query_job)

        self._cached()
        # TODO(swast): pass max_columns and get the true column count back. Maybe
        # get 1 more column than we have requested so that pandas can add the
        # ... for us?
        pandas_df, row_count, query_job = self._block.retrieve_repr_request_results(
            max_results
        )

        self._set_internal_query_job(query_job)

        column_count = len(pandas_df.columns)

        with display_options.pandas_repr(opts):
            repr_string = repr(pandas_df)

        # Modify the end of the string to reflect count.
        lines = repr_string.split("\n")
        pattern = re.compile("\\[[0-9]+ rows x [0-9]+ columns\\]")
        if pattern.match(lines[-1]):
            lines = lines[:-2]

        if row_count > len(lines) - 1:
            lines.append("...")

        lines.append("")
        lines.append(f"[{row_count} rows x {column_count} columns]")
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        """
        Returns an html string primarily for use by notebooks for displaying
        a representation of the DataFrame. Displays 20 rows by default since
        many notebooks are not configured for large tables.
        """
        opts = bigframes.options.display
        max_results = bigframes.options.display.max_rows
        if opts.repr_mode == "deferred":
            return formatter.repr_query_job_html(self.query_job)

        self._cached()
        # TODO(swast): pass max_columns and get the true column count back. Maybe
        # get 1 more column than we have requested so that pandas can add the
        # ... for us?
        pandas_df, row_count, query_job = self._block.retrieve_repr_request_results(
            max_results
        )

        self._set_internal_query_job(query_job)

        column_count = len(pandas_df.columns)

        with display_options.pandas_repr(opts):
            # _repr_html_ stub is missing so mypy thinks it's a Series. Ignore mypy.
            html_string = pandas_df._repr_html_()  # type:ignore

        html_string += f"[{row_count} rows x {column_count} columns in total]"
        return html_string

    def __setitem__(self, key: str, value: SingleItemValue):
        df = self._assign_single_item(key, value)
        self._set_block(df._get_block())

    __setitem__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__setitem__)

    def _apply_binop(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        op,
        axis: str | int = "columns",
        how: str = "outer",
        reverse: bool = False,
    ):
        if isinstance(other, (float, int, bool)):
            return self._apply_scalar_binop(other, op, reverse=reverse)
        elif isinstance(other, DataFrame):
            return self._apply_dataframe_binop(other, op, how=how, reverse=reverse)
        elif isinstance(other, pandas.DataFrame):
            return self._apply_dataframe_binop(
                DataFrame(other), op, how=how, reverse=reverse
            )
        elif utils.get_axis_number(axis) == 0:
            bf_series = bigframes.core.convert.to_bf_series(other, self.index)
            return self._apply_series_binop_axis_0(bf_series, op, how, reverse)
        elif utils.get_axis_number(axis) == 1:
            pd_series = bigframes.core.convert.to_pd_series(other, self.columns)
            return self._apply_series_binop_axis_1(pd_series, op, how, reverse)
        raise NotImplementedError(
            f"binary operation is not implemented on the second operand of type {type(other).__name__}."
            f"{constants.FEEDBACK_LINK}"
        )

    def _apply_scalar_binop(
        self, other: float | int, op: ops.BinaryOp, reverse: bool = False
    ) -> DataFrame:
        block = self._block
        for column_id, label in zip(
            self._block.value_columns, self._block.column_labels
        ):
            expr = (
                op.as_expr(ex.const(other), column_id)
                if reverse
                else op.as_expr(column_id, ex.const(other))
            )
            block, _ = block.project_expr(expr, label)
            block = block.drop_columns([column_id])
        return DataFrame(block)

    def _apply_series_binop_axis_0(
        self,
        other: bigframes.series.Series,
        op: ops.BinaryOp,
        how: str = "outer",
        reverse: bool = False,
    ) -> DataFrame:
        block, (get_column_left, get_column_right) = self._block.join(
            other._block, how=how
        )

        series_column_id = other._value_column
        series_col = get_column_right[series_column_id]
        for column_id, label in zip(
            self._block.value_columns, self._block.column_labels
        ):
            self_col = get_column_left[column_id]
            expr = (
                op.as_expr(series_col, self_col)
                if reverse
                else op.as_expr(self_col, series_col)
            )
            block, _ = block.project_expr(expr, label)
            block = block.drop_columns([get_column_left[column_id]])

        block = block.drop_columns([series_col])
        block = block.with_index_labels(self.index.names)
        return DataFrame(block)

    def _apply_series_binop_axis_1(
        self,
        other: pandas.Series,
        op: ops.BinaryOp,
        how: str = "outer",
        reverse: bool = False,
    ) -> DataFrame:
        # Somewhat different alignment than df-df so separate codepath for now.
        if self.columns.equals(other.index):
            columns, lcol_indexer, rcol_indexer = self.columns, None, None
        else:
            if not (self.columns.is_unique and other.index.is_unique):
                raise ValueError("Cannot align non-unique indices")
            columns, lcol_indexer, rcol_indexer = self.columns.join(
                other.index, how=how, return_indexers=True
            )

        binop_result_ids = []

        column_indices = zip(
            lcol_indexer if (lcol_indexer is not None) else range(len(columns)),
            rcol_indexer if (rcol_indexer is not None) else range(len(columns)),
        )

        block = self._block
        for left_index, right_index in column_indices:
            if left_index >= 0 and right_index >= 0:  # -1 indices indicate missing
                self_col_id = self._block.value_columns[left_index]
                other_scalar = other.iloc[right_index]
                expr = (
                    op.as_expr(ex.const(other_scalar), self_col_id)
                    if reverse
                    else op.as_expr(self_col_id, ex.const(other_scalar))
                )
            elif left_index >= 0:
                self_col_id = self._block.value_columns[left_index]
                expr = (
                    op.as_expr(ex.const(None), self_col_id)
                    if reverse
                    else op.as_expr(self_col_id, ex.const(None))
                )
            elif right_index >= 0:
                other_scalar = other.iloc[right_index]
                expr = (
                    op.as_expr(ex.const(other_scalar), ex.const(None))
                    if reverse
                    else op.as_expr(ex.const(None), ex.const(other_scalar))
                )
            else:
                # Should not be possible
                raise ValueError("No right or left index.")
            block, result_col_id = block.project_expr(expr)
            binop_result_ids.append(result_col_id)

        block = block.select_columns(binop_result_ids)
        return DataFrame(block.with_column_labels(columns))

    def _apply_dataframe_binop(
        self,
        other: DataFrame,
        op: ops.BinaryOp,
        how: str = "outer",
        reverse: bool = False,
    ) -> DataFrame:
        # Join rows
        block, (get_column_left, get_column_right) = self._block.join(
            other._block, how=how
        )
        # join columns schema
        # indexers will be none for exact match
        columns, lcol_indexer, rcol_indexer = self.columns.join(
            other.columns, how=how, return_indexers=True
        )

        binop_result_ids = []

        column_indices = zip(
            lcol_indexer if (lcol_indexer is not None) else range(len(columns)),
            rcol_indexer if (lcol_indexer is not None) else range(len(columns)),
        )

        for left_index, right_index in column_indices:
            if left_index >= 0 and right_index >= 0:  # -1 indices indicate missing
                self_col_id = get_column_left[self._block.value_columns[left_index]]
                other_col_id = get_column_right[other._block.value_columns[right_index]]
                expr = (
                    op.as_expr(other_col_id, self_col_id)
                    if reverse
                    else op.as_expr(self_col_id, other_col_id)
                )
            elif left_index >= 0:
                self_col_id = get_column_left[self._block.value_columns[left_index]]
                expr = (
                    op.as_expr(ex.const(None), self_col_id)
                    if reverse
                    else op.as_expr(self_col_id, ex.const(None))
                )
            elif right_index >= 0:
                other_col_id = get_column_right[other._block.value_columns[right_index]]
                expr = (
                    op.as_expr(other_col_id, ex.const(None))
                    if reverse
                    else op.as_expr(ex.const(None), other_col_id)
                )
            else:
                # Should not be possible
                raise ValueError("No right or left index.")
            block, result_col_id = block.project_expr(expr)
            binop_result_ids.append(result_col_id)

        block = block.select_columns(binop_result_ids).with_column_labels(columns)
        return DataFrame(block)

    def eq(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.eq_op, axis=axis)

    def __eq__(self, other) -> DataFrame:  # type: ignore
        return self.eq(other)

    __eq__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__eq__)

    def ne(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.ne_op, axis=axis)

    def __ne__(self, other) -> DataFrame:  # type: ignore
        return self.ne(other)

    __ne__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__ne__)

    def le(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.le_op, axis=axis)

    def __le__(self, other) -> DataFrame:
        return self.le(other)

    __le__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__le__)

    def lt(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.lt_op, axis=axis)

    def __lt__(self, other) -> DataFrame:
        return self.lt(other)

    __lt__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__lt__)

    def ge(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.ge_op, axis=axis)

    def __ge__(self, other) -> DataFrame:
        return self.ge(other)

    __ge__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__ge__)

    def gt(self, other: typing.Any, axis: str | int = "columns") -> DataFrame:
        return self._apply_binop(other, ops.gt_op, axis=axis)

    def __gt__(self, other) -> DataFrame:
        return self.gt(other)

    __gt__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__gt__)

    def add(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        # TODO(swast): Support fill_value parameter.
        # TODO(swast): Support level parameter with MultiIndex.
        return self._apply_binop(other, ops.add_op, axis=axis)

    def radd(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        # TODO(swast): Support fill_value parameter.
        # TODO(swast): Support level parameter with MultiIndex.
        return self.add(other, axis=axis)

    def __add__(self, other) -> DataFrame:
        return self.add(other)

    __add__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__add__)

    __radd__ = __add__

    def sub(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.sub_op, axis=axis)

    subtract = sub
    subtract.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.sub)

    def __sub__(self, other):
        return self.sub(other)

    __sub__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__sub__)

    def rsub(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.sub_op, axis=axis, reverse=True)

    def __rsub__(self, other):
        return self.rsub(other)

    __rsub__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__rsub__)

    def mul(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.mul_op, axis=axis)

    multiply = mul
    multiply.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.mul)

    def __mul__(self, other):
        return self.mul(other)

    __mul__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__mul__)

    def rmul(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self.mul(other, axis=axis)

    def __rmul__(self, other):
        return self.rmul(other)

    __rmul__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__rmul__)

    def truediv(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.div_op, axis=axis)

    truediv.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.truediv)
    div = divide = truediv

    def __truediv__(self, other):
        return self.truediv(other)

    __truediv__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__truediv__)

    def rtruediv(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.div_op, axis=axis, reverse=True)

    rdiv = rtruediv
    rdiv.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.rtruediv)

    def __rtruediv__(self, other):
        return self.rtruediv(other)

    __rtruediv__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__rtruediv__)

    def floordiv(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.floordiv_op, axis=axis)

    def __floordiv__(self, other):
        return self.floordiv(other)

    __floordiv__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__floordiv__)

    def rfloordiv(
        self,
        other: float | int | bigframes.series.Series | DataFrame,
        axis: str | int = "columns",
    ) -> DataFrame:
        return self._apply_binop(other, ops.floordiv_op, axis=axis, reverse=True)

    def __rfloordiv__(self, other):
        return self.rfloordiv(other)

    __rfloordiv__.__doc__ = inspect.getdoc(
        vendored_pandas_frame.DataFrame.__rfloordiv__
    )

    def mod(self, other: int | bigframes.series.Series | DataFrame, axis: str | int = "columns") -> DataFrame:  # type: ignore
        return self._apply_binop(other, ops.mod_op, axis=axis)

    def __mod__(self, other):
        return self.mod(other)

    __mod__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__mod__)

    def rmod(self, other: int | bigframes.series.Series | DataFrame, axis: str | int = "columns") -> DataFrame:  # type: ignore
        return self._apply_binop(other, ops.mod_op, axis=axis, reverse=True)

    def __rmod__(self, other):
        return self.rmod(other)

    __rmod__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__rmod__)

    def pow(
        self, other: int | bigframes.series.Series, axis: str | int = "columns"
    ) -> DataFrame:
        return self._apply_binop(other, ops.pow_op, axis=axis)

    def __pow__(self, other):
        return self.pow(other)

    __pow__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__pow__)

    def rpow(
        self, other: int | bigframes.series.Series, axis: str | int = "columns"
    ) -> DataFrame:
        return self._apply_binop(other, ops.pow_op, axis=axis, reverse=True)

    def __rpow__(self, other):
        return self.rpow(other)

    __rpow__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__rpow__)

    def align(
        self,
        other: typing.Union[DataFrame, bigframes.series.Series],
        join: str = "outer",
        axis: typing.Union[str, int, None] = None,
    ) -> typing.Tuple[
        typing.Union[DataFrame, bigframes.series.Series],
        typing.Union[DataFrame, bigframes.series.Series],
    ]:
        axis_n = utils.get_axis_number(axis) if axis else None
        if axis_n == 1 and isinstance(other, bigframes.series.Series):
            raise NotImplementedError(
                f"align with series and axis=1 not supported. {constants.FEEDBACK_LINK}"
            )
        left_block, right_block = block_ops.align(
            self._block, other._block, join=join, axis=axis
        )
        return DataFrame(left_block), other.__class__(right_block)

    def update(self, other, join: str = "left", overwrite=True, filter_func=None):
        other = other if isinstance(other, DataFrame) else DataFrame(other)
        if join != "left":
            raise ValueError("Only 'left' join supported for update")

        if filter_func is not None:  # Will always take other if possible

            def update_func(
                left: bigframes.series.Series, right: bigframes.series.Series
            ) -> bigframes.series.Series:
                return left.mask(right.notna() & filter_func(left), right)

        elif overwrite:

            def update_func(
                left: bigframes.series.Series, right: bigframes.series.Series
            ) -> bigframes.series.Series:
                return left.mask(right.notna(), right)

        else:

            def update_func(
                left: bigframes.series.Series, right: bigframes.series.Series
            ) -> bigframes.series.Series:
                return left.mask(left.isna(), right)

        result = self.combine(other, update_func, how=join)

        self._set_block(result._block)

    def combine(
        self,
        other: DataFrame,
        func: typing.Callable[
            [bigframes.series.Series, bigframes.series.Series], bigframes.series.Series
        ],
        fill_value=None,
        overwrite: bool = True,
        *,
        how: str = "outer",
    ) -> DataFrame:
        l_aligned, r_aligned = block_ops.align(self._block, other._block, join=how)

        other_missing_labels = self._block.column_labels.difference(
            other._block.column_labels
        )

        l_frame = DataFrame(l_aligned)
        r_frame = DataFrame(r_aligned)
        results = []
        for (label, lseries), (_, rseries) in zip(l_frame.items(), r_frame.items()):
            if not ((label in other_missing_labels) and not overwrite):
                if fill_value is not None:
                    result = func(
                        lseries.fillna(fill_value), rseries.fillna(fill_value)
                    )
                else:
                    result = func(lseries, rseries)
            else:
                result = (
                    lseries.fillna(fill_value) if fill_value is not None else lseries
                )
            results.append(result)

        if all([isinstance(val, bigframes.series.Series) for val in results]):
            import bigframes.core.reshape as rs

            return rs.concat(results, axis=1)
        else:
            raise ValueError("'func' must return Series")

    def combine_first(self, other: DataFrame):
        return self._apply_dataframe_binop(other, ops.fillna_op)

    def corr(self, method="pearson", min_periods=None, numeric_only=False) -> DataFrame:
        if method != "pearson":
            raise NotImplementedError(
                f"Only Pearson correlation is currently supported. {constants.FEEDBACK_LINK}"
            )
        if min_periods:
            raise NotImplementedError(
                f"min_periods not yet supported. {constants.FEEDBACK_LINK}"
            )

        if not numeric_only:
            frame = self._raise_on_non_numeric("corr")
        else:
            frame = self._drop_non_numeric()

        return DataFrame(frame._block.calculate_pairwise_metric(op=agg_ops.CorrOp()))

    def cov(self, *, numeric_only: bool = False) -> DataFrame:
        if not numeric_only:
            frame = self._raise_on_non_numeric("corr")
        else:
            frame = self._drop_non_numeric()

        return DataFrame(frame._block.calculate_pairwise_metric(agg_ops.CovOp()))

    def to_pandas(
        self,
        max_download_size: Optional[int] = None,
        sampling_method: Optional[str] = None,
        random_state: Optional[int] = None,
        *,
        ordered: bool = True,
    ) -> pandas.DataFrame:
        """Write DataFrame to pandas DataFrame.

        Args:
            max_download_size (int, default None):
                Download size threshold in MB. If max_download_size is exceeded when downloading data
                (e.g., to_pandas()), the data will be downsampled if
                bigframes.options.sampling.enable_downsampling is True, otherwise, an error will be
                raised. If set to a value other than None, this will supersede the global config.
            sampling_method (str, default None):
                Downsampling algorithms to be chosen from, the choices are: "head": This algorithm
                returns a portion of the data from the beginning. It is fast and requires minimal
                computations to perform the downsampling; "uniform": This algorithm returns uniform
                random samples of the data. If set to a value other than None, this will supersede
                the global config.
            random_state (int, default None):
                The seed for the uniform downsampling algorithm. If provided, the uniform method may
                take longer to execute and require more computation. If set to a value other than
                None, this will supersede the global config.
            ordered (bool, default True):
                Determines whether the resulting pandas dataframe will be deterministically ordered.
                In some cases, unordered may result in a faster-executing query.

        Returns:
            pandas.DataFrame: A pandas DataFrame with all rows and columns of this DataFrame if the
                data_sampling_threshold_mb is not exceeded; otherwise, a pandas DataFrame with
                downsampled rows and all columns of this DataFrame.
        """
        # TODO(orrbradford): Optimize this in future. Potentially some cases where we can return the stored query job
        self._optimize_query_complexity()
        df, query_job = self._block.to_pandas(
            max_download_size=max_download_size,
            sampling_method=sampling_method,
            random_state=random_state,
            ordered=ordered,
        )
        self._set_internal_query_job(query_job)
        return df.set_axis(self._block.column_labels, axis=1, copy=False)

    def to_pandas_batches(self) -> Iterable[pandas.DataFrame]:
        """Stream DataFrame results to an iterable of pandas DataFrame"""
        self._optimize_query_complexity()
        return self._block.to_pandas_batches()

    def _compute_dry_run(self) -> bigquery.QueryJob:
        return self._block._compute_dry_run()

    def copy(self) -> DataFrame:
        return DataFrame(self._block)

    def head(self, n: int = 5) -> DataFrame:
        return typing.cast(DataFrame, self.iloc[:n])

    def tail(self, n: int = 5) -> DataFrame:
        return typing.cast(DataFrame, self.iloc[-n:])

    def peek(self, n: int = 5, *, force: bool = True) -> pandas.DataFrame:
        """
        Preview n arbitrary rows from the dataframe. No guarantees about row selection or ordering.
        ``DataFrame.peek(force=False)`` will always be very fast, but will not succeed if data requires
        full data scanning. Using ``force=True`` will always succeed, but may be perform queries.
        Query results will be cached so that future steps will benefit from these queries.

        Args:
            n (int, default 5):
                The number of rows to select from the dataframe. Which N rows are returned is non-deterministic.
            force (bool, default True):
                If the data cannot be peeked efficiently, the dataframe will instead be fully materialized as part
                of the operation if ``force=True``. If ``force=False``, the operation will throw a ValueError.
        Returns:
            pandas.DataFrame: A pandas DataFrame with n rows.

        Raises:
            ValueError: If force=False and data cannot be efficiently peeked.
        """
        maybe_result = self._block.try_peek(n)
        if maybe_result is None:
            if force:
                self._cached()
                maybe_result = self._block.try_peek(n, force=True)
                assert maybe_result is not None
            else:
                raise ValueError(
                    "Cannot peek efficiently when data has aggregates, joins or window functions applied. Use force=True to fully compute dataframe."
                )
        return maybe_result.set_axis(self._block.column_labels, axis=1, copy=False)

    def nlargest(
        self,
        n: int,
        columns: typing.Union[blocks.Label, typing.Sequence[blocks.Label]],
        keep: str = "first",
    ) -> DataFrame:
        if keep not in ("first", "last", "all"):
            raise ValueError("'keep must be one of 'first', 'last', or 'all'")
        column_ids = self._sql_names(columns)
        return DataFrame(block_ops.nlargest(self._block, n, column_ids, keep=keep))

    def nsmallest(
        self,
        n: int,
        columns: typing.Union[blocks.Label, typing.Sequence[blocks.Label]],
        keep: str = "first",
    ) -> DataFrame:
        if keep not in ("first", "last", "all"):
            raise ValueError("'keep must be one of 'first', 'last', or 'all'")
        column_ids = self._sql_names(columns)
        return DataFrame(block_ops.nsmallest(self._block, n, column_ids, keep=keep))

    def drop(
        self,
        labels: typing.Any = None,
        *,
        axis: typing.Union[int, str] = 0,
        index: typing.Any = None,
        columns: Union[blocks.Label, Sequence[blocks.Label]] = None,
        level: typing.Optional[LevelType] = None,
    ) -> DataFrame:
        if labels:
            if index or columns:
                raise ValueError("Cannot specify both 'labels' and 'index'/'columns")
            axis_n = utils.get_axis_number(axis)
            if axis_n == 0:
                index = labels
            else:
                columns = labels

        block = self._block
        if index is not None:
            level_id = self._resolve_levels(level or 0)[0]

            if utils.is_list_like(index):
                # Only tuple is treated as multi-index value combinations
                if isinstance(index, tuple):
                    if level is not None:
                        raise ValueError("Multi-index tuple can't specify level.")
                    condition_id = None
                    for i, idx in enumerate(index):
                        level_id = self._resolve_levels(i)[0]
                        block, condition_id_cur = block.project_expr(
                            ops.ne_op.as_expr(level_id, ex.const(idx))
                        )
                        if condition_id:
                            block, condition_id = block.apply_binary_op(
                                condition_id, condition_id_cur, ops.or_op
                            )
                        else:
                            condition_id = condition_id_cur

                    condition_id = typing.cast(str, condition_id)
                else:
                    block, inverse_condition_id = block.apply_unary_op(
                        level_id, ops.IsInOp(values=tuple(index), match_nulls=True)
                    )
                    block, condition_id = block.apply_unary_op(
                        inverse_condition_id, ops.invert_op
                    )
            elif isinstance(index, indexes.Index):
                return self._drop_by_index(index)
            else:
                block, condition_id = block.project_expr(
                    ops.ne_op.as_expr(level_id, ex.const(index))
                )
            block = block.filter_by_id(condition_id, keep_null=True).select_columns(
                self._block.value_columns
            )
        if columns:
            block = block.drop_columns(self._sql_names(columns))
        if index is None and not columns:
            raise ValueError("Must specify 'labels' or 'index'/'columns")
        return DataFrame(block)

    def _drop_by_index(self, index: indexes.Index) -> DataFrame:
        block = index._block
        block, ordering_col = block.promote_offsets()
        joined_index, (get_column_left, get_column_right) = self._block.join(block)

        new_ordering_col = get_column_right[ordering_col]
        drop_block = joined_index
        drop_block, drop_col = drop_block.apply_unary_op(
            new_ordering_col,
            ops.isnull_op,
        )

        drop_block = drop_block.filter_by_id(drop_col)
        original_columns = [
            get_column_left[column] for column in self._block.value_columns
        ]
        drop_block = drop_block.select_columns(original_columns)
        return DataFrame(drop_block)

    def droplevel(self, level: LevelsType, axis: int | str = 0):
        axis_n = utils.get_axis_number(axis)
        if axis_n == 0:
            resolved_level_ids = self._resolve_levels(level)
            return DataFrame(self._block.drop_levels(resolved_level_ids))
        else:
            if isinstance(self.columns, pandas.MultiIndex):
                new_df = self.copy()
                new_df.columns = self.columns.droplevel(level)
                return new_df
            else:
                raise ValueError("Columns must be a multiindex to drop levels.")

    def swaplevel(self, i: int = -2, j: int = -1, axis: int | str = 0):
        axis_n = utils.get_axis_number(axis)
        if axis_n == 0:
            level_i = self._block.index_columns[i]
            level_j = self._block.index_columns[j]
            mapping = {level_i: level_j, level_j: level_i}
            reordering = [
                mapping.get(index_id, index_id)
                for index_id in self._block.index_columns
            ]
            return DataFrame(self._block.reorder_levels(reordering))
        else:
            if isinstance(self.columns, pandas.MultiIndex):
                new_df = self.copy()
                new_df.columns = self.columns.swaplevel(i, j)
                return new_df
            else:
                raise ValueError("Columns must be a multiindex to reorder levels.")

    def reorder_levels(self, order: LevelsType, axis: int | str = 0):
        axis_n = utils.get_axis_number(axis)
        if axis_n == 0:
            resolved_level_ids = self._resolve_levels(order)
            return DataFrame(self._block.reorder_levels(resolved_level_ids))
        else:
            if isinstance(self.columns, pandas.MultiIndex):
                new_df = self.copy()
                new_df.columns = self.columns.reorder_levels(order)
                return new_df
            else:
                raise ValueError("Columns must be a multiindex to reorder levels.")

    def _resolve_levels(self, level: LevelsType) -> typing.Sequence[str]:
        return self._block.index.resolve_level(level)

    def rename(self, *, columns: Mapping[blocks.Label, blocks.Label]) -> DataFrame:
        block = self._block.rename(columns=columns)
        return DataFrame(block)

    def rename_axis(
        self,
        mapper: typing.Union[blocks.Label, typing.Sequence[blocks.Label]],
        **kwargs,
    ) -> DataFrame:
        if len(kwargs) != 0:
            raise NotImplementedError(
                f"rename_axis does not currently support any keyword arguments. {constants.FEEDBACK_LINK}"
            )
        # limited implementation: the new index name is simply the 'mapper' parameter
        if utils.is_list_like(mapper):
            labels = mapper
        else:
            labels = [mapper]
        return DataFrame(self._block.with_index_labels(labels))

    def equals(self, other: typing.Union[bigframes.series.Series, DataFrame]) -> bool:
        # Must be same object type, same column dtypes, and same label values
        if not isinstance(other, DataFrame):
            return False
        return block_ops.equals(self._block, other._block)

    def assign(self, **kwargs) -> DataFrame:
        # TODO(garrettwu) Support list-like values. Requires ordering.
        # TODO(garrettwu) Support callable values.

        cur = self
        for k, v in kwargs.items():
            cur = cur._assign_single_item(k, v)

        return cur

    def _assign_single_item(
        self,
        k: str,
        v: SingleItemValue,
    ) -> DataFrame:
        if isinstance(v, bigframes.series.Series):
            return self._assign_series_join_on_index(k, v)
        elif isinstance(v, bigframes.dataframe.DataFrame):
            v_df_col_count = len(v._block.value_columns)
            if v_df_col_count != 1:
                raise ValueError(
                    f"Cannot set a DataFrame with {v_df_col_count} columns to the single column {k}"
                )
            return self._assign_series_join_on_index(k, v[v.columns[0]])
        elif callable(v):
            copy = self.copy()
            copy[k] = v(copy)
            return copy
        elif utils.is_list_like(v):
            return self._assign_single_item_listlike(k, v)
        else:
            return self._assign_scalar(k, v)

    def _assign_single_item_listlike(self, k: str, v: Sequence) -> DataFrame:
        given_rows = len(v)
        actual_rows = len(self)
        assigning_to_empty_df = len(self.columns) == 0 and actual_rows == 0
        if not assigning_to_empty_df and given_rows != actual_rows:
            raise ValueError(
                f"Length of values ({given_rows}) does not match length of index ({actual_rows})"
            )

        local_df = DataFrame({k: v}, session=self._get_block().expr.session)
        # local_df is likely (but not guaranteed) to be cached locally
        # since the original list came from memory and so is probably < MAX_INLINE_DF_SIZE

        new_column_block = local_df._block
        original_index_column_ids = self._block.index_columns
        self_block = self._block.reset_index(drop=False)
        if assigning_to_empty_df:
            if len(self._block.index_columns) > 1:
                # match error raised by pandas here
                raise ValueError(
                    "Assigning listlike to a first column under multiindex is not supported."
                )
            result_block = new_column_block.with_index_labels(self._block.index.names)
            result_block = result_block.with_column_labels([k])
        else:
            result_block, (
                get_column_left,
                get_column_right,
            ) = self_block.join(new_column_block, how="left", block_identity_join=True)
            result_block = result_block.set_index(
                [get_column_left[col_id] for col_id in original_index_column_ids],
                index_labels=self._block.index.names,
            )
            src_col = get_column_right[new_column_block.value_columns[0]]
            # Check to see if key exists, and modify in place
            col_ids = self._block.cols_matching_label(k)
            for col_id in col_ids:
                result_block = result_block.copy_values(
                    src_col, get_column_left[col_id]
                )
            if len(col_ids) > 0:
                result_block = result_block.drop_columns([src_col])
        return DataFrame(result_block)

    def _assign_scalar(self, label: str, value: Union[int, float]) -> DataFrame:
        col_ids = self._block.cols_matching_label(label)

        block, constant_col_id = self._block.create_constant(value, label)
        for col_id in col_ids:
            block = block.copy_values(constant_col_id, col_id)

        if len(col_ids) > 0:
            block = block.drop_columns([constant_col_id])

        return DataFrame(block)

    def _assign_series_join_on_index(
        self, label: str, series: bigframes.series.Series
    ) -> DataFrame:
        block, (get_column_left, get_column_right) = self._block.join(
            series._block, how="left"
        )

        column_ids = [
            get_column_left[col_id] for col_id in self._block.cols_matching_label(label)
        ]
        source_column = get_column_right[series._value_column]

        # Replace each column matching the label
        for column_id in column_ids:
            block = block.copy_values(source_column, column_id).assign_label(
                column_id, label
            )

        if not column_ids:
            # Append case, so new column needs appropriate label
            block = block.assign_label(source_column, label)
        else:
            # Update case, remove after copying into columns
            block = block.drop_columns([source_column])

        return DataFrame(block.with_index_labels(self.index.names))

    def reset_index(self, *, drop: bool = False) -> DataFrame:
        block = self._block.reset_index(drop)
        return DataFrame(block)

    def set_index(
        self,
        keys: typing.Union[blocks.Label, typing.Sequence[blocks.Label]],
        append: bool = False,
        drop: bool = True,
    ) -> DataFrame:
        if not utils.is_list_like(keys):
            keys = typing.cast(typing.Sequence[blocks.Label], (keys,))
        else:
            keys = typing.cast(typing.Sequence[blocks.Label], tuple(keys))
        col_ids = [self._resolve_label_exact(key) for key in keys]
        missing = [keys[i] for i in range(len(col_ids)) if col_ids[i] is None]
        if len(missing) > 0:
            raise KeyError(f"None of {missing} are in the columns")
        # convert col_ids to non-optional strs since we just determined they are not None
        col_ids_strs: List[str] = [col_id for col_id in col_ids if col_id is not None]
        return DataFrame(self._block.set_index(col_ids_strs, append=append, drop=drop))

    def sort_index(
        self, ascending: bool = True, na_position: Literal["first", "last"] = "last"
    ) -> DataFrame:
        if na_position not in ["first", "last"]:
            raise ValueError("Param na_position must be one of 'first' or 'last'")
        na_last = na_position == "last"
        index_columns = self._block.index_columns
        ordering = [
            order.ascending_over(column, na_last)
            if ascending
            else order.descending_over(column, na_last)
            for column in index_columns
        ]
        return DataFrame(self._block.order_by(ordering))

    def sort_values(
        self,
        by: str | typing.Sequence[str],
        *,
        ascending: bool | typing.Sequence[bool] = True,
        kind: str = "quicksort",
        na_position: typing.Literal["first", "last"] = "last",
    ) -> DataFrame:
        if na_position not in {"first", "last"}:
            raise ValueError("Param na_position must be one of 'first' or 'last'")

        sort_labels = list(by) if utils.is_list_like(by) else [by]
        sort_column_ids = self._sql_names(sort_labels)

        len_by = len(sort_labels)
        if not isinstance(ascending, bool):
            if len(ascending) != len_by:
                raise ValueError("Length of 'ascending' must equal length of 'by'")
            sort_directions = ascending
        else:
            sort_directions = (ascending,) * len_by

        ordering = []
        for i in range(len(sort_labels)):
            column_id = sort_column_ids[i]
            is_ascending = sort_directions[i]
            na_last = na_position == "last"
            ordering.append(
                order.ascending_over(column_id, na_last)
                if is_ascending
                else order.descending_over(column_id, na_last)
            )
        return DataFrame(self._block.order_by(ordering))

    def eval(self, expr: str) -> DataFrame:
        import bigframes.core.eval as bf_eval

        return bf_eval.eval(self, expr, target=self)

    def query(self, expr: str) -> DataFrame:
        import bigframes.core.eval as bf_eval

        eval_result = bf_eval.eval(self, expr, target=None)
        return self[eval_result]

    def value_counts(
        self,
        subset: typing.Union[blocks.Label, typing.Sequence[blocks.Label]] = None,
        normalize: bool = False,
        sort: bool = True,
        ascending: bool = False,
        dropna: bool = True,
    ):
        # 'sort'=False allows arbitrary sorting, so we will sort anyways and ignore the param
        columns = self._sql_names(subset) if subset else self._block.value_columns
        block = block_ops.value_counts(
            self._block,
            columns,
            normalize=normalize,
            sort=sort,
            ascending=ascending,
            dropna=dropna,
        )
        return bigframes.series.Series(block)

    def add_prefix(self, prefix: str, axis: int | str | None = None) -> DataFrame:
        axis = 1 if axis is None else axis
        return DataFrame(self._get_block().add_prefix(prefix, axis))

    def add_suffix(self, suffix: str, axis: int | str | None = None) -> DataFrame:
        axis = 1 if axis is None else axis
        return DataFrame(self._get_block().add_suffix(suffix, axis))

    def filter(
        self,
        items: typing.Optional[typing.Iterable] = None,
        like: typing.Optional[str] = None,
        regex: typing.Optional[str] = None,
        axis: int | str | None = None,
    ) -> DataFrame:
        if sum([(items is not None), (like is not None), (regex is not None)]) != 1:
            raise ValueError(
                "Need to provide exactly one of 'items', 'like', or 'regex'"
            )
        axis_n = utils.get_axis_number(axis) if (axis is not None) else 1
        if axis_n == 0:  # row labels
            return self._filter_rows(items, like, regex)
        else:  # column labels
            return self._filter_columns(items, like, regex)

    def _filter_rows(
        self,
        items: typing.Optional[typing.Iterable] = None,
        like: typing.Optional[str] = None,
        regex: typing.Optional[str] = None,
    ) -> DataFrame:
        if len(self._block.index_columns) > 1:
            raise NotImplementedError(
                f"Method filter does not support rows multiindex. {constants.FEEDBACK_LINK}"
            )
        if (like is not None) or (regex is not None):
            block = self._block
            block, label_string_id = block.apply_unary_op(
                self._block.index_columns[0],
                ops.AsTypeOp(to_type=pandas.StringDtype(storage="pyarrow")),
            )
            if like is not None:
                block, mask_id = block.apply_unary_op(
                    label_string_id, ops.StrContainsOp(pat=like)
                )
            else:  # regex
                assert regex is not None
                block, mask_id = block.apply_unary_op(
                    label_string_id, ops.StrContainsRegexOp(pat=regex)
                )

            block = block.filter_by_id(mask_id)
            block = block.select_columns(self._block.value_columns)
            return DataFrame(block)
        elif items is not None:
            # Behavior matches pandas 2.1+, older pandas versions would reindex
            block = self._block
            block, mask_id = block.apply_unary_op(
                self._block.index_columns[0], ops.IsInOp(values=tuple(items))
            )
            block = block.filter_by_id(mask_id)
            block = block.select_columns(self._block.value_columns)
            return DataFrame(block)
        else:
            raise ValueError("Need to provide 'items', 'like', or 'regex'")

    def _filter_columns(
        self,
        items: typing.Optional[typing.Iterable] = None,
        like: typing.Optional[str] = None,
        regex: typing.Optional[str] = None,
    ) -> DataFrame:
        if (like is not None) or (regex is not None):

            def label_filter(label):
                label_str = label if isinstance(label, str) else str(label)
                if like:
                    return like in label_str
                else:  # regex
                    return re.match(regex, label_str) is not None

            cols = [
                col_id
                for col_id, label in zip(self._block.value_columns, self.columns)
                if label_filter(label)
            ]
            return DataFrame(self._block.select_columns(cols))
        if items is not None:
            # Behavior matches pandas 2.1+, older pandas versions would reorder using order of items
            new_columns = self.columns.intersection(pandas.Index(items))
            return self.reindex(columns=new_columns)
        else:
            raise ValueError("Need to provide 'items', 'like', or 'regex'")

    def reindex(
        self,
        labels=None,
        *,
        index=None,
        columns=None,
        axis: typing.Optional[typing.Union[str, int]] = None,
        validate: typing.Optional[bool] = None,
    ):
        if labels:
            if index or columns:
                raise ValueError("Cannot specify both 'labels' and 'index'/'columns")
            axis_n = utils.get_axis_number(axis) if (axis is not None) else 0
            if axis_n == 0:
                index = labels
            else:
                columns = labels
        if (index is not None) and (columns is not None):
            return self._reindex_columns(columns)._reindex_rows(
                index, validate=validate or False
            )
        if index is not None:
            return self._reindex_rows(index, validate=validate or False)
        if columns is not None:
            return self._reindex_columns(columns)

    def _reindex_rows(
        self,
        index,
        *,
        validate: typing.Optional[bool] = None,
    ):
        if validate and not self.index.is_unique:
            raise ValueError("Original index must be unique to reindex")
        keep_original_names = False
        if isinstance(index, indexes.Index):
            new_indexer = DataFrame(data=index._block)[[]]
        else:
            if not isinstance(index, pandas.Index):
                keep_original_names = True
                index = pandas.Index(index)
            if index.nlevels != self.index.nlevels:
                raise NotImplementedError(
                    "Cannot reindex with index with different nlevels"
                )
            new_indexer = DataFrame(index=index, session=self._session)[[]]
        # multiindex join is senstive to index names, so we will set all these
        result = new_indexer.rename_axis(range(new_indexer.index.nlevels)).join(
            self.rename_axis(range(self.index.nlevels)),
            how="left",
        )
        # and then reset the names after the join
        return result.rename_axis(
            self.index.names if keep_original_names else index.names
        )

    def _reindex_columns(self, columns):
        block = self._block
        new_column_index, indexer = self.columns.reindex(columns)
        result_cols = []
        for label, index in zip(columns, indexer):
            if index >= 0:
                result_cols.append(self._block.value_columns[index])
            else:
                block, null_col = block.create_constant(
                    pandas.NA, label, dtype=pandas.Float64Dtype()
                )
                result_cols.append(null_col)
        result_df = DataFrame(block.select_columns(result_cols))
        result_df.columns = new_column_index
        return result_df

    def reindex_like(self, other: DataFrame, *, validate: typing.Optional[bool] = None):
        return self.reindex(index=other.index, columns=other.columns, validate=validate)

    def interpolate(self, method: str = "linear") -> DataFrame:
        if method == "pad":
            return self.ffill()
        result = block_ops.interpolate(self._block, method)
        return DataFrame(result)

    def fillna(self, value=None) -> DataFrame:
        return self._apply_binop(value, ops.fillna_op, how="left")

    def replace(
        self, to_replace: typing.Any, value: typing.Any = None, *, regex: bool = False
    ):
        if utils.is_dict_like(value):
            return self.apply(
                lambda x: x.replace(
                    to_replace=to_replace, value=value[x.name], regex=regex
                )
                if (x.name in value)
                else x
            )
        return self.apply(
            lambda x: x.replace(to_replace=to_replace, value=value, regex=regex)
        )

    def ffill(self, *, limit: typing.Optional[int] = None) -> DataFrame:
        window = bigframes.core.WindowSpec(preceding=limit, following=0)
        return self._apply_window_op(agg_ops.LastNonNullOp(), window)

    def bfill(self, *, limit: typing.Optional[int] = None) -> DataFrame:
        window = bigframes.core.WindowSpec(preceding=0, following=limit)
        return self._apply_window_op(agg_ops.FirstNonNullOp(), window)

    def isin(self, values) -> DataFrame:
        if utils.is_dict_like(values):
            block = self._block
            result_ids = []
            for col, label in zip(self._block.value_columns, self._block.column_labels):
                if label in values.keys():
                    value_for_key = values[label]
                    block, result_id = block.apply_unary_op(
                        col,
                        ops.IsInOp(values=tuple(value_for_key), match_nulls=True),
                        label,
                    )
                    result_ids.append(result_id)
                else:
                    block, result_id = block.create_constant(
                        False, label=label, dtype=pandas.BooleanDtype()
                    )
                    result_ids.append(result_id)
            return DataFrame(block.select_columns(result_ids)).fillna(value=False)
        elif utils.is_list_like(values):
            return self._apply_unary_op(
                ops.IsInOp(values=tuple(values), match_nulls=True)
            ).fillna(value=False)
        else:
            raise TypeError(
                "only list-like objects are allowed to be passed to "
                f"isin(), you passed a [{type(values).__name__}]"
            )

    def keys(self) -> pandas.Index:
        return self.columns

    def items(self):
        column_ids = self._block.value_columns
        column_labels = self._block.column_labels
        for col_id, col_label in zip(column_ids, column_labels):
            yield col_label, bigframes.series.Series(self._block.select_column(col_id))

    def iterrows(self) -> Iterable[tuple[typing.Any, pandas.Series]]:
        for df in self.to_pandas_batches():
            for item in df.iterrows():
                yield item

    def itertuples(
        self, index: bool = True, name: typing.Optional[str] = "Pandas"
    ) -> Iterable[tuple[typing.Any, ...]]:
        for df in self.to_pandas_batches():
            for item in df.itertuples(index=index, name=name):
                yield item

    def dropna(
        self,
        *,
        axis: int | str = 0,
        inplace: bool = False,
        how: str = "any",
        ignore_index=False,
    ) -> DataFrame:
        if inplace:
            raise NotImplementedError(
                f"'inplace'=True not supported. {constants.FEEDBACK_LINK}"
            )
        if how not in ("any", "all"):
            raise ValueError("'how' must be one of 'any', 'all'")

        axis_n = utils.get_axis_number(axis)

        if axis_n == 0:
            result = block_ops.dropna(self._block, self._block.value_columns, how=how)  # type: ignore
            if ignore_index:
                result = result.reset_index()
            return DataFrame(result)
        else:
            isnull_block = self._block.multi_apply_unary_op(
                self._block.value_columns, ops.isnull_op
            )
            if how == "any":
                null_locations = DataFrame(isnull_block).any().to_pandas()
            else:  # 'all'
                null_locations = DataFrame(isnull_block).all().to_pandas()
            keep_columns = [
                col
                for col, to_drop in zip(self._block.value_columns, null_locations)
                if not to_drop
            ]
            return DataFrame(self._block.select_columns(keep_columns))

    def any(
        self,
        *,
        axis: typing.Union[str, int] = 0,
        bool_only: bool = False,
    ) -> bigframes.series.Series:
        if not bool_only:
            frame = self._raise_on_non_boolean("any")
        else:
            frame = self._drop_non_bool()
        block = frame._block.aggregate_all_and_stack(agg_ops.any_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def all(
        self, axis: typing.Union[str, int] = 0, *, bool_only: bool = False
    ) -> bigframes.series.Series:
        if not bool_only:
            frame = self._raise_on_non_boolean("all")
        else:
            frame = self._drop_non_bool()
        block = frame._block.aggregate_all_and_stack(agg_ops.all_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def sum(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("sum")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.sum_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def mean(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("mean")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.mean_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def median(
        self, *, numeric_only: bool = False, exact: bool = True
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("median")
        else:
            frame = self._drop_non_numeric()
        if exact:
            result = frame.quantile()
            result.name = None
            return result
        else:
            block = frame._block.aggregate_all_and_stack(agg_ops.median_op)
            return bigframes.series.Series(block.select_column("values"))

    def quantile(
        self, q: Union[float, Sequence[float]] = 0.5, *, numeric_only: bool = False
    ):
        if not numeric_only:
            frame = self._raise_on_non_numeric("median")
        else:
            frame = self._drop_non_numeric()
        multi_q = utils.is_list_like(q)
        result = block_ops.quantile(
            frame._block, frame._block.value_columns, qs=tuple(q) if multi_q else (q,)  # type: ignore
        )
        if multi_q:
            return DataFrame(result.stack()).droplevel(0)
        else:
            result_df = (
                DataFrame(result)
                .stack(list(range(0, frame.columns.nlevels)))
                .droplevel(0)
            )
            result_series = bigframes.series.Series(result_df._block)
            result_series.name = q
            return result_series

    def std(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("std")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.std_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def var(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("var")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.var_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def min(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("min")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.min_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def max(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("max")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.max_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    def prod(
        self, axis: typing.Union[str, int] = 0, *, numeric_only: bool = False
    ) -> bigframes.series.Series:
        if not numeric_only:
            frame = self._raise_on_non_numeric("prod")
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.product_op, axis=axis)
        return bigframes.series.Series(block.select_column("values"))

    product = prod
    product.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.prod)

    def count(self, *, numeric_only: bool = False) -> bigframes.series.Series:
        if not numeric_only:
            frame = self
        else:
            frame = self._drop_non_numeric()
        block = frame._block.aggregate_all_and_stack(agg_ops.count_op)
        return bigframes.series.Series(block.select_column("values"))

    def nunique(self) -> bigframes.series.Series:
        block = self._block.aggregate_all_and_stack(agg_ops.nunique_op)
        return bigframes.series.Series(block.select_column("values"))

    def agg(
        self, func: str | typing.Sequence[str]
    ) -> DataFrame | bigframes.series.Series:
        if utils.is_list_like(func):
            if any(
                dtype not in bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_PERMISSIVE
                for dtype in self.dtypes
            ):
                raise NotImplementedError(
                    f"Multiple aggregations only supported on numeric columns. {constants.FEEDBACK_LINK}"
                )
            aggregations = [agg_ops.lookup_agg_func(f) for f in func]
            return DataFrame(
                self._block.summarize(
                    self._block.value_columns,
                    aggregations,
                )
            )
        else:
            return bigframes.series.Series(
                self._block.aggregate_all_and_stack(
                    agg_ops.lookup_agg_func(typing.cast(str, func))
                )
            )

    aggregate = agg
    aggregate.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.agg)

    def idxmin(self) -> bigframes.series.Series:
        return bigframes.series.Series(block_ops.idxmin(self._block))

    def idxmax(self) -> bigframes.series.Series:
        return bigframes.series.Series(block_ops.idxmax(self._block))

    def melt(
        self,
        id_vars: typing.Optional[typing.Iterable[typing.Hashable]] = None,
        value_vars: typing.Optional[typing.Iterable[typing.Hashable]] = None,
        var_name: typing.Union[
            typing.Hashable, typing.Sequence[typing.Hashable]
        ] = None,
        value_name: typing.Hashable = "value",
    ):
        if var_name is None:
            # Determine default var_name. Attempt to use column labels if they are unique
            if self.columns.nlevels > 1:
                if len(set(self.columns.names)) == len(self.columns.names):
                    var_name = self.columns.names
                else:
                    var_name = [f"variable_{i}" for i in range(len(self.columns.names))]
            else:
                var_name = self.columns.name or "variable"

        var_name = tuple(var_name) if utils.is_list_like(var_name) else (var_name,)

        if id_vars is not None:
            id_col_ids = [self._resolve_label_exact(col) for col in id_vars]
        else:
            id_col_ids = []
        if value_vars is not None:
            val_col_ids = [self._resolve_label_exact(col) for col in value_vars]
        else:
            val_col_ids = [
                col_id
                for col_id in self._block.value_columns
                if col_id not in id_col_ids
            ]

        return DataFrame(
            self._block.melt(id_col_ids, val_col_ids, var_name, value_name)
        )

    def describe(self) -> DataFrame:
        df_numeric = self._drop_non_numeric(permissive=False)
        if len(df_numeric.columns) == 0:
            raise NotImplementedError(
                f"df.describe() currently only supports numeric values. {constants.FEEDBACK_LINK}"
            )
        result = df_numeric.agg(
            ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]
        )
        return typing.cast(DataFrame, result)

    def skew(self, *, numeric_only: bool = False):
        if not numeric_only:
            frame = self._raise_on_non_numeric("skew")
        else:
            frame = self._drop_non_numeric()
        result_block = block_ops.skew(frame._block, frame._block.value_columns)
        return bigframes.series.Series(result_block)

    def kurt(self, *, numeric_only: bool = False):
        if not numeric_only:
            frame = self._raise_on_non_numeric("kurt")
        else:
            frame = self._drop_non_numeric()
        result_block = block_ops.kurt(frame._block, frame._block.value_columns)
        return bigframes.series.Series(result_block)

    kurtosis = kurt
    kurtosis.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.kurt)

    def _pivot(
        self,
        *,
        columns: typing.Union[blocks.Label, Sequence[blocks.Label]],
        columns_unique_values: typing.Optional[
            typing.Union[pandas.Index, Sequence[object]]
        ] = None,
        index: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
        values: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
    ) -> DataFrame:
        if index:
            block = self.set_index(index)._block
        else:
            block = self._block

        column_ids = self._sql_names(columns)
        if values:
            value_col_ids = self._sql_names(values)
        else:
            value_col_ids = [
                col for col in block.value_columns if col not in column_ids
            ]

        pivot_block = block.pivot(
            columns=column_ids,
            values=value_col_ids,
            columns_unique_values=columns_unique_values,
            values_in_index=utils.is_list_like(values),
        )
        return DataFrame(pivot_block)

    def pivot(
        self,
        *,
        columns: typing.Union[blocks.Label, Sequence[blocks.Label]],
        index: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
        values: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
    ) -> DataFrame:
        return self._pivot(columns=columns, index=index, values=values)

    def pivot_table(
        self,
        values: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
        index: typing.Optional[
            typing.Union[blocks.Label, Sequence[blocks.Label]]
        ] = None,
        columns: typing.Union[blocks.Label, Sequence[blocks.Label]] = None,
        aggfunc: str = "mean",
    ) -> DataFrame:
        if isinstance(index, Iterable) and not (
            isinstance(index, blocks.Label) and index in self.columns
        ):
            index = list(index)
        else:
            index = [index]

        if isinstance(columns, Iterable) and not (
            isinstance(columns, blocks.Label) and columns in self.columns
        ):
            columns = list(columns)
        else:
            columns = [columns]

        if isinstance(values, Iterable) and not (
            isinstance(values, blocks.Label) and values in self.columns
        ):
            values = list(values)
        else:
            values = [values]

        # Unlike pivot, pivot_table has values always ordered.
        values.sort()

        keys = index + columns
        agged = self.groupby(keys, dropna=True)[values].agg(aggfunc)

        if isinstance(agged, bigframes.series.Series):
            agged = agged.to_frame()

        agged = agged.dropna(how="all")

        if len(values) == 1:
            agged = agged.rename(columns={agged.columns[0]: values[0]})

        agged = agged.reset_index()

        pivoted = agged.pivot(
            columns=columns,
            index=index,
            values=values if len(values) > 1 else None,
        ).sort_index()

        # TODO: Remove the reordering step once the issue is resolved.
        # The pivot_table method results in multi-index columns that are always ordered.
        # However, the order of the pivoted result columns is not guaranteed to be sorted.
        # Sort and reorder.
        return pivoted[pivoted.columns.sort_values()]

    def stack(self, level: LevelsType = -1):
        if not isinstance(self.columns, pandas.MultiIndex):
            if level not in [0, -1, self.columns.name]:
                raise IndexError(f"Invalid level {level} for single-level index")
            return self._stack_mono()
        return self._stack_multi(level)

    def _stack_mono(self):
        result_block = self._block.stack()
        return bigframes.series.Series(result_block)

    def _stack_multi(self, level: LevelsType = -1):
        n_levels = self.columns.nlevels
        if not utils.is_list_like(level):
            level = [level]
        level_indices = []
        for level_ref in level:
            if isinstance(level_ref, int):
                if level_ref < 0:
                    level_indices.append(n_levels + level_ref)
                else:
                    level_indices.append(level_ref)
            else:  # str
                level_indices.append(self.columns.names.index(level_ref))  # type: ignore

        new_order = [
            *[i for i in range(n_levels) if i not in level_indices],
            *level_indices,
        ]

        original_columns = typing.cast(pandas.MultiIndex, self.columns)
        new_columns = original_columns.reorder_levels(new_order)

        block = self._block.with_column_labels(new_columns)

        block = block.stack(levels=len(level))
        return DataFrame(block)

    def unstack(self, level: LevelsType = -1):
        if not utils.is_list_like(level):
            level = [level]

        block = self._block
        # Special case, unstack with mono-index transpose into a series
        if self.index.nlevels == 1:
            block = block.stack(how="right", levels=self.columns.nlevels)
            return bigframes.series.Series(block)

        # Pivot by index levels
        unstack_ids = self._resolve_levels(level)
        block = block.reset_index(drop=False)
        block = block.set_index(
            [col for col in self._block.index_columns if col not in unstack_ids]
        )

        pivot_block = block.pivot(
            columns=unstack_ids,
            values=self._block.value_columns,
            values_in_index=True,
        )
        return DataFrame(pivot_block)

    def _drop_non_numeric(self, permissive=True) -> DataFrame:
        types_to_keep = (
            set(bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_PERMISSIVE)
            if permissive
            else set(bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_RESTRICTIVE)
        )
        non_numeric_cols = [
            col_id
            for col_id, dtype in zip(self._block.value_columns, self._block.dtypes)
            if dtype not in types_to_keep
        ]
        return DataFrame(self._block.drop_columns(non_numeric_cols))

    def _drop_non_bool(self) -> DataFrame:
        non_bool_cols = [
            col_id
            for col_id, dtype in zip(self._block.value_columns, self._block.dtypes)
            if dtype not in bigframes.dtypes.BOOL_BIGFRAMES_TYPES
        ]
        return DataFrame(self._block.drop_columns(non_bool_cols))

    def _raise_on_non_numeric(self, op: str):
        if not all(
            dtype in bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_PERMISSIVE
            for dtype in self._block.dtypes
        ):
            raise NotImplementedError(
                f"'{op}' does not support non-numeric columns. "
                f"Set 'numeric_only'=True to ignore non-numeric columns. {constants.FEEDBACK_LINK}"
            )
        return self

    def _raise_on_non_boolean(self, op: str):
        if not all(
            dtype in bigframes.dtypes.BOOL_BIGFRAMES_TYPES
            for dtype in self._block.dtypes
        ):
            raise NotImplementedError(
                f"'{op}' does not support non-bool columns. "
                f"Set 'bool_only'=True to ignore non-bool columns. {constants.FEEDBACK_LINK}"
            )
        return self

    def merge(
        self,
        right: DataFrame,
        how: Literal[
            "inner",
            "left",
            "outer",
            "right",
            "cross",
        ] = "inner",
        # TODO(garrettwu): Currently can take inner, outer, left and right. To support
        # cross joins
        on: Union[blocks.Label, Sequence[blocks.Label], None] = None,
        *,
        left_on: Union[blocks.Label, Sequence[blocks.Label], None] = None,
        right_on: Union[blocks.Label, Sequence[blocks.Label], None] = None,
        sort: bool = False,
        suffixes: tuple[str, str] = ("_x", "_y"),
    ) -> DataFrame:
        if how == "cross":
            if on is not None:
                raise ValueError("'on' is not supported for cross join.")
            result_block = self._block.merge(
                right._block,
                left_join_ids=[],
                right_join_ids=[],
                suffixes=suffixes,
                how=how,
                sort=True,
            )
            return DataFrame(result_block)

        if on is None:
            if left_on is None or right_on is None:
                raise ValueError("Must specify `on` or `left_on` + `right_on`.")
        else:
            if left_on is not None or right_on is not None:
                raise ValueError(
                    "Can not pass both `on` and `left_on` + `right_on` params."
                )
            left_on, right_on = on, on

        if utils.is_list_like(left_on):
            left_on = list(left_on)  # type: ignore
        else:
            left_on = [left_on]

        if utils.is_list_like(right_on):
            right_on = list(right_on)  # type: ignore
        else:
            right_on = [right_on]

        left_join_ids = []
        for label in left_on:  # type: ignore
            left_col_id = self._resolve_label_exact(label)
            # 0 elements already throws an exception
            if not left_col_id:
                raise ValueError(f"No column {label} found in self.")
            left_join_ids.append(left_col_id)

        right_join_ids = []
        for label in right_on:  # type: ignore
            right_col_id = right._resolve_label_exact(label)
            if not right_col_id:
                raise ValueError(f"No column {label} found in other.")
            right_join_ids.append(right_col_id)

        block = self._block.merge(
            right._block,
            how,
            left_join_ids,
            right_join_ids,
            sort=sort,
            suffixes=suffixes,
        )
        return DataFrame(block)

    def join(
        self, other: DataFrame, *, on: Optional[str] = None, how: str = "left"
    ) -> DataFrame:
        left, right = self, other
        if not left.columns.intersection(right.columns).empty:
            raise NotImplementedError(
                f"Deduping column names is not implemented. {constants.FEEDBACK_LINK}"
            )
        if how == "cross":
            if on is not None:
                raise ValueError("'on' is not supported for cross join.")
            result_block = left._block.merge(
                right._block,
                left_join_ids=[],
                right_join_ids=[],
                suffixes=("", ""),
                how="cross",
                sort=True,
            )
            return DataFrame(result_block)

        # Join left columns with right index
        if on is not None:
            if other._block.index.nlevels != 1:
                raise ValueError(
                    "Join on columns must match the index level of the other DataFrame. Join on column with multi-index haven't been supported."
                )
            # Switch left index with on column
            left_columns = left.columns
            left_idx_original_names = left.index.names
            left_idx_names_in_cols = [
                f"bigframes_left_idx_name_{i}" for i in range(len(left.index.names))
            ]
            left.index.names = left_idx_names_in_cols
            left = left.reset_index(drop=False)
            left = left.set_index(on)

            # Join on index and switch back
            combined_df = left._perform_join_by_index(right, how=how)
            combined_df.index.name = on
            combined_df = combined_df.reset_index(drop=False)
            combined_df = combined_df.set_index(left_idx_names_in_cols)

            # To be consistent with Pandas
            combined_df.index.names = (
                left_idx_original_names
                if how in ("inner", "left")
                else ([None] * len(combined_df.index.names))
            )

            # Reorder columns
            combined_df = combined_df[list(left_columns) + list(right.columns)]
            return combined_df

        # Join left index with right index
        if left._block.index.nlevels != right._block.index.nlevels:
            raise ValueError("Index to join on must have the same number of levels.")

        return left._perform_join_by_index(right, how=how)

    def _perform_join_by_index(
        self, other: Union[DataFrame, indexes.Index], *, how: str = "left"
    ):
        block, _ = self._block.join(other._block, how=how, block_identity_join=True)
        return DataFrame(block)

    def rolling(self, window: int, min_periods=None) -> bigframes.core.window.Window:
        # To get n size window, need current row and n-1 preceding rows.
        window_spec = bigframes.core.WindowSpec(
            preceding=window - 1, following=0, min_periods=min_periods or window
        )
        return bigframes.core.window.Window(
            self._block, window_spec, self._block.value_columns
        )

    def expanding(self, min_periods: int = 1) -> bigframes.core.window.Window:
        window_spec = bigframes.core.WindowSpec(following=0, min_periods=min_periods)
        return bigframes.core.window.Window(
            self._block, window_spec, self._block.value_columns
        )

    def groupby(
        self,
        by: typing.Union[
            blocks.Label,
            bigframes.series.Series,
            typing.Sequence[typing.Union[blocks.Label, bigframes.series.Series]],
            None,
        ] = None,
        *,
        level: typing.Optional[LevelsType] = None,
        as_index: bool = True,
        dropna: bool = True,
    ) -> groupby.DataFrameGroupBy:
        if (by is not None) and (level is not None):
            raise ValueError("Do not specify both 'by' and 'level'")
        if by is not None:
            return self._groupby_series(by, as_index=as_index, dropna=dropna)
        if level is not None:
            return self._groupby_level(level, as_index=as_index, dropna=dropna)
        else:
            raise TypeError("You have to supply one of 'by' and 'level'")

    def _groupby_level(
        self,
        level: LevelsType,
        as_index: bool = True,
        dropna: bool = True,
    ):
        return groupby.DataFrameGroupBy(
            self._block,
            by_col_ids=self._resolve_levels(level),
            as_index=as_index,
            dropna=dropna,
        )

    def _groupby_series(
        self,
        by: typing.Union[
            blocks.Label,
            bigframes.series.Series,
            typing.Sequence[typing.Union[blocks.Label, bigframes.series.Series]],
        ],
        as_index: bool = True,
        dropna: bool = True,
    ):
        if not isinstance(by, bigframes.series.Series) and utils.is_list_like(by):
            by = list(by)
        else:
            by = [typing.cast(typing.Union[blocks.Label, bigframes.series.Series], by)]

        block = self._block
        col_ids: typing.Sequence[str] = []
        for key in by:
            if isinstance(key, bigframes.series.Series):
                block, (
                    get_column_left,
                    get_column_right,
                ) = block.join(key._block, how="inner" if dropna else "left")
                col_ids = [
                    *[get_column_left[value] for value in col_ids],
                    get_column_right[key._value_column],
                ]
            else:
                # Interpret as index level or column name
                col_matches = block.label_to_col_id.get(key, [])
                level_matches = block.index_name_to_col_id.get(key, [])
                matches = [*col_matches, *level_matches]
                if len(matches) != 1:
                    raise ValueError(
                        f"GroupBy key {key} does not match a unique column or index level. BigQuery DataFrames only interprets lists of strings as column or index names, not directly as per-row group assignments."
                    )
                col_ids = [*col_ids, matches[0]]

        return groupby.DataFrameGroupBy(
            block,
            by_col_ids=col_ids,
            as_index=as_index,
            dropna=dropna,
        )

    def abs(self) -> DataFrame:
        return self._apply_unary_op(ops.abs_op)

    def isna(self) -> DataFrame:
        return self._apply_unary_op(ops.isnull_op)

    isnull = isna
    isnull.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.isna)

    def notna(self) -> DataFrame:
        return self._apply_unary_op(ops.notnull_op)

    notnull = notna
    notnull.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.notna)

    def cumsum(self):
        is_numeric_types = [
            (dtype in bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_PERMISSIVE)
            for _, dtype in self.dtypes.items()
        ]
        if not all(is_numeric_types):
            raise ValueError("All values must be numeric to apply cumsum.")
        return self._apply_window_op(
            agg_ops.sum_op,
            bigframes.core.WindowSpec(following=0),
        )

    def cumprod(self) -> DataFrame:
        is_numeric_types = [
            (dtype in bigframes.dtypes.NUMERIC_BIGFRAMES_TYPES_PERMISSIVE)
            for _, dtype in self.dtypes.items()
        ]
        if not all(is_numeric_types):
            raise ValueError("All values must be numeric to apply cumsum.")
        return self._apply_window_op(
            agg_ops.product_op,
            bigframes.core.WindowSpec(following=0),
        )

    def cummin(self) -> DataFrame:
        return self._apply_window_op(
            agg_ops.min_op,
            bigframes.core.WindowSpec(following=0),
        )

    def cummax(self) -> DataFrame:
        return self._apply_window_op(
            agg_ops.max_op,
            bigframes.core.WindowSpec(following=0),
        )

    def shift(self, periods: int = 1) -> DataFrame:
        window = bigframes.core.WindowSpec(
            preceding=periods if periods > 0 else None,
            following=-periods if periods < 0 else None,
        )
        return self._apply_window_op(agg_ops.ShiftOp(periods), window)

    def diff(self, periods: int = 1) -> DataFrame:
        window = bigframes.core.WindowSpec(
            preceding=periods if periods > 0 else None,
            following=-periods if periods < 0 else None,
        )
        return self._apply_window_op(agg_ops.DiffOp(periods), window)

    def pct_change(self, periods: int = 1) -> DataFrame:
        # Future versions of pandas will not perfrom ffill automatically
        df = self.ffill()
        return DataFrame(block_ops.pct_change(df._block, periods=periods))

    def _apply_window_op(
        self,
        op: agg_ops.WindowOp,
        window_spec: bigframes.core.WindowSpec,
    ):
        block, result_ids = self._block.multi_apply_window_op(
            self._block.value_columns,
            op,
            window_spec=window_spec,
        )
        return DataFrame(block.select_columns(result_ids))

    def sample(
        self,
        n: Optional[int] = None,
        frac: Optional[float] = None,
        *,
        random_state: Optional[int] = None,
        sort: Optional[bool | Literal["random"]] = "random",
    ) -> DataFrame:
        if n is not None and frac is not None:
            raise ValueError("Only one of 'n' or 'frac' parameter can be specified.")

        ns = (n,) if n is not None else ()
        fracs = (frac,) if frac is not None else ()
        return DataFrame(
            self._block._split(
                ns=ns, fracs=fracs, random_state=random_state, sort=sort
            )[0]
        )

    def explode(
        self,
        column: typing.Union[blocks.Label, typing.Sequence[blocks.Label]],
        *,
        ignore_index: Optional[bool] = False,
    ) -> DataFrame:
        if not utils.is_list_like(column):
            column_labels = typing.cast(typing.Sequence[blocks.Label], (column,))
        else:
            column_labels = typing.cast(typing.Sequence[blocks.Label], tuple(column))

        if not column_labels:
            raise ValueError("column must be nonempty")
        if len(column_labels) > len(set(column_labels)):
            raise ValueError("column must be unique")

        column_ids = [self._resolve_label_exact(label) for label in column_labels]
        missing = [
            column_labels[i] for i in range(len(column_ids)) if column_ids[i] is None
        ]
        if len(missing) > 0:
            raise KeyError(f"None of {missing} are in the columns")

        return DataFrame(
            self._block.explode(
                column_ids=typing.cast(typing.Sequence[str], tuple(column_ids)),
                ignore_index=ignore_index,
            )
        )

    def _split(
        self,
        ns: Iterable[int] = (),
        fracs: Iterable[float] = (),
        *,
        random_state: Optional[int] = None,
    ) -> List[DataFrame]:
        """Internal function to support splitting DF to multiple parts along index axis.

        At most one of ns and fracs can be passed in. If neither, default to ns = (1,).
        Return a list of sampled DataFrames.
        """
        blocks = self._block._split(ns=ns, fracs=fracs, random_state=random_state)
        return [DataFrame(block) for block in blocks]

    @classmethod
    def from_dict(
        cls,
        data: dict,
        orient: str = "columns",
        dtype=None,
        columns=None,
    ) -> DataFrame:
        return cls(pandas.DataFrame.from_dict(data, orient, dtype, columns))  # type: ignore

    @classmethod
    def from_records(
        cls,
        data,
        index=None,
        exclude=None,
        columns=None,
        coerce_float: bool = False,
        nrows: int | None = None,
    ) -> DataFrame:
        return cls(
            pandas.DataFrame.from_records(
                data, index, exclude, columns, coerce_float, nrows
            )
        )

    def to_csv(
        self, path_or_buf: str, sep=",", *, header: bool = True, index: bool = True
    ) -> None:
        # TODO(swast): Can we support partition columns argument?
        # TODO(chelsealin): Support local file paths.
        # TODO(swast): Some warning that wildcard is recommended for large
        # query results? See:
        # https://cloud.google.com/bigquery/docs/exporting-data#limit_the_exported_file_size
        if not path_or_buf.startswith("gs://"):
            raise NotImplementedError(ERROR_IO_ONLY_GS_PATHS)
        if "*" not in path_or_buf:
            raise NotImplementedError(ERROR_IO_REQUIRES_WILDCARD)

        result_table = self._run_io_query(
            index=index, ordering_id=bigframes.session._io.bigquery.IO_ORDERING_ID
        )
        export_data_statement = bigframes.session._io.bigquery.create_export_csv_statement(
            f"{result_table.project}.{result_table.dataset_id}.{result_table.table_id}",
            uri=path_or_buf,
            field_delimiter=sep,
            header=header,
        )
        _, query_job = self._block.expr.session._start_query(export_data_statement)
        self._set_internal_query_job(query_job)

    def to_json(
        self,
        path_or_buf: str,
        orient: Literal[
            "split", "records", "index", "columns", "values", "table"
        ] = "columns",
        *,
        lines: bool = False,
        index: bool = True,
    ) -> None:
        # TODO(swast): Can we support partition columns argument?
        # TODO(chelsealin): Support local file paths.
        if not path_or_buf.startswith("gs://"):
            raise NotImplementedError(ERROR_IO_ONLY_GS_PATHS)

        if "*" not in path_or_buf:
            raise NotImplementedError(ERROR_IO_REQUIRES_WILDCARD)

        # TODO(ashleyxu) Support lines=False for small tables with arrays and TO_JSON_STRING.
        # See: https://cloud.google.com/bigquery/docs/reference/standard-sql/json_functions#to_json_string
        if lines is False:
            raise NotImplementedError(
                f"Only newline-delimited JSON is supported. Add `lines=True` to your function call. {constants.FEEDBACK_LINK}"
            )

        if lines is True and orient != "records":
            raise ValueError(
                "'lines' keyword is only valid when 'orient' is 'records'."
            )

        result_table = self._run_io_query(
            index=index, ordering_id=bigframes.session._io.bigquery.IO_ORDERING_ID
        )
        export_data_statement = bigframes.session._io.bigquery.create_export_data_statement(
            f"{result_table.project}.{result_table.dataset_id}.{result_table.table_id}",
            uri=path_or_buf,
            format="JSON",
            export_options={},
        )
        _, query_job = self._block.expr.session._start_query(export_data_statement)
        self._set_internal_query_job(query_job)

    def to_gbq(
        self,
        destination_table: Optional[str] = None,
        *,
        if_exists: Optional[Literal["fail", "replace", "append"]] = None,
        index: bool = True,
        ordering_id: Optional[str] = None,
        clustering_columns: Union[pandas.Index, Iterable[typing.Hashable]] = (),
    ) -> str:
        dispositions = {
            "fail": bigquery.WriteDisposition.WRITE_EMPTY,
            "replace": bigquery.WriteDisposition.WRITE_TRUNCATE,
            "append": bigquery.WriteDisposition.WRITE_APPEND,
        }

        temp_table_ref = None

        if destination_table is None:
            if if_exists is not None and if_exists != "replace":
                raise ValueError(
                    f"Got invalid value {repr(if_exists)} for if_exists. "
                    "When no destination table is specified, a new table is always created. "
                    "None or 'replace' are the only valid options in this case."
                )
            if_exists = "replace"

            temp_table_ref = bigframes.session._io.bigquery.random_table(
                self._session._anonymous_dataset
            )
            destination_table = f"{temp_table_ref.project}.{temp_table_ref.dataset_id}.{temp_table_ref.table_id}"

        table_parts = destination_table.split(".")
        default_project = self._block.expr.session.bqclient.project

        if len(table_parts) == 2:
            destination_dataset = f"{default_project}.{table_parts[0]}"
        elif len(table_parts) == 3:
            destination_dataset = f"{table_parts[0]}.{table_parts[1]}"
        else:
            raise ValueError(
                f"Got invalid value for destination_table {repr(destination_table)}. "
                "Should be of the form 'datasetId.tableId' or 'projectId.datasetId.tableId'."
            )

        if if_exists is None:
            if_exists = "fail"

        if if_exists not in dispositions:
            raise ValueError(
                f"Got invalid value {repr(if_exists)} for if_exists. "
                f"Valid options include None or one of {dispositions.keys()}."
            )

        try:
            self._session.bqclient.get_dataset(destination_dataset)
        except google.api_core.exceptions.NotFound:
            self._session.bqclient.create_dataset(destination_dataset, exists_ok=True)

        clustering_fields = self._map_clustering_columns(
            clustering_columns, index=index
        )

        job_config = bigquery.QueryJobConfig(
            write_disposition=dispositions[if_exists],
            destination=bigquery.table.TableReference.from_string(
                destination_table,
                default_project=default_project,
            ),
            clustering_fields=clustering_fields if clustering_fields else None,
        )

        self._run_io_query(index=index, ordering_id=ordering_id, job_config=job_config)

        if temp_table_ref:
            bigframes.session._io.bigquery.set_table_expiration(
                self._session.bqclient,
                temp_table_ref,
                datetime.datetime.now(datetime.timezone.utc)
                + constants.DEFAULT_EXPIRATION,
            )

        return destination_table

    def to_numpy(
        self, dtype=None, copy=False, na_value=None, **kwargs
    ) -> numpy.ndarray:
        return self.to_pandas().to_numpy(dtype, copy, na_value, **kwargs)

    def __array__(self, dtype=None) -> numpy.ndarray:
        return self.to_numpy(dtype=dtype)

    __array__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__array__)

    def to_parquet(
        self,
        path: str,
        *,
        compression: Optional[Literal["snappy", "gzip"]] = "snappy",
        index: bool = True,
    ) -> None:
        # TODO(swast): Can we support partition columns argument?
        # TODO(chelsealin): Support local file paths.
        # TODO(swast): Some warning that wildcard is recommended for large
        # query results? See:
        # https://cloud.google.com/bigquery/docs/exporting-data#limit_the_exported_file_size
        if not path.startswith("gs://"):
            raise NotImplementedError(ERROR_IO_ONLY_GS_PATHS)

        if "*" not in path:
            raise NotImplementedError(ERROR_IO_REQUIRES_WILDCARD)

        if compression not in {None, "snappy", "gzip"}:
            raise ValueError("'{0}' is not valid for compression".format(compression))

        export_options: Dict[str, Union[bool, str]] = {}
        if compression:
            export_options["compression"] = compression.upper()

        result_table = self._run_io_query(
            index=index, ordering_id=bigframes.session._io.bigquery.IO_ORDERING_ID
        )
        export_data_statement = bigframes.session._io.bigquery.create_export_data_statement(
            f"{result_table.project}.{result_table.dataset_id}.{result_table.table_id}",
            uri=path,
            format="PARQUET",
            export_options=export_options,
        )
        _, query_job = self._block.expr.session._start_query(export_data_statement)
        self._set_internal_query_job(query_job)

    def to_dict(
        self,
        orient: Literal[
            "dict", "list", "series", "split", "tight", "records", "index"
        ] = "dict",
        into: type[dict] = dict,
        **kwargs,
    ) -> dict | list[dict]:
        return self.to_pandas().to_dict(orient, into, **kwargs)  # type: ignore

    def to_excel(self, excel_writer, sheet_name: str = "Sheet1", **kwargs) -> None:
        return self.to_pandas().to_excel(excel_writer, sheet_name, **kwargs)

    def to_latex(
        self,
        buf=None,
        columns: Sequence | None = None,
        header: bool | Sequence[str] = True,
        index: bool = True,
        **kwargs,
    ) -> str | None:
        return self.to_pandas().to_latex(
            buf, columns=columns, header=header, index=index, **kwargs  # type: ignore
        )

    def to_records(
        self, index: bool = True, column_dtypes=None, index_dtypes=None
    ) -> numpy.recarray:
        return self.to_pandas().to_records(index, column_dtypes, index_dtypes)

    def to_string(
        self,
        buf=None,
        columns: Sequence[str] | None = None,
        col_space=None,
        header: bool | Sequence[str] = True,
        index: bool = True,
        na_rep: str = "NaN",
        formatters=None,
        float_format=None,
        sparsify: bool | None = None,
        index_names: bool = True,
        justify: str | None = None,
        max_rows: int | None = None,
        max_cols: int | None = None,
        show_dimensions: bool = False,
        decimal: str = ".",
        line_width: int | None = None,
        min_rows: int | None = None,
        max_colwidth: int | None = None,
        encoding: str | None = None,
    ) -> str | None:
        return self.to_pandas().to_string(
            buf,
            columns,  # type: ignore
            col_space,
            header,  # type: ignore
            index,
            na_rep,
            formatters,
            float_format,
            sparsify,
            index_names,
            justify,
            max_rows,
            max_cols,
            show_dimensions,
            decimal,
            line_width,
            min_rows,
            max_colwidth,
            encoding,
        )

    def to_html(
        self,
        buf=None,
        columns: Sequence[str] | None = None,
        col_space=None,
        header: bool = True,
        index: bool = True,
        na_rep: str = "NaN",
        formatters=None,
        float_format=None,
        sparsify: bool | None = None,
        index_names: bool = True,
        justify: str | None = None,
        max_rows: int | None = None,
        max_cols: int | None = None,
        show_dimensions: bool = False,
        decimal: str = ".",
        bold_rows: bool = True,
        classes: str | list | tuple | None = None,
        escape: bool = True,
        notebook: bool = False,
        border: int | None = None,
        table_id: str | None = None,
        render_links: bool = False,
        encoding: str | None = None,
    ) -> str:
        return self.to_pandas().to_html(
            buf,
            columns,  # type: ignore
            col_space,
            header,
            index,
            na_rep,
            formatters,
            float_format,
            sparsify,
            index_names,
            justify,  # type: ignore
            max_rows,
            max_cols,
            show_dimensions,
            decimal,
            bold_rows,
            classes,
            escape,
            notebook,
            border,
            table_id,
            render_links,
            encoding,
        )

    def to_markdown(
        self,
        buf=None,
        mode: str = "wt",
        index: bool = True,
        **kwargs,
    ) -> str | None:
        return self.to_pandas().to_markdown(buf, mode, index, **kwargs)  # type: ignore

    def to_pickle(self, path, **kwargs) -> None:
        return self.to_pandas().to_pickle(path, **kwargs)

    def to_orc(self, path=None, **kwargs) -> bytes | None:
        as_pandas = self.to_pandas()
        # to_orc only works with default index
        as_pandas_default_index = as_pandas.reset_index()
        return as_pandas_default_index.to_orc(path, **kwargs)

    def _apply_unary_op(self, operation: ops.UnaryOp) -> DataFrame:
        block = self._block.multi_apply_unary_op(self._block.value_columns, operation)
        return DataFrame(block)

    def _map_clustering_columns(
        self,
        clustering_columns: Union[pandas.Index, Iterable[typing.Hashable]],
        index: bool,
    ) -> List[str]:
        """Maps the provided clustering columns to the existing columns in the DataFrame."""

        def map_columns_on_occurrence(columns):
            mapped_columns = []
            for col in clustering_columns:
                if col in columns:
                    count = columns.count(col)
                    mapped_columns.extend([col] * count)
            return mapped_columns

        if not clustering_columns:
            return []

        if len(list(clustering_columns)) != len(set(clustering_columns)):
            raise ValueError("Duplicates are not supported in clustering_columns")

        all_possible_columns = (
            (set(self.columns) | set(self.index.names)) if index else set(self.columns)
        )
        missing_columns = set(clustering_columns) - all_possible_columns
        if missing_columns:
            raise ValueError(
                f"Clustering columns not found in DataFrame: {missing_columns}"
            )

        clustering_columns_for_df = map_columns_on_occurrence(
            list(self._block.column_labels)
        )
        clustering_columns_for_index = (
            map_columns_on_occurrence(list(self.index.names)) if index else []
        )

        (
            clustering_columns_for_df,
            clustering_columns_for_index,
        ) = utils.get_standardized_ids(
            clustering_columns_for_df, clustering_columns_for_index
        )

        return clustering_columns_for_index + clustering_columns_for_df

    def _prepare_export(
        self, index: bool, ordering_id: Optional[str]
    ) -> Tuple[bigframes.core.ArrayValue, Dict[str, str]]:
        array_value = self._block.expr

        new_col_labels, new_idx_labels = utils.get_standardized_ids(
            self._block.column_labels, self.index.names
        )

        columns = list(self._block.value_columns)
        column_labels = new_col_labels
        # This code drops unnamed indexes to keep consistent with the behavior of
        # most pandas write APIs. The exception is `pandas.to_csv`, which keeps
        # unnamed indexes as `Unnamed: 0`.
        # TODO(chelsealin): check if works for multiple indexes.
        if index and self.index.name is not None:
            columns.extend(self._block.index_columns)
            column_labels.extend(new_idx_labels)
        else:
            array_value = array_value.drop_columns(self._block.index_columns)

        # Make columns in SQL reflect _labels_ not _ids_. Note: This may use
        # the arbitrary unicode column labels feature in BigQuery, which is
        # currently (June 2023) in preview.
        id_overrides = {
            col_id: col_label for col_id, col_label in zip(columns, column_labels)
        }

        if ordering_id is not None:
            array_value = array_value.promote_offsets(ordering_id)
        return array_value, id_overrides

    def _run_io_query(
        self,
        index: bool,
        ordering_id: Optional[str] = None,
        job_config: Optional[bigquery.job.QueryJobConfig] = None,
    ) -> bigquery.TableReference:
        """Executes a query job presenting this dataframe and returns the destination
        table."""
        session = self._block.expr.session
        self._optimize_query_complexity()
        export_array, id_overrides = self._prepare_export(
            index=index, ordering_id=ordering_id
        )

        _, query_job = session._execute(
            export_array,
            job_config=job_config,
            sorted=False,
            col_id_overrides=id_overrides,
        )
        self._set_internal_query_job(query_job)

        # The query job should have finished, so there should be always be a result table.
        result_table = query_job.destination
        assert result_table is not None
        return result_table

    def map(self, func, na_action: Optional[str] = None) -> DataFrame:
        if not callable(func):
            raise TypeError("the first argument must be callable")

        if na_action not in {None, "ignore"}:
            raise ValueError(f"na_action={na_action} not supported")

        # TODO(shobs): Support **kwargs
        # Reproject as workaround to applying filter too late. This forces the filter
        # to be applied before passing data to remote function, protecting from bad
        # inputs causing errors.
        reprojected_df = DataFrame(self._block._force_reproject())
        return reprojected_df._apply_unary_op(
            ops.RemoteFunctionOp(func=func, apply_on_null=(na_action is None))
        )

    def apply(self, func, *, args: typing.Tuple = (), **kwargs):
        results = {name: func(col, *args, **kwargs) for name, col in self.items()}
        if all(
            [
                isinstance(val, bigframes.series.Series) or utils.is_list_like(val)
                for val in results.values()
            ]
        ):
            return DataFrame(data=results)
        else:
            return pandas.Series(data=results)

    def drop_duplicates(
        self,
        subset: typing.Union[blocks.Label, typing.Sequence[blocks.Label]] = None,
        *,
        keep: str = "first",
    ) -> DataFrame:
        if subset is None:
            column_ids = self._block.value_columns
        elif utils.is_list_like(subset):
            column_ids = [
                id for label in subset for id in self._block.label_to_col_id[label]
            ]
        else:
            # interpret as single label
            column_ids = self._block.label_to_col_id[typing.cast(blocks.Label, subset)]
        block = block_ops.drop_duplicates(self._block, column_ids, keep)
        return DataFrame(block)

    def duplicated(self, subset=None, keep: str = "first") -> bigframes.series.Series:
        if subset is None:
            column_ids = self._block.value_columns
        else:
            column_ids = [
                id for label in subset for id in self._block.label_to_col_id[label]
            ]
        block, indicator = block_ops.indicate_duplicates(self._block, column_ids, keep)
        return bigframes.series.Series(
            block.select_column(
                indicator,
            )
        )

    def rank(
        self,
        axis=0,
        method: str = "average",
        numeric_only=False,
        na_option: str = "keep",
        ascending=True,
    ) -> DataFrame:
        df = self._drop_non_numeric() if numeric_only else self
        return DataFrame(block_ops.rank(df._block, method, na_option, ascending))

    def first_valid_index(self):
        return

    applymap = map
    applymap.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.map)

    def _slice(
        self,
        start: typing.Optional[int] = None,
        stop: typing.Optional[int] = None,
        step: typing.Optional[int] = None,
    ) -> DataFrame:
        block = self._block.slice(start=start, stop=stop, step=step)
        return DataFrame(block)

    def __array_ufunc__(
        self, ufunc: numpy.ufunc, method: str, *inputs, **kwargs
    ) -> DataFrame:
        """Used to support numpy ufuncs.
        See: https://numpy.org/doc/stable/reference/ufuncs.html
        """
        if method != "__call__" or len(inputs) > 2 or len(kwargs) > 0:
            return NotImplemented

        if len(inputs) == 1 and ufunc in ops.NUMPY_TO_OP:
            return self._apply_unary_op(ops.NUMPY_TO_OP[ufunc])
        if len(inputs) == 2 and ufunc in ops.NUMPY_TO_BINOP:
            binop = ops.NUMPY_TO_BINOP[ufunc]
            if inputs[0] is self:
                return self._apply_binop(inputs[1], binop)
            else:
                return self._apply_binop(inputs[0], binop, reverse=True)

        return NotImplemented

    def _set_block(self, block: blocks.Block):
        self._block = block

    def _get_block(self) -> blocks.Block:
        return self._block

    def _cached(self, *, force: bool = False) -> DataFrame:
        """Materialize dataframe to a temporary table.
        No-op if the dataframe represents a trivial transformation of an existing materialization.
        Force=True is used for BQML integration where need to copy data rather than use snapshot.
        """
        self._set_block(self._block.cached(force=force))
        return self

    def _optimize_query_complexity(self):
        """Reduce query complexity by caching repeated subtrees and recursively materializing maximum-complexity subtrees.
        May generate many queries and take substantial time to execute.
        """
        # TODO: Move all this to session
        new_expr = self._session._simplify_with_caching(self._block.expr)
        self._set_block(self._block.swap_array_expr(new_expr))

    _DataFrameOrSeries = typing.TypeVar("_DataFrameOrSeries")

    def dot(self, other: _DataFrameOrSeries) -> _DataFrameOrSeries:
        if not isinstance(other, (DataFrame, bf_series.Series)):
            raise NotImplementedError(
                f"Only DataFrame or Series operand is supported. {constants.FEEDBACK_LINK}"
            )

        if len(self.index.names) > 1 or len(other.index.names) > 1:
            raise NotImplementedError(
                f"Multi-index input is not supported. {constants.FEEDBACK_LINK}"
            )

        if len(self.columns.names) > 1 or (
            isinstance(other, DataFrame) and len(other.columns.names) > 1
        ):
            raise NotImplementedError(
                f"Multi-level column input is not supported. {constants.FEEDBACK_LINK}"
            )

        # Convert the dataframes into cell-value-decomposed representation, i.e.
        # each cell value is present in a separate row
        row_id = "row"
        col_id = "col"
        val_id = "val"
        left_suffix = "_left"
        right_suffix = "_right"
        cvd_columns = [row_id, col_id, val_id]

        def get_left_id(id):
            return f"{id}{left_suffix}"

        def get_right_id(id):
            return f"{id}{right_suffix}"

        other_frame = other if isinstance(other, DataFrame) else other.to_frame()

        left = self.stack().reset_index()
        left.columns = cvd_columns

        right = other_frame.stack().reset_index()
        right.columns = cvd_columns

        merged = left.merge(
            right,
            left_on=col_id,
            right_on=row_id,
            suffixes=(left_suffix, right_suffix),
        )

        left_row_id = get_left_id(row_id)
        right_col_id = get_right_id(col_id)

        aggregated = (
            merged.assign(
                val=merged[get_left_id(val_id)] * merged[get_right_id(val_id)]
            )[[left_row_id, right_col_id, val_id]]
            .groupby([left_row_id, right_col_id])
            .sum(numeric_only=True)
        )
        aggregated_noindex = aggregated.reset_index()
        aggregated_noindex.columns = cvd_columns
        result = aggregated_noindex._pivot(
            columns=col_id, columns_unique_values=other_frame.columns, index=row_id
        )

        # Set the index names to match the left side matrix
        result.index.names = self.index.names

        # Pivot has the result columns ordered alphabetically. It should still
        # match the columns in the right sided matrix. Let's reorder them as per
        # the right side matrix
        if not result.columns.difference(other_frame.columns).empty:
            raise RuntimeError(
                f"Could not construct all columns. {constants.FEEDBACK_LINK}"
            )
        result = result[other_frame.columns]

        if isinstance(other, bf_series.Series):
            # There should be exactly one column in the result
            result = result[result.columns[0]].rename()

        return result

    @property
    def plot(self):
        return plotting.PlotAccessor(self)

    def __matmul__(self, other) -> DataFrame:
        return self.dot(other)

    __matmul__.__doc__ = inspect.getdoc(vendored_pandas_frame.DataFrame.__matmul__)
