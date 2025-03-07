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

import pytest

from bigframes.ml import llm


def test_create_text_generator_model(
    palm2_text_generator_model, dataset_id, bq_connection
):
    # Model creation doesn't return error
    assert palm2_text_generator_model is not None
    assert palm2_text_generator_model._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = palm2_text_generator_model.to_gbq(
        f"{dataset_id}.temp_text_model", replace=True
    )
    assert f"{dataset_id}.temp_text_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.model_name == "text-bison"
    assert reloaded_model.connection_name == bq_connection


def test_create_text_generator_32k_model(
    palm2_text_generator_32k_model, dataset_id, bq_connection
):
    # Model creation doesn't return error
    assert palm2_text_generator_32k_model is not None
    assert palm2_text_generator_32k_model._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = palm2_text_generator_32k_model.to_gbq(
        f"{dataset_id}.temp_text_model", replace=True
    )
    assert f"{dataset_id}.temp_text_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.model_name == "text-bison-32k"
    assert reloaded_model.connection_name == bq_connection


@pytest.mark.flaky(retries=2)
def test_create_text_generator_model_default_session(
    bq_connection, llm_text_pandas_df, bigquery_client
):
    import bigframes.pandas as bpd

    bpd.close_session()
    bpd.options.bigquery.bq_connection = bq_connection
    bpd.options.bigquery.location = "us"

    model = llm.PaLM2TextGenerator()
    assert model is not None
    assert model._bqml_model is not None
    assert (
        model.connection_name.casefold()
        == f"{bigquery_client.project}.us.bigframes-rf-conn"
    )

    llm_text_df = bpd.read_pandas(llm_text_pandas_df)

    df = model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_create_text_generator_32k_model_default_session(
    bq_connection, llm_text_pandas_df, bigquery_client
):
    import bigframes.pandas as bpd

    bpd.close_session()
    bpd.options.bigquery.bq_connection = bq_connection
    bpd.options.bigquery.location = "us"

    model = llm.PaLM2TextGenerator(model_name="text-bison-32k")
    assert model is not None
    assert model._bqml_model is not None
    assert (
        model.connection_name.casefold()
        == f"{bigquery_client.project}.us.bigframes-rf-conn"
    )

    llm_text_df = bpd.read_pandas(llm_text_pandas_df)

    df = model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_create_text_generator_model_default_connection(
    llm_text_pandas_df, bigquery_client
):
    from bigframes import _config
    import bigframes.pandas as bpd

    bpd.close_session()
    _config.options = _config.Options()  # reset configs

    llm_text_df = bpd.read_pandas(llm_text_pandas_df)

    model = llm.PaLM2TextGenerator()
    assert model is not None
    assert model._bqml_model is not None
    assert (
        model.connection_name.casefold()
        == f"{bigquery_client.project}.us.bigframes-default-connection"
    )

    df = model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


# Marked as flaky only because BQML LLM is in preview, the service only has limited capacity, not stable enough.
@pytest.mark.flaky(retries=2)
def test_text_generator_predict_default_params_success(
    palm2_text_generator_model, llm_text_df
):
    df = palm2_text_generator_model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_text_generator_predict_series_default_params_success(
    palm2_text_generator_model, llm_text_df
):
    df = palm2_text_generator_model.predict(llm_text_df["prompt"]).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_text_generator_predict_arbitrary_col_label_success(
    palm2_text_generator_model, llm_text_df
):
    llm_text_df = llm_text_df.rename(columns={"prompt": "arbitrary"})
    df = palm2_text_generator_model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_text_generator_predict_with_params_success(
    palm2_text_generator_model, llm_text_df
):
    df = palm2_text_generator_model.predict(
        llm_text_df, temperature=0.5, max_output_tokens=100, top_k=20, top_p=0.5
    ).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


def test_create_embedding_generator_model(
    palm2_embedding_generator_model, dataset_id, bq_connection
):
    # Model creation doesn't return error
    assert palm2_embedding_generator_model is not None
    assert palm2_embedding_generator_model._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = palm2_embedding_generator_model.to_gbq(
        f"{dataset_id}.temp_embedding_model", replace=True
    )
    assert f"{dataset_id}.temp_embedding_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.model_name == "textembedding-gecko"
    assert reloaded_model.connection_name == bq_connection


def test_create_embedding_generator_model_002(
    palm2_embedding_generator_model_002, dataset_id, bq_connection
):
    # Model creation doesn't return error
    assert palm2_embedding_generator_model_002 is not None
    assert palm2_embedding_generator_model_002._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = palm2_embedding_generator_model_002.to_gbq(
        f"{dataset_id}.temp_embedding_model", replace=True
    )
    assert f"{dataset_id}.temp_embedding_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.model_name == "textembedding-gecko"
    assert reloaded_model.version == "002"
    assert reloaded_model.connection_name == bq_connection


def test_create_embedding_generator_multilingual_model(
    palm2_embedding_generator_multilingual_model,
    dataset_id,
    bq_connection,
):
    # Model creation doesn't return error
    assert palm2_embedding_generator_multilingual_model is not None
    assert palm2_embedding_generator_multilingual_model._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = palm2_embedding_generator_multilingual_model.to_gbq(
        f"{dataset_id}.temp_embedding_model", replace=True
    )
    assert f"{dataset_id}.temp_embedding_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.model_name == "textembedding-gecko-multilingual"
    assert reloaded_model.connection_name == bq_connection


def test_create_text_embedding_generator_model_defaults(bq_connection):
    import bigframes.pandas as bpd

    bpd.close_session()
    bpd.options.bigquery.bq_connection = bq_connection
    bpd.options.bigquery.location = "us"

    model = llm.PaLM2TextEmbeddingGenerator()
    assert model is not None
    assert model._bqml_model is not None


def test_create_text_embedding_generator_multilingual_model_defaults(bq_connection):
    import bigframes.pandas as bpd

    bpd.close_session()
    bpd.options.bigquery.bq_connection = bq_connection
    bpd.options.bigquery.location = "us"

    model = llm.PaLM2TextEmbeddingGenerator(
        model_name="textembedding-gecko-multilingual"
    )
    assert model is not None
    assert model._bqml_model is not None


@pytest.mark.flaky(retries=2)
def test_embedding_generator_predict_success(
    palm2_embedding_generator_model, llm_text_df
):
    df = palm2_embedding_generator_model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "text_embedding" in df.columns
    series = df["text_embedding"]
    value = series[0]
    assert len(value) == 768


@pytest.mark.flaky(retries=2)
def test_embedding_generator_multilingual_predict_success(
    palm2_embedding_generator_multilingual_model, llm_text_df
):
    df = palm2_embedding_generator_multilingual_model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "text_embedding" in df.columns
    series = df["text_embedding"]
    value = series[0]
    assert len(value) == 768


@pytest.mark.flaky(retries=2)
def test_embedding_generator_predict_series_success(
    palm2_embedding_generator_model, llm_text_df
):
    df = palm2_embedding_generator_model.predict(llm_text_df["prompt"]).to_pandas()
    assert df.shape == (3, 4)
    assert "text_embedding" in df.columns
    series = df["text_embedding"]
    value = series[0]
    assert len(value) == 768


def test_create_gemini_text_generator_model(
    gemini_text_generator_model, dataset_id, bq_connection
):
    # Model creation doesn't return error
    assert gemini_text_generator_model is not None
    assert gemini_text_generator_model._bqml_model is not None

    # save, load to ensure configuration was kept
    reloaded_model = gemini_text_generator_model.to_gbq(
        f"{dataset_id}.temp_text_model", replace=True
    )
    assert f"{dataset_id}.temp_text_model" == reloaded_model._bqml_model.model_name
    assert reloaded_model.connection_name == bq_connection


@pytest.mark.flaky(retries=2)
def test_gemini_text_generator_predict_default_params_success(
    gemini_text_generator_model, llm_text_df
):
    df = gemini_text_generator_model.predict(llm_text_df).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)


@pytest.mark.flaky(retries=2)
def test_gemini_text_generator_predict_with_params_success(
    gemini_text_generator_model, llm_text_df
):
    df = gemini_text_generator_model.predict(
        llm_text_df, temperature=0.5, max_output_tokens=100, top_k=20, top_p=0.5
    ).to_pandas()
    assert df.shape == (3, 4)
    assert "ml_generate_text_llm_result" in df.columns
    series = df["ml_generate_text_llm_result"]
    assert all(series.str.len() > 20)
