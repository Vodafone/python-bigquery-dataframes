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
from __future__ import annotations

import abc
import functools
import itertools
import textwrap
import typing
from typing import Collection, Iterable, Literal, Optional, Sequence

import bigframes_vendored.ibis.expr.operations as vendored_ibis_ops
import ibis
import ibis.backends.bigquery as ibis_bigquery
import ibis.common.deferred  # type: ignore
import ibis.expr.datatypes as ibis_dtypes
import ibis.expr.types as ibis_types
import pandas

import bigframes.core.compile.aggregate_compiler as agg_compiler
import bigframes.core.compile.scalar_op_compiler as op_compilers
import bigframes.core.expression as ex
import bigframes.core.guid
from bigframes.core.ordering import (
    ascending_over,
    encode_order_string,
    ExpressionOrdering,
    IntegerEncoding,
    OrderingExpression,
)
import bigframes.core.schema as schemata
from bigframes.core.window_spec import WindowSpec
import bigframes.dtypes
import bigframes.operations.aggregations as agg_ops

ORDER_ID_COLUMN = "bigframes_ordering_id"
PREDICATE_COLUMN = "bigframes_predicate"


T = typing.TypeVar("T", bound="BaseIbisIR")

op_compiler = op_compilers.scalar_op_compiler


class BaseIbisIR(abc.ABC):
    """Implementation detail, contains common logic between ordered and unordered IR"""

    def __init__(
        self,
        table: ibis_types.Table,
        columns: Sequence[ibis_types.Value],
        predicates: Optional[Collection[ibis_types.BooleanValue]] = None,
    ):
        self._table = table
        self._predicates = tuple(predicates) if predicates is not None else ()
        # Allow creating a DataFrame directly from an Ibis table expression.
        # TODO(swast): Validate that each column references the same table (or
        # no table for literal values).
        self._columns = tuple(columns)
        # To allow for more efficient lookup by column name, create a
        # dictionary mapping names to column values.
        self._column_names = {
            (
                column.resolve(table)
                # TODO(https://github.com/ibis-project/ibis/issues/7613): use
                # public API to refer to Deferred type.
                if isinstance(column, ibis.common.deferred.Deferred)
                else column
            ).get_name(): column
            for column in self._columns
        }

    @property
    def columns(self) -> typing.Tuple[ibis_types.Value, ...]:
        return self._columns

    @property
    def column_ids(self) -> typing.Sequence[str]:
        return tuple(self._column_names.keys())

    @property
    def _reduced_predicate(self) -> typing.Optional[ibis_types.BooleanValue]:
        """Returns the frame's predicates as an equivalent boolean value, useful where a single predicate value is preferred."""
        return (
            _reduce_predicate_list(self._predicates).name(PREDICATE_COLUMN)
            if self._predicates
            else None
        )

    @property
    def _ibis_bindings(self) -> dict[str, ibis_types.Value]:
        return {col: self._get_ibis_column(col) for col in self.column_ids}

    @abc.abstractmethod
    def filter(self: T, predicate: ex.Expression) -> T:
        """Filter the table on a given expression, the predicate must be a boolean expression."""
        ...

    @abc.abstractmethod
    def _reproject_to_table(self: T) -> T:
        """
        Internal operators that projects the internal representation into a
        new ibis table expression where each value column is a direct
        reference to a column in that table expression. Needed after
        some operations such as window operations that cannot be used
        recursively in projections.
        """
        ...

    def projection(
        self: T,
        expression_id_pairs: typing.Tuple[typing.Tuple[ex.Expression, str], ...],
    ) -> T:
        """Apply an expression to the ArrayValue and assign the output to a column."""
        bindings = {col: self._get_ibis_column(col) for col in self.column_ids}
        values = [
            op_compiler.compile_expression(expression, bindings).name(id)
            for expression, id in expression_id_pairs
        ]
        result = self._select(tuple(values))  # type: ignore

        # Need to reproject to convert ibis Scalar to ibis Column object
        if any(exp_id[0].is_const for exp_id in expression_id_pairs):
            result = result._reproject_to_table()
        return result

    @abc.abstractmethod
    def _select(self: T, values: typing.Tuple[ibis_types.Value]) -> T:
        ...

    @abc.abstractmethod
    def _set_or_replace_by_id(self: T, id: str, new_value: ibis_types.Value) -> T:
        ...

    def _get_ibis_column(self, key: str) -> ibis_types.Value:
        """Gets the Ibis expression for a given column."""
        if key not in self.column_ids:
            raise ValueError(
                "Column name {} not in set of values: {}".format(key, self.column_ids)
            )
        return typing.cast(
            ibis_types.Value,
            bigframes.dtypes.ibis_value_to_canonical_type(self._column_names[key]),
        )

    def get_column_type(self, key: str) -> bigframes.dtypes.Dtype:
        ibis_type = typing.cast(
            bigframes.dtypes.IbisDtype, self._get_ibis_column(key).type()
        )
        return typing.cast(
            bigframes.dtypes.Dtype,
            bigframes.dtypes.ibis_dtype_to_bigframes_dtype(ibis_type),
        )


# Ibis Implementations
class UnorderedIR(BaseIbisIR):
    def __init__(
        self,
        table: ibis_types.Table,
        columns: Sequence[ibis_types.Value],
        predicates: Optional[Collection[ibis_types.BooleanValue]] = None,
    ):
        super().__init__(table, columns, predicates)

    def builder(self):
        """Creates a mutable builder for expressions."""
        # Since ArrayValue is intended to be immutable (immutability offers
        # potential opportunities for caching, though we might need to introduce
        # more node types for that to be useful), we create a builder class.
        return UnorderedIR.Builder(
            self._table,
            columns=self._columns,
            predicates=self._predicates,
        )

    def peek_sql(self, n: int):
        # Peek currently implemented as top level LIMIT op.
        # Execution engine handles limit pushdown.
        # In future, may push down limit/filters in compilation.
        sql = ibis_bigquery.Backend().compile(self._to_ibis_expr().limit(n))
        return typing.cast(str, sql)

    def to_sql(
        self,
        offset_column: typing.Optional[str] = None,
        col_id_overrides: typing.Mapping[str, str] = {},
        sorted: bool = False,
    ) -> str:
        if offset_column or sorted:
            raise ValueError("Cannot produce sorted sql in unordered mode")
        sql = ibis_bigquery.Backend().compile(
            self._to_ibis_expr(
                col_id_overrides=col_id_overrides,
            )
        )
        return typing.cast(str, sql)

    def row_count(self) -> OrderedIR:
        original_table = self._to_ibis_expr()
        ibis_table = original_table.agg(
            [
                original_table.count().name("count"),
            ]
        )
        return OrderedIR(
            ibis_table,
            (ibis_table["count"],),
            ordering=ExpressionOrdering(
                ordering_value_columns=(ascending_over("count"),),
                total_ordering_columns=frozenset(["count"]),
            ),
        )

    def _to_ibis_expr(
        self,
        *,
        expose_hidden_cols: bool = False,
        fraction: Optional[float] = None,
        col_id_overrides: typing.Mapping[str, str] = {},
    ):
        """
        Creates an Ibis table expression representing the DataFrame.

        ArrayValue objects are sorted, so the following options are available
        to reflect this in the ibis expression.

        * "offset_col": Zero-based offsets are generated as a column, this will
          not sort the rows however.
        * "string_encoded": An ordered string column is provided in output table.
        * "unordered": No ordering information will be provided in output. Only
          value columns are projected.

        For offset or ordered column, order_col_name can be used to assign the
        output label for the ordering column. If none is specified, the default
        column name will be 'bigframes_ordering_id'

        Args:
            expose_hidden_cols:
                If True, include the hidden ordering columns in the results.
                Only compatible with `order_by` and `unordered`
                ``ordering_mode``.
            col_id_overrides:
                overrides the column ids for the result
        Returns:
            An ibis expression representing the data help by the ArrayValue object.
        """
        columns = list(self._columns)
        columns_to_drop: list[
            str
        ] = []  # Ordering/Filtering columns that will be dropped at end

        if self._reduced_predicate is not None:
            columns.append(self._reduced_predicate)
            # Usually drop predicate as it is will be all TRUE after filtering
            if not expose_hidden_cols:
                columns_to_drop.append(self._reduced_predicate.get_name())

        # Special case for empty tables, since we can't create an empty
        # projection.
        if not columns:
            return ibis.memtable([])

        # Make sure all dtypes are the "canonical" ones for BigFrames. This is
        # important for operations like UNION where the schema must match.
        table = self._table.select(
            bigframes.dtypes.ibis_value_to_canonical_type(column) for column in columns
        )
        base_table = table
        if self._reduced_predicate is not None:
            table = table.filter(base_table[PREDICATE_COLUMN])
        table = table.drop(*columns_to_drop)
        if col_id_overrides:
            table = table.rename(
                {value: key for key, value in col_id_overrides.items()}
            )
        if fraction is not None:
            table = table.filter(ibis.random() < ibis.literal(fraction))
        return table

    def filter(self, predicate: ex.Expression) -> UnorderedIR:
        bindings = {col: self._get_ibis_column(col) for col in self.column_ids}
        condition = op_compiler.compile_expression(predicate, bindings)
        return self._filter(condition)

    def _filter(self, predicate_value: ibis_types.BooleanValue) -> UnorderedIR:
        """Filter the table on a given expression, the predicate must be a boolean series aligned with the table expression."""
        expr = self.builder()
        expr.predicates = [*self._predicates, predicate_value]
        return expr.build()

    def aggregate(
        self,
        aggregations: typing.Sequence[typing.Tuple[ex.Aggregation, str]],
        by_column_ids: typing.Sequence[str] = (),
        dropna: bool = True,
    ) -> OrderedIR:
        """
        Apply aggregations to the expression.
        Arguments:
            aggregations: input_column_id, operation, output_column_id tuples
            by_column_id: column id of the aggregation key, this is preserved through the transform
            dropna: whether null keys should be dropped
        """
        table = self._to_ibis_expr()
        bindings = {col: table[col] for col in self.column_ids}
        stats = {
            col_out: agg_compiler.compile_aggregate(aggregate, bindings)
            for aggregate, col_out in aggregations
        }
        if by_column_ids:
            result = table.group_by(by_column_ids).aggregate(**stats)
            # Must have deterministic ordering, so order by the unique "by" column
            ordering = ExpressionOrdering(
                tuple([ascending_over(column_id) for column_id in by_column_ids]),
                total_ordering_columns=frozenset(by_column_ids),
            )
            columns = tuple(result[key] for key in result.columns)
            expr = OrderedIR(result, columns=columns, ordering=ordering)
            if dropna:
                for column_id in by_column_ids:
                    expr = expr._filter(expr._get_ibis_column(column_id).notnull())
            # Can maybe remove this as Ordering id is redundant as by_column is unique after aggregation
            return expr._project_offsets()
        else:
            aggregates = {**stats, ORDER_ID_COLUMN: ibis_types.literal(0)}
            result = table.aggregate(**aggregates)
            # Ordering is irrelevant for single-row output, but set ordering id regardless as other ops(join etc.) expect it.
            # TODO: Maybe can make completely empty
            ordering = ExpressionOrdering(
                ordering_value_columns=tuple([]),
                total_ordering_columns=frozenset([]),
            )
            return OrderedIR(
                result,
                columns=[result[col_id] for col_id in [*stats.keys()]],
                hidden_ordering_columns=[result[ORDER_ID_COLUMN]],
                ordering=ordering,
            )

    def _uniform_sampling(self, fraction: float) -> UnorderedIR:
        """Sampling the table on given fraction.

        .. warning::
            The row numbers of result is non-deterministic, avoid to use.
        """
        table = self._to_ibis_expr(fraction=fraction)
        columns = [table[column_name] for column_name in self._column_names]
        return UnorderedIR(
            table,
            columns=columns,
        )

    def explode(self, column_ids: typing.Sequence[str]) -> UnorderedIR:
        table = self._to_ibis_expr()

        # The offset array ensures null represents empty arrays after unnesting.
        offset_array_id = bigframes.core.guid.generate_guid("offset_array_")
        offset_array = (
            vendored_ibis_ops.GenerateArray(
                ibis.greatest(
                    0,
                    ibis.least(
                        *[table[column_id].length() - 1 for column_id in column_ids]
                    ),
                )
            )
            .to_expr()
            .name(offset_array_id),
        )
        table_w_offset_array = table.select(
            offset_array,
            *self._column_names,
        )

        unnest_offset_id = bigframes.core.guid.generate_guid("unnest_offset_")
        unnest_offset = (
            table_w_offset_array[offset_array_id].unnest().name(unnest_offset_id)
        )
        table_w_offset = table_w_offset_array.select(
            unnest_offset,
            *self._column_names,
        )

        unnested_columns = [
            table_w_offset[column_id][table_w_offset[unnest_offset_id]].name(column_id)
            if column_id in column_ids
            else table_w_offset[column_id]
            for column_id in self._column_names
        ]
        table_w_unnest = table_w_offset.select(*unnested_columns)

        columns = [table_w_unnest[column_name] for column_name in self._column_names]
        return UnorderedIR(
            table_w_unnest,
            columns=columns,
        )

    ## Helpers
    def _set_or_replace_by_id(
        self, id: str, new_value: ibis_types.Value
    ) -> UnorderedIR:
        builder = self.builder()
        if id in self.column_ids:
            builder.columns = [
                val if (col_id != id) else new_value.name(id)
                for col_id, val in zip(self.column_ids, self._columns)
            ]
        else:
            builder.columns = [*self.columns, new_value.name(id)]
        return builder.build()

    def _select(self, values: typing.Tuple[ibis_types.Value]) -> UnorderedIR:
        builder = self.builder()
        builder.columns = values
        return builder.build()

    def _reproject_to_table(self) -> UnorderedIR:
        """
        Internal operators that projects the internal representation into a
        new ibis table expression where each value column is a direct
        reference to a column in that table expression. Needed after
        some operations such as window operations that cannot be used
        recursively in projections.
        """
        table = self._to_ibis_expr()
        columns = [table[column_name] for column_name in self._column_names]
        return UnorderedIR(
            table,
            columns=columns,
        )

    class Builder:
        def __init__(
            self,
            table: ibis_types.Table,
            columns: Collection[ibis_types.Value] = (),
            predicates: Optional[Collection[ibis_types.BooleanValue]] = None,
        ):
            self.table = table
            self.columns = list(columns)
            self.predicates = list(predicates) if predicates is not None else None

        def build(self) -> UnorderedIR:
            return UnorderedIR(
                table=self.table,
                columns=self.columns,
                predicates=self.predicates,
            )


class OrderedIR(BaseIbisIR):
    """Immutable BigQuery DataFrames expression tree.

    Note: Usage of this class is considered to be private and subject to change
    at any time.

    This class is a wrapper around Ibis expressions. Its purpose is to defer
    Ibis projection operations to keep generated SQL small and correct when
    mixing and matching columns from different versions of a DataFrame.

    Args:
        table: An Ibis table expression.
        columns: Ibis value expressions that can be projected as columns.
        hidden_ordering_columns: Ibis value expressions to store ordering.
        ordering: An ordering property of the data frame.
        predicates: A list of filters on the data frame.
    """

    def __init__(
        self,
        table: ibis_types.Table,
        columns: Sequence[ibis_types.Value],
        hidden_ordering_columns: Optional[Sequence[ibis_types.Value]] = None,
        ordering: ExpressionOrdering = ExpressionOrdering(),
        predicates: Optional[Collection[ibis_types.BooleanValue]] = None,
    ):
        super().__init__(table, columns, predicates)
        self._ordering = ordering
        # Meta columns store ordering, or other data that doesn't correspond to dataframe columns
        self._hidden_ordering_columns = (
            tuple(hidden_ordering_columns)
            if hidden_ordering_columns is not None
            else ()
        )

        # To allow for more efficient lookup by column name, create a
        # dictionary mapping names to column values.
        self._column_names = {
            (
                column.resolve(table)
                # TODO(https://github.com/ibis-project/ibis/issues/7613): use
                # public API to refer to Deferred type.
                if isinstance(column, ibis.common.deferred.Deferred)
                else column
            ).get_name(): column
            for column in self._columns
        }
        self._hidden_ordering_column_names = {
            column.get_name(): column for column in self._hidden_ordering_columns
        }
        ### Validation
        value_col_ids = self._column_names.keys()
        hidden_col_ids = self._hidden_ordering_column_names.keys()

        all_columns = value_col_ids | hidden_col_ids
        ordering_valid = all(
            set(col.scalar_expression.unbound_variables).issubset(all_columns)
            for col in ordering.all_ordering_columns
        )
        if value_col_ids & hidden_col_ids:
            raise ValueError(
                f"Keys in both hidden and exposed list: {value_col_ids & hidden_col_ids}"
            )
        if not ordering_valid:
            raise ValueError(f"Illegal ordering keys: {ordering.all_ordering_columns}")

    @classmethod
    def from_pandas(
        cls,
        pd_df: pandas.DataFrame,
        schema: schemata.ArraySchema,
    ) -> OrderedIR:
        """
        Builds an in-memory only (SQL only) expr from a pandas dataframe.

        Assumed that the dataframe has unique string column names and bigframes-suppported dtypes.
        """

        # ibis memtable cannot handle NA, must convert to None
        # this destroys the schema however
        ibis_values = pd_df.astype("object").where(pandas.notnull(pd_df), None)  # type: ignore
        ibis_values = ibis_values.assign(**{ORDER_ID_COLUMN: range(len(pd_df))})
        # derive the ibis schema from the original pandas schema
        ibis_schema = [
            (name, bigframes.dtypes.bigframes_dtype_to_ibis_dtype(dtype))
            for name, dtype in zip(schema.names, schema.dtypes)
        ]
        ibis_schema.append((ORDER_ID_COLUMN, ibis_dtypes.int64))

        keys_memtable = ibis.memtable(ibis_values, schema=ibis.schema(ibis_schema))

        return cls(
            keys_memtable,
            columns=[keys_memtable[column].name(column) for column in pd_df.columns],
            ordering=ExpressionOrdering(
                ordering_value_columns=tuple([ascending_over(ORDER_ID_COLUMN)]),
                total_ordering_columns=frozenset([ORDER_ID_COLUMN]),
            ),
            hidden_ordering_columns=(keys_memtable[ORDER_ID_COLUMN],),
        )

    @property
    def _ibis_bindings(self) -> dict[str, ibis_types.Value]:
        all_keys = itertools.chain(self.column_ids, self._hidden_column_ids)
        return {col: self._get_any_column(col) for col in all_keys}

    @property
    def _hidden_column_ids(self) -> typing.Sequence[str]:
        return tuple(self._hidden_ordering_column_names.keys())

    @property
    def _ibis_order(self) -> Sequence[ibis_types.Value]:
        """Returns a sequence of ibis values which can be directly used to order a table expression. Has direction modifiers applied."""
        return _convert_ordering_to_table_values(
            {**self._column_names, **self._hidden_ordering_column_names},
            self._ordering.all_ordering_columns,
        )

    def to_unordered(self) -> UnorderedIR:
        return UnorderedIR(self._table, self._columns, self._predicates)

    def builder(self) -> OrderedIR.Builder:
        """Creates a mutable builder for expressions."""
        # Since ArrayValue is intended to be immutable (immutability offers
        # potential opportunities for caching, though we might need to introduce
        # more node types for that to be useful), we create a builder class.
        return OrderedIR.Builder(
            self._table,
            columns=self._columns,
            hidden_ordering_columns=self._hidden_ordering_columns,
            ordering=self._ordering,
            predicates=self._predicates,
        )

    def order_by(self, by: Sequence[OrderingExpression]) -> OrderedIR:
        expr_builder = self.builder()
        expr_builder.ordering = self._ordering.with_ordering_columns(by)
        return expr_builder.build()

    def reversed(self) -> OrderedIR:
        expr_builder = self.builder()
        expr_builder.ordering = self._ordering.with_reverse()
        return expr_builder.build()

    def _uniform_sampling(self, fraction: float) -> OrderedIR:
        """Sampling the table on given fraction.

        .. warning::
            The row numbers of result is non-deterministic, avoid to use.
        """
        table = self._to_ibis_expr(
            ordering_mode="unordered", expose_hidden_cols=True, fraction=fraction
        )
        columns = [table[column_name] for column_name in self._column_names]
        hidden_ordering_columns = [
            table[column_name] for column_name in self._hidden_ordering_column_names
        ]
        return OrderedIR(
            table,
            columns=columns,
            hidden_ordering_columns=hidden_ordering_columns,
            ordering=self._ordering,
        )

    def explode(self, column_ids: typing.Sequence[str]) -> OrderedIR:
        table = self._to_ibis_expr(ordering_mode="unordered", expose_hidden_cols=True)

        offset_array_id = bigframes.core.guid.generate_guid("offset_array_")
        offset_array = (
            vendored_ibis_ops.GenerateArray(
                ibis.greatest(
                    0,
                    ibis.least(
                        *[table[column_id].length() - 1 for column_id in column_ids]
                    ),
                )
            )
            .to_expr()
            .name(offset_array_id),
        )
        table_w_offset_array = table.select(
            offset_array,
            *self._column_names,
            *self._hidden_ordering_column_names,
        )

        unnest_offset_id = bigframes.core.guid.generate_guid("unnest_offset_")
        unnest_offset = (
            table_w_offset_array[offset_array_id].unnest().name(unnest_offset_id)
        )
        table_w_offset = table_w_offset_array.select(
            unnest_offset,
            *self._column_names,
            *self._hidden_ordering_column_names,
        )

        unnested_columns = [
            table_w_offset[column_id][table_w_offset[unnest_offset_id]].name(column_id)
            if column_id in column_ids
            else table_w_offset[column_id]
            for column_id in self._column_names
        ]

        table_w_unnest = table_w_offset.select(
            table_w_offset[unnest_offset_id],
            *unnested_columns,
            *self._hidden_ordering_column_names,
        )

        columns = [table_w_unnest[column_name] for column_name in self._column_names]
        hidden_ordering_columns = [
            *[
                table_w_unnest[column_name]
                for column_name in self._hidden_ordering_column_names
            ],
            table_w_unnest[unnest_offset_id],
        ]
        ordering = ExpressionOrdering(
            ordering_value_columns=tuple(
                [
                    *self._ordering.ordering_value_columns,
                    ascending_over(unnest_offset_id),
                ]
            ),
            total_ordering_columns=frozenset(
                [*self._ordering.total_ordering_columns, unnest_offset_id]
            ),
        )

        return OrderedIR(
            table_w_unnest,
            columns=columns,
            hidden_ordering_columns=hidden_ordering_columns,
            ordering=ordering,
        )

    def promote_offsets(self, col_id: str) -> OrderedIR:
        """
        Convenience function to promote copy of column offsets to a value column. Can be used to reset index.
        """
        # Special case: offsets already exist
        ordering = self._ordering

        if (not ordering.is_sequential) or (not ordering.total_order_col):
            return self._project_offsets().promote_offsets(col_id)
        expr_builder = self.builder()
        expr_builder.columns = [
            self._compile_expression(ordering.total_order_col.scalar_expression).name(
                col_id
            ),
            *self.columns,
        ]
        return expr_builder.build()

    ## Methods that only work with ordering
    def project_window_op(
        self,
        column_name: str,
        op: agg_ops.UnaryWindowOp,
        window_spec: WindowSpec,
        output_name=None,
        *,
        never_skip_nulls=False,
        skip_reproject_unsafe: bool = False,
    ) -> OrderedIR:
        """
        Creates a new expression based on this expression with unary operation applied to one column.
        column_name: the id of the input column present in the expression
        op: the windowable operator to apply to the input column
        window_spec: a specification of the window over which to apply the operator
        output_name: the id to assign to the output of the operator, by default will replace input col if distinct output id not provided
        never_skip_nulls: will disable null skipping for operators that would otherwise do so
        skip_reproject_unsafe: skips the reprojection step, can be used when performing many non-dependent window operations, user responsible for not nesting window expressions, or using outputs as join, filter or aggregation keys before a reprojection
        """
        column = typing.cast(ibis_types.Column, self._get_ibis_column(column_name))
        window = self._ibis_window_from_spec(window_spec, allow_ties=op.handles_ties)
        bindings = {col: self._get_ibis_column(col) for col in self.column_ids}

        window_op = agg_compiler.compile_analytic(
            ex.UnaryAggregation(op, ex.free_var(column_name)), window, bindings=bindings
        )

        clauses = []
        if op.skips_nulls and not never_skip_nulls:
            clauses.append((column.isnull(), ibis.NA))
        if window_spec.min_periods:
            if op.skips_nulls:
                # Most operations do not count NULL values towards min_periods
                observation_count = agg_compiler.compile_analytic(
                    ex.UnaryAggregation(agg_ops.count_op, ex.free_var(column_name)),
                    window,
                    bindings=bindings,
                )
            else:
                # Operations like count treat even NULLs as valid observations for the sake of min_periods
                # notnull is just used to convert null values to non-null (FALSE) values to be counted
                denulled_value = typing.cast(ibis_types.BooleanColumn, column.notnull())
                observation_count = agg_compiler.compile_analytic(
                    ex.UnaryAggregation(agg_ops.count_op, ex.free_var("_denulled")),
                    window,
                    bindings={**bindings, "_denulled": denulled_value},
                )
            clauses.append(
                (
                    observation_count < ibis_types.literal(window_spec.min_periods),
                    ibis.NA,
                )
            )
        if clauses:
            case_statement = ibis.case()
            for clause in clauses:
                case_statement = case_statement.when(clause[0], clause[1])
            case_statement = case_statement.else_(window_op).end()  # type: ignore
            window_op = case_statement

        result = self._set_or_replace_by_id(output_name or column_name, window_op)
        # TODO(tbergeron): Automatically track analytic expression usage and defer reprojection until required for valid query generation.
        return result._reproject_to_table() if not skip_reproject_unsafe else result

    def _reproject_to_table(self) -> OrderedIR:
        table = self._to_ibis_expr(
            ordering_mode="unordered",
            expose_hidden_cols=True,
        )
        columns = [table[column_name] for column_name in self._column_names]
        ordering_col_ids = list(
            itertools.chain.from_iterable(
                ref.scalar_expression.unbound_variables
                for ref in self._ordering.all_ordering_columns
            )
        )
        hidden_ordering_columns = [
            table[column_name]
            for column_name in self._hidden_ordering_column_names
            if column_name in ordering_col_ids
        ]
        return OrderedIR(
            table,
            columns=columns,
            hidden_ordering_columns=hidden_ordering_columns,
            ordering=self._ordering,
        )

    def to_sql(
        self,
        col_id_overrides: typing.Mapping[str, str] = {},
        sorted: bool = False,
    ) -> str:
        if sorted:
            # Need to bake ordering expressions into the selected column in order for our ordering clause builder to work.
            baked_ir = self._bake_ordering()
            sql = ibis_bigquery.Backend().compile(
                baked_ir._to_ibis_expr(
                    ordering_mode="unordered",
                    col_id_overrides=col_id_overrides,
                    expose_hidden_cols=True,
                )
            )
            output_columns = [
                col_id_overrides.get(col) if (col in col_id_overrides) else col
                for col in baked_ir.column_ids
            ]
            selection = ", ".join(map(lambda col_id: f"`{col_id}`", output_columns))
            order_by_clause = baked_ir._ordering_clause(
                baked_ir._ordering.all_ordering_columns
            )

            sql = textwrap.dedent(
                f"SELECT {selection}\n"
                "FROM (\n"
                f"{sql}\n"
                ")\n"
                f"{order_by_clause}\n"
            )
        else:
            sql = ibis_bigquery.Backend().compile(
                self._to_ibis_expr(
                    ordering_mode="unordered",
                    col_id_overrides=col_id_overrides,
                    expose_hidden_cols=False,
                )
            )
        return typing.cast(str, sql)

    def _ordering_clause(self, ordering: Iterable[OrderingExpression]) -> str:
        parts = []
        for col_ref in ordering:
            asc_desc = "ASC" if col_ref.direction.is_ascending else "DESC"
            null_clause = "NULLS LAST" if col_ref.na_last else "NULLS FIRST"
            ordering_expr = col_ref.scalar_expression
            # We don't know how to compile scalar expressions in isolation
            if ordering_expr.is_const:
                # Probably shouldn't have constants in ordering definition, but best to ignore if somehow they end up here.
                continue
            if not isinstance(ordering_expr, ex.UnboundVariableExpression):
                raise ValueError("Expected direct column reference.")
            part = f"`{ordering_expr.id}` {asc_desc} {null_clause}"
            parts.append(part)
        return f"ORDER BY {' ,'.join(parts)}"

    def _to_ibis_expr(
        self,
        *,
        expose_hidden_cols: bool = False,
        fraction: Optional[float] = None,
        col_id_overrides: typing.Mapping[str, str] = {},
        ordering_mode: Literal["string_encoded", "offset_col", "unordered"],
        order_col_name: Optional[str] = ORDER_ID_COLUMN,
    ):
        """
        Creates an Ibis table expression representing the DataFrame.

        ArrayValue objects are sorted, so the following options are available
        to reflect this in the ibis expression.

        * "offset_col": Zero-based offsets are generated as a column, this will
          not sort the rows however.
        * "string_encoded": An ordered string column is provided in output table.
        * "unordered": No ordering information will be provided in output. Only
          value columns are projected.

        For offset or ordered column, order_col_name can be used to assign the
        output label for the ordering column. If none is specified, the default
        column name will be 'bigframes_ordering_id'

        Args:
            expose_hidden_cols:
                If True, include the hidden ordering columns in the results.
                Only compatible with `order_by` and `unordered`
                ``ordering_mode``.
            ordering_mode:
                How to construct the Ibis expression from the ArrayValue. See
                above for details.
            order_col_name:
                If the ordering mode outputs a single ordering or offsets
                column, use this as the column name.
            col_id_overrides:
                overrides the column ids for the result
        Returns:
            An ibis expression representing the data help by the ArrayValue object.
        """
        assert ordering_mode in (
            "string_encoded",
            "offset_col",
            "unordered",
        )
        if expose_hidden_cols and ordering_mode in ("ordered_col", "offset_col"):
            raise ValueError(
                f"Cannot expose hidden ordering columns with ordering_mode {ordering_mode}"
            )

        columns = list(self._columns)
        columns_to_drop: list[
            str
        ] = []  # Ordering/Filtering columns that will be dropped at end

        if self._reduced_predicate is not None:
            columns.append(self._reduced_predicate)
            # Usually drop predicate as it is will be all TRUE after filtering
            if not expose_hidden_cols:
                columns_to_drop.append(self._reduced_predicate.get_name())

        order_columns = self._create_order_columns(
            ordering_mode, order_col_name, expose_hidden_cols
        )
        columns.extend(order_columns)

        # Special case for empty tables, since we can't create an empty
        # projection.
        if not columns:
            return ibis.memtable([])

        # Make sure we don't have any unbound (deferred) columns.
        table = self._table.select(columns)

        # Make sure all dtypes are the "canonical" ones for BigFrames. This is
        # important for operations like UNION where the schema must match.
        table = table.select(
            bigframes.dtypes.ibis_value_to_canonical_type(table[column])
            for column in table.columns
        )
        base_table = table
        if self._reduced_predicate is not None:
            table = table.filter(base_table[PREDICATE_COLUMN])
        table = table.drop(*columns_to_drop)
        if col_id_overrides:
            table = table.rename(
                {value: key for key, value in col_id_overrides.items()}
            )
        if fraction is not None:
            table = table.filter(ibis.random() < ibis.literal(fraction))
        return table

    def filter(self, predicate: ex.Expression) -> OrderedIR:
        bindings = {col: self._get_ibis_column(col) for col in self.column_ids}
        condition = op_compiler.compile_expression(predicate, bindings)
        return self._filter(condition)

    def _filter(self, predicate_value: ibis_types.BooleanValue) -> OrderedIR:
        """Filter the table on a given expression, the predicate must be a boolean series aligned with the table expression."""
        expr = self.builder()
        expr.ordering = expr.ordering.with_non_sequential()
        expr.predicates = [*self._predicates, predicate_value]
        return expr.build()

    def _set_or_replace_by_id(self, id: str, new_value: ibis_types.Value) -> OrderedIR:
        """Safely assign by id while maintaining ordering integrity."""
        # TODO: Split into explicit set and replace methods
        ordering_col_ids = set(
            itertools.chain.from_iterable(
                col_ref.scalar_expression.unbound_variables
                for col_ref in self._ordering.ordering_value_columns
            )
        )
        if id in ordering_col_ids:
            return self._hide_column(id)._set_or_replace_by_id(id, new_value)

        builder = self.builder()
        if id in self.column_ids:
            builder.columns = [
                val if (col_id != id) else new_value.name(id)
                for col_id, val in zip(self.column_ids, self._columns)
            ]
        else:
            builder.columns = [*self.columns, new_value.name(id)]
        return builder.build()

    def _select(self, values: typing.Tuple[ibis_types.Value]) -> OrderedIR:
        """Safely assign by id while maintaining ordering integrity."""
        # TODO: Split into explicit set and replace methods
        ordering_col_ids = set(
            itertools.chain.from_iterable(
                [
                    col_ref.scalar_expression.unbound_variables
                    for col_ref in self._ordering.ordering_value_columns
                ]
            )
        )
        ir = self
        mappings = {value.name: value for value in values}
        for ordering_id in ordering_col_ids:
            # Drop case
            if (ordering_id not in mappings) and (ordering_id in ir.column_ids):
                # id is being dropped, hide it first
                ir = ir._hide_column(ordering_id)
            # Mutate case
            elif (ordering_id in mappings) and not mappings[ordering_id].equals(
                ir._get_any_column(ordering_id)
            ):
                ir = ir._hide_column(ordering_id)

        builder = ir.builder()
        builder.columns = list(values)
        return builder.build()

    ## Ordering specific helpers
    def _get_any_column(self, key: str) -> ibis_types.Value:
        """Gets the Ibis expression for a given column. Will also get hidden columns."""
        all_columns = {**self._column_names, **self._hidden_ordering_column_names}
        if key not in all_columns.keys():
            raise ValueError(
                "Column name {} not in set of values: {}".format(
                    key, all_columns.keys()
                )
            )
        return typing.cast(ibis_types.Value, all_columns[key])

    def _get_hidden_ordering_column(self, key: str) -> ibis_types.Column:
        """Gets the Ibis expression for a given hidden column."""
        if key not in self._hidden_ordering_column_names.keys():
            raise ValueError(
                "Column name {} not in set of values: {}".format(
                    key, self._hidden_ordering_column_names.keys()
                )
            )
        return typing.cast(ibis_types.Column, self._hidden_ordering_column_names[key])

    def _hide_column(self, column_id) -> OrderedIR:
        """Pushes columns to hidden columns list. Used to hide ordering columns that have been dropped or destructively mutated."""
        expr_builder = self.builder()
        # Need to rename column as caller might be creating a new row with the same name but different values.
        # Can avoid this if don't allow callers to determine ids and instead generate unique ones in this class.
        new_name = bigframes.core.guid.generate_guid(prefix="bigframes_hidden_")
        expr_builder.hidden_ordering_columns = [
            *self._hidden_ordering_columns,
            self._get_ibis_column(column_id).name(new_name),
        ]
        expr_builder.ordering = self._ordering.with_column_remap({column_id: new_name})
        return expr_builder.build()

    def _bake_ordering(self) -> OrderedIR:
        """Bakes ordering expression into the selection, maybe creating hidden columns."""
        ordering_expressions = self._ordering.all_ordering_columns
        new_exprs = []
        new_baked_cols = []
        for expr in ordering_expressions:
            if isinstance(expr.scalar_expression, ex.OpExpression):
                baked_column = self._compile_expression(expr.scalar_expression).name(
                    bigframes.core.guid.generate_guid()
                )
                new_baked_cols.append(baked_column)
                new_expr = OrderingExpression(
                    ex.free_var(baked_column.name), expr.direction, expr.na_last
                )
                new_exprs.append(new_expr)
            else:
                new_exprs.append(expr)

        ordering = self._ordering.with_ordering_columns(new_exprs)
        return OrderedIR(
            self._table,
            columns=self.columns,
            hidden_ordering_columns=[*self._hidden_ordering_columns, *new_baked_cols],
            ordering=ordering,
            predicates=self._predicates,
        )

    def _project_offsets(self) -> OrderedIR:
        """Create a new expression that contains offsets. Should only be executed when offsets are needed for an operations. Has no effect on expression semantics."""
        if self._ordering.is_sequential:
            return self
        table = self._to_ibis_expr(
            ordering_mode="offset_col", order_col_name=ORDER_ID_COLUMN
        )
        columns = [table[column_name] for column_name in self._column_names]
        ordering = ExpressionOrdering(
            ordering_value_columns=tuple([ascending_over(ORDER_ID_COLUMN)]),
            total_ordering_columns=frozenset([ORDER_ID_COLUMN]),
            integer_encoding=IntegerEncoding(True, is_sequential=True),
        )
        return OrderedIR(
            table,
            columns=columns,
            hidden_ordering_columns=[table[ORDER_ID_COLUMN]],
            ordering=ordering,
        )

    def _create_order_columns(
        self,
        ordering_mode: str,
        order_col_name: Optional[str],
        expose_hidden_cols: bool,
    ) -> typing.Sequence[ibis_types.Value]:
        # Generate offsets if current ordering id semantics are not sufficiently strict
        if ordering_mode == "offset_col":
            return (self._create_offset_column().name(order_col_name),)
        elif ordering_mode == "string_encoded":
            return (self._create_string_ordering_column().name(order_col_name),)
        elif expose_hidden_cols:
            return self._hidden_ordering_columns
        return ()

    def _create_offset_column(self) -> ibis_types.IntegerColumn:
        if self._ordering.total_order_col and self._ordering.is_sequential:
            offsets = self._compile_expression(
                self._ordering.total_order_col.scalar_expression
            )
            return typing.cast(ibis_types.IntegerColumn, offsets)
        else:
            window = ibis.window(order_by=self._ibis_order)
            if self._predicates:
                window = window.group_by(self._reduced_predicate)
            offsets = ibis.row_number().over(window)
            return typing.cast(ibis_types.IntegerColumn, offsets)

    def _create_string_ordering_column(self) -> ibis_types.StringColumn:
        if self._ordering.total_order_col and self._ordering.is_string_encoded:
            string_order_ids = op_compiler.compile_expression(
                self._ordering.total_order_col.scalar_expression, self._ibis_bindings
            )
            return typing.cast(ibis_types.StringColumn, string_order_ids)
        if (
            self._ordering.total_order_col
            and self._ordering.integer_encoding.is_encoded
        ):
            # Special case: non-negative integer ordering id can be converted directly to string without regenerating row numbers
            int_values = self._compile_expression(
                self._ordering.total_order_col.scalar_expression
            )
            return encode_order_string(
                typing.cast(ibis_types.IntegerColumn, int_values),
            )
        else:
            # Have to build string from scratch
            window = ibis.window(order_by=self._ibis_order)
            if self._predicates:
                window = window.group_by(self._reduced_predicate)
            row_nums = typing.cast(
                ibis_types.IntegerColumn, ibis.row_number().over(window)
            )
            return encode_order_string(row_nums)

    def _compile_expression(self, expr: ex.Expression):
        return op_compiler.compile_expression(expr, self._ibis_bindings)

    def _ibis_window_from_spec(self, window_spec: WindowSpec, allow_ties: bool = False):
        group_by: typing.List[ibis_types.Value] = (
            [
                typing.cast(
                    ibis_types.Column, _as_identity(self._get_ibis_column(column))
                )
                for column in window_spec.grouping_keys
            ]
            if window_spec.grouping_keys
            else []
        )
        if self._reduced_predicate is not None:
            group_by.append(self._reduced_predicate)
        if window_spec.ordering:
            order_by = _convert_ordering_to_table_values(
                {**self._column_names, **self._hidden_ordering_column_names},
                window_spec.ordering,
            )
            if not allow_ties:
                # Most operator need an unambiguous ordering, so the table's total ordering is appended
                order_by = tuple([*order_by, *self._ibis_order])
        elif (window_spec.following is not None) or (window_spec.preceding is not None):
            # If window spec has following or preceding bounds, we need to apply an unambiguous ordering.
            order_by = tuple(self._ibis_order)
        else:
            # Unbound grouping window. Suitable for aggregations but not for analytic function application.
            order_by = None
        return ibis.window(
            preceding=window_spec.preceding,
            following=window_spec.following,
            order_by=order_by,
            group_by=group_by,
        )

    class Builder:
        def __init__(
            self,
            table: ibis_types.Table,
            ordering: ExpressionOrdering,
            columns: Collection[ibis_types.Value] = (),
            hidden_ordering_columns: Collection[ibis_types.Value] = (),
            predicates: Optional[Collection[ibis_types.BooleanValue]] = None,
        ):
            self.table = table
            self.columns = list(columns)
            self.hidden_ordering_columns = list(hidden_ordering_columns)
            self.ordering = ordering
            self.predicates = list(predicates) if predicates is not None else None

        def build(self) -> OrderedIR:
            return OrderedIR(
                table=self.table,
                columns=self.columns,
                hidden_ordering_columns=self.hidden_ordering_columns,
                ordering=self.ordering,
                predicates=self.predicates,
            )


def _reduce_predicate_list(
    predicate_list: typing.Collection[ibis_types.BooleanValue],
) -> ibis_types.BooleanValue:
    """Converts a list of predicates BooleanValues into a single BooleanValue."""
    if len(predicate_list) == 0:
        raise ValueError("Cannot reduce empty list of predicates")
    if len(predicate_list) == 1:
        (item,) = predicate_list
        return item
    return functools.reduce(lambda acc, pred: acc.__and__(pred), predicate_list)


def _convert_ordering_to_table_values(
    value_lookup: typing.Mapping[str, ibis_types.Value],
    ordering_columns: typing.Sequence[OrderingExpression],
) -> typing.Sequence[ibis_types.Value]:
    column_refs = ordering_columns
    ordering_values = []
    for ordering_col in column_refs:
        expr = op_compiler.compile_expression(
            ordering_col.scalar_expression, value_lookup
        )
        ordering_value = (
            ibis.asc(expr) if ordering_col.direction.is_ascending else ibis.desc(expr)
        )
        # Bigquery SQL considers NULLS to be "smallest" values, but we need to override in these cases.
        if (not ordering_col.na_last) and (not ordering_col.direction.is_ascending):
            # Force nulls to be first
            is_null_val = typing.cast(ibis_types.Column, expr.isnull())
            ordering_values.append(ibis.desc(is_null_val))
        elif (ordering_col.na_last) and (ordering_col.direction.is_ascending):
            # Force nulls to be last
            is_null_val = typing.cast(ibis_types.Column, expr.isnull())
            ordering_values.append(ibis.asc(is_null_val))
        ordering_values.append(ordering_value)
    return ordering_values


def _as_identity(value: ibis_types.Value):
    # Some types need to be converted to string to enable groupby
    if value.type().is_float64() or value.type().is_geospatial():
        return value.cast(ibis_dtypes.str)
    return value
