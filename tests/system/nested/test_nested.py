# Copyright 2024 Google LLC
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

from google.cloud import bigquery
from bigframes.functions.nested import BQSchemaLayout, SchemaField
from bigframes.functions.NestedContextManager import NestedDataFrame
from google.cloud.bigquery_storage_v1 import types as gtypes
#import pytest
from typing import List
import bigframes.pandas as bfpd

from bigframes.dataframe import DataFrame
from bigframes.series import Series


# start context manager (cm) in pandas/__init__.py
# use dataframe object, there is dtypes info on it.
# cm constructur get schema by: dataframe._cashed [replaces block by cached version, one to one bq table to dataframe]
#   and get block with _block.expr

def table_schema(table_name_full: str) -> List[SchemaField]:
    project = table_name_full.split(".")[0]
    client = bigquery.Client(project=project, location="EU")
    query_job = client.get_table(table_name_full)
    return query_job.schema


def test_unroll_schema():  #table_name_full: pytest.CaptureFixture[str]
    schema = table_schema("vf-de-aib-prd-cmr-chn-lab.staging.scs_mini")
    bqs = BQSchemaLayout(schema)
    bqs.determine_layout() # TODO: add prefix get_ or determine_
    return bqs
    #assert isinstance(schema, List[SchemaField])

def test_nested_cm():
    bfpd.options.bigquery.project = "vf-de-aib-prd-cmr-chn-lab"
    bfpd.options.bigquery.location = "EU"


def fct_cm(cm: NestedDataFrame):
    cm._current_data = bfpd.read_gbq(f"SELECT * FROM {table}"),
    testdf.apply(cm._current_data),
    bfpd.get_dummies(cm._current_data)   


if __name__ == "__main__":
    #TODO: autodetect if bfpd si already setup and copy proj/loc if availabe
    # bfpd.options.bigquery.project = "vf-de-aib-prd-cmr-chn-lab"
    # bfpd.options.bigquery._location = "europe-west3"
    table = "vf-de-ca-lab.andreas_beschorner.nested_mini"  #"vf-de-aib-prd-cmr-chn-lab.staging.scs_mini"
    ncm =  NestedDataFrame(table, project="vf-de-ca-lab", location="europe-west3")
    testdf = DataFrame()
    testsq = Series()

    with ncm:
        ncm |= bfpd.get_dummies(ncm.data)
        #ncm |=  ncm.data, {"columns": []} | n_get_dummies
    pass


    # bqs = test_unroll_schema()
    # shdl = SchemaHandler(bqs, layer_separator=bsq.layer_separator)
    # cmd = CommandDAG(shdl)
    # cmd.dag_from_schema()
    