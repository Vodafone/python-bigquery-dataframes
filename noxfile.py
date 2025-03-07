# -*- coding: utf-8 -*-
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

from multiprocessing import Process
import os
import pathlib
from pathlib import Path
import re
import shutil
from typing import Dict, List
import warnings

import nox
import nox.sessions

BLACK_VERSION = "black==22.3.0"
ISORT_VERSION = "isort==5.12.0"

# pytest-retry is not yet compatible with pytest 8.x.
# https://github.com/str0zzapreti/pytest-retry/issues/32
PYTEST_VERSION = "pytest<8.0.0dev"
SPHINX_VERSION = "sphinx==4.5.0"
LINT_PATHS = ["docs", "bigframes", "tests", "third_party", "noxfile.py", "setup.py"]

DEFAULT_PYTHON_VERSION = "3.10"

UNIT_TEST_PYTHON_VERSIONS = ["3.9", "3.10", "3.11", "3.12"]
UNIT_TEST_STANDARD_DEPENDENCIES = [
    "mock",
    "asyncmock",
    PYTEST_VERSION,
    "pytest-cov",
    "pytest-asyncio",
    "pytest-mock",
]
UNIT_TEST_EXTERNAL_DEPENDENCIES: List[str] = []
UNIT_TEST_LOCAL_DEPENDENCIES: List[str] = []
UNIT_TEST_DEPENDENCIES: List[str] = []
UNIT_TEST_EXTRAS: List[str] = []
UNIT_TEST_EXTRAS_BY_PYTHON: Dict[str, List[str]] = {}

SYSTEM_TEST_PYTHON_VERSIONS = ["3.9", "3.12"]
SYSTEM_TEST_STANDARD_DEPENDENCIES = [
    "jinja2",
    "mock",
    "openpyxl",
    PYTEST_VERSION,
    "pytest-cov",
    "pytest-retry",
    "pytest-timeout",
    "pytest-xdist",
    "google-cloud-testutils",
    "tabulate",
    "xarray",
]
SYSTEM_TEST_EXTERNAL_DEPENDENCIES = [
    "google-cloud-bigquery",
]
SYSTEM_TEST_LOCAL_DEPENDENCIES: List[str] = []
SYSTEM_TEST_DEPENDENCIES: List[str] = []
SYSTEM_TEST_EXTRAS: List[str] = ["tests"]
SYSTEM_TEST_EXTRAS_BY_PYTHON: Dict[str, List[str]] = {}

CURRENT_DIRECTORY = pathlib.Path(__file__).parent.absolute()

# Sessions are executed in the order so putting the smaller sessions
# ahead to fail fast at presubmit running.
# 'docfx' is excluded since it only needs to run in 'docs-presubmit'
nox.options.sessions = [
    "lint",
    "lint_setup_py",
    "mypy",
    "format",
    "docs",
    "docfx",
    "unit",
    "unit_noextras",
    "system",
    "doctest",
    "cover",
]

# Error if a python version is missing
nox.options.error_on_missing_interpreters = True


@nox.session(python=DEFAULT_PYTHON_VERSION)
def lint(session):
    """Run linters.

    Returns a failure if the linters find linting errors or sufficiently
    serious code quality issues.
    """
    session.install("flake8", BLACK_VERSION)
    session.run(
        "black",
        "--check",
        *LINT_PATHS,
    )
    session.run("flake8", *LINT_PATHS)


@nox.session(python=DEFAULT_PYTHON_VERSION)
def blacken(session):
    """Run black. Format code to uniform standard."""
    session.install(BLACK_VERSION)
    session.run(
        "black",
        *LINT_PATHS,
    )


@nox.session(python=DEFAULT_PYTHON_VERSION)
def format(session):
    """
    Run isort to sort imports. Then run black
    to format code to uniform standard.
    """
    session.install(BLACK_VERSION, ISORT_VERSION)
    # Use the --fss option to sort imports using strict alphabetical order.
    # See https://pycqa.github.io/isort/docs/configuration/options.html#force-sort-within-sections
    session.run(
        "isort",
        *LINT_PATHS,
    )
    session.run(
        "black",
        *LINT_PATHS,
    )


@nox.session(python=DEFAULT_PYTHON_VERSION)
def lint_setup_py(session):
    """Verify that setup.py is valid (including RST check)."""
    session.install("docutils", "pygments")
    session.run("python", "setup.py", "check", "--restructuredtext", "--strict")


def install_unittest_dependencies(session, install_test_extra, *constraints):
    standard_deps = UNIT_TEST_STANDARD_DEPENDENCIES + UNIT_TEST_DEPENDENCIES
    session.install(*standard_deps, *constraints)

    if UNIT_TEST_EXTERNAL_DEPENDENCIES:
        warnings.warn(
            "'unit_test_external_dependencies' is deprecated. Instead, please "
            "use 'unit_test_dependencies' or 'unit_test_local_dependencies'.",
            DeprecationWarning,
        )
        session.install(*UNIT_TEST_EXTERNAL_DEPENDENCIES, *constraints)

    if UNIT_TEST_LOCAL_DEPENDENCIES:
        session.install(*UNIT_TEST_LOCAL_DEPENDENCIES, *constraints)

    if install_test_extra and UNIT_TEST_EXTRAS_BY_PYTHON:
        extras = UNIT_TEST_EXTRAS_BY_PYTHON.get(session.python, [])
    elif install_test_extra and UNIT_TEST_EXTRAS:
        extras = UNIT_TEST_EXTRAS
    else:
        extras = []

    if extras:
        session.install("-e", f".[{','.join(extras)}]", *constraints)
    else:
        session.install("-e", ".", *constraints)


def run_unit(session, install_test_extra):
    """Run the unit test suite."""
    constraints_path = str(
        CURRENT_DIRECTORY / "testing" / f"constraints-{session.python}.txt"
    )
    install_unittest_dependencies(session, install_test_extra, "-c", constraints_path)

    # Run py.test against the unit tests.
    tests_path = os.path.join("tests", "unit")
    third_party_tests_path = os.path.join("third_party", "bigframes_vendored")
    session.run(
        "py.test",
        "--quiet",
        f"--junitxml=unit_{session.python}_sponge_log.xml",
        "--cov=bigframes",
        f"--cov={tests_path}",
        "--cov-append",
        "--cov-config=.coveragerc",
        "--cov-report=term-missing",
        "--cov-fail-under=0",
        tests_path,
        third_party_tests_path,
        *session.posargs,
    )


@nox.session(python=UNIT_TEST_PYTHON_VERSIONS)
def unit(session):
    run_unit(session, install_test_extra=True)


@nox.session(python=UNIT_TEST_PYTHON_VERSIONS[-1])
def unit_noextras(session):
    run_unit(session, install_test_extra=False)


@nox.session(python=DEFAULT_PYTHON_VERSION)
def mypy(session):
    """Run type checks with mypy."""
    # Editable mode is not compatible with mypy when there are multiple
    # package directories. See:
    # https://github.com/python/mypy/issues/10564#issuecomment-851687749
    session.install(".")

    # Just install the dependencies' type info directly, since "mypy --install-types"
    # might require an additional pass.
    deps = (
        set(
            [
                "mypy",
                "pandas-stubs",
                "types-protobuf",
                "types-python-dateutil",
                "types-requests",
                "types-setuptools",
                "types-tabulate",
            ]
        )
        | set(SYSTEM_TEST_STANDARD_DEPENDENCIES)
        | set(UNIT_TEST_STANDARD_DEPENDENCIES)
    )

    session.install(*deps)
    shutil.rmtree(".mypy_cache", ignore_errors=True)
    session.run(
        "mypy",
        "bigframes",
        os.path.join("tests", "system"),
        os.path.join("tests", "unit"),
        "--explicit-package-bases",
        '--exclude="^third_party"',
    )


def install_systemtest_dependencies(session, install_test_extra, *constraints):
    # Use pre-release gRPC for system tests.
    # Exclude version 1.49.0rc1 which has a known issue.
    # See https://github.com/grpc/grpc/pull/30642
    session.install("--pre", "grpcio!=1.49.0rc1")

    session.install(*SYSTEM_TEST_STANDARD_DEPENDENCIES, *constraints)

    if SYSTEM_TEST_EXTERNAL_DEPENDENCIES:
        session.install(*SYSTEM_TEST_EXTERNAL_DEPENDENCIES, *constraints)

    if SYSTEM_TEST_LOCAL_DEPENDENCIES:
        session.install("-e", *SYSTEM_TEST_LOCAL_DEPENDENCIES, *constraints)

    if SYSTEM_TEST_DEPENDENCIES:
        session.install("-e", *SYSTEM_TEST_DEPENDENCIES, *constraints)

    if install_test_extra and SYSTEM_TEST_EXTRAS_BY_PYTHON:
        extras = SYSTEM_TEST_EXTRAS_BY_PYTHON.get(session.python, [])
    elif install_test_extra and SYSTEM_TEST_EXTRAS:
        extras = SYSTEM_TEST_EXTRAS
    else:
        extras = []

    if extras:
        session.install("-e", f".[{','.join(extras)}]", *constraints)
    else:
        session.install("-e", ".", *constraints)


def run_system(
    session: nox.sessions.Session,
    prefix_name,
    test_folder,
    *,
    check_cov=False,
    install_test_extra=True,
    print_duration=False,
    extra_pytest_options=(),
    timeout_seconds=900,
):
    """Run the system test suite."""
    constraints_path = str(
        CURRENT_DIRECTORY / "testing" / f"constraints-{session.python}.txt"
    )

    # Check the value of `RUN_SYSTEM_TESTS` env var. It defaults to true.
    if os.environ.get("RUN_SYSTEM_TESTS", "true") == "false":
        session.skip("RUN_SYSTEM_TESTS is set to false, skipping")
    # Install pyopenssl for mTLS testing.
    if os.environ.get("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false") == "true":
        session.install("pyopenssl")

    install_systemtest_dependencies(session, install_test_extra, "-c", constraints_path)

    # Run py.test against the system tests.
    pytest_cmd = [
        "py.test",
        "--quiet",
        "-n=20",
        # Any individual test taking longer than 15 mins will be terminated.
        f"--timeout={timeout_seconds}",
        # Log 20 slowest tests
        "--durations=20",
        f"--junitxml={prefix_name}_{session.python}_sponge_log.xml",
    ]
    if print_duration:
        pytest_cmd.extend(
            [
                "--durations=0",
            ]
        )
    if check_cov:
        pytest_cmd.extend(
            [
                "--cov=bigframes",
                f"--cov={test_folder}",
                "--cov-append",
                "--cov-config=.coveragerc",
                "--cov-report=term-missing",
                "--cov-fail-under=0",
            ]
        )

    pytest_cmd.extend(extra_pytest_options)
    session.run(
        *pytest_cmd,
        *session.posargs,
        test_folder,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS)
def system(session: nox.sessions.Session):
    """Run the system test suite."""
    run_system(
        session=session,
        prefix_name="system",
        test_folder=os.path.join("tests", "system", "small"),
        check_cov=True,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS[-1])
def system_noextras(session: nox.sessions.Session):
    """Run the system test suite."""
    run_system(
        session=session,
        prefix_name="system_noextras",
        test_folder=os.path.join("tests", "system", "small"),
        install_test_extra=False,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS[-1])
def doctest(session: nox.sessions.Session):
    """Run the system test suite."""
    run_system(
        session=session,
        prefix_name="doctest",
        extra_pytest_options=("--doctest-modules", "third_party"),
        test_folder="bigframes",
        check_cov=True,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS[-1])
def e2e(session: nox.sessions.Session):
    """Run the large tests in system test suite."""
    run_system(
        session=session,
        prefix_name="e2e",
        test_folder=os.path.join("tests", "system", "large"),
        print_duration=True,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS[-1])
def load(session: nox.sessions.Session):
    """Run the very large tests in system test suite."""
    run_system(
        session=session,
        prefix_name="load",
        test_folder=os.path.join("tests", "system", "load"),
        print_duration=True,
        timeout_seconds=60 * 60 * 12,
    )


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS)
def samples(session):
    """Run the samples test suite."""

    constraints_path = str(
        CURRENT_DIRECTORY / "testing" / f"constraints-{session.python}.txt"
    )

    # TODO(b/332735129): Remove this session and use python_samples templates
    # where each samples directory has its own noxfile.py file, instead.
    install_test_extra = True
    install_systemtest_dependencies(session, install_test_extra, "-c", constraints_path)

    session.run(
        "py.test",
        "samples",
        *session.posargs,
    )


@nox.session(python=DEFAULT_PYTHON_VERSION)
def cover(session):
    """Run the final coverage report.

    This outputs the coverage report aggregating coverage from the test runs
    (including system test runs), and then erases coverage data.
    """
    session.install("coverage", "pytest-cov")
    session.run("coverage", "report", "--show-missing", "--fail-under=90")

    # Make sure there is no dead code in our test directories.
    session.run(
        "coverage",
        "report",
        "--show-missing",
        "--include=tests/unit/*",
        "--include=tests/system/small/*",
        "--fail-under=100",
    )

    session.run("coverage", "erase")


@nox.session(python=DEFAULT_PYTHON_VERSION)
def docs(session):
    """Build the docs for this library."""

    session.install("-e", ".")
    session.install(
        # We need to pin to specific versions of the `sphinxcontrib-*` packages
        # which still support sphinx 4.x.
        # See https://github.com/googleapis/sphinx-docfx-yaml/issues/344
        # and https://github.com/googleapis/sphinx-docfx-yaml/issues/345.
        "sphinxcontrib-applehelp==1.0.4",
        "sphinxcontrib-devhelp==1.0.2",
        "sphinxcontrib-htmlhelp==2.0.1",
        "sphinxcontrib-qthelp==1.0.3",
        "sphinxcontrib-serializinghtml==1.1.5",
        SPHINX_VERSION,
        "alabaster",
        "recommonmark",
    )

    shutil.rmtree(os.path.join("docs", "_build"), ignore_errors=True)

    session.run(
        "python",
        "scripts/publish_api_coverage.py",
        "docs",
    )
    session.run(
        "sphinx-build",
        "-W",  # warnings as errors
        "-T",  # show full traceback on exception
        "-N",  # no colors
        "-b",
        "html",
        "-d",
        os.path.join("docs", "_build", "doctrees", ""),
        os.path.join("docs", ""),
        os.path.join("docs", "_build", "html", ""),
    )


@nox.session(python=DEFAULT_PYTHON_VERSION)
def docfx(session):
    """Build the docfx yaml files for this library."""

    session.install("-e", ".")
    session.install(
        # We need to pin to specific versions of the `sphinxcontrib-*` packages
        # which still support sphinx 4.x.
        # See https://github.com/googleapis/sphinx-docfx-yaml/issues/344
        # and https://github.com/googleapis/sphinx-docfx-yaml/issues/345.
        "sphinxcontrib-applehelp==1.0.4",
        "sphinxcontrib-devhelp==1.0.2",
        "sphinxcontrib-htmlhelp==2.0.1",
        "sphinxcontrib-qthelp==1.0.3",
        "sphinxcontrib-serializinghtml==1.1.5",
        SPHINX_VERSION,
        "alabaster",
        "recommonmark",
        "gcp-sphinx-docfx-yaml==3.0.1",
    )

    shutil.rmtree(os.path.join("docs", "_build"), ignore_errors=True)

    session.run(
        "python",
        "scripts/publish_api_coverage.py",
        "docs",
    )
    session.run(
        "sphinx-build",
        "-T",  # show full traceback on exception
        "-N",  # no colors
        "-D",
        (
            "extensions=sphinx.ext.autodoc,"
            "sphinx.ext.autosummary,"
            "docfx_yaml.extension,"
            "sphinx.ext.intersphinx,"
            "sphinx.ext.coverage,"
            "sphinx.ext.napoleon,"
            "sphinx.ext.todo,"
            "sphinx.ext.viewcode,"
            "recommonmark"
        ),
        "-b",
        "html",
        "-d",
        os.path.join("docs", "_build", "doctrees", ""),
        os.path.join("docs", ""),
        os.path.join("docs", "_build", "html", ""),
    )


def prerelease(session: nox.sessions.Session, tests_path):
    constraints_path = str(
        CURRENT_DIRECTORY / "testing" / f"constraints-{session.python}.txt"
    )

    # Ignore officially released versions of certain packages specified in
    # testing/constraints-*.txt and install a more recent, pre-release versions
    # directly
    already_installed = set()

    # PyArrow prerelease packages are published to an alternative PyPI host.
    # https://arrow.apache.org/docs/python/install.html#installing-nightly-packages
    session.install(
        "--extra-index-url",
        "https://pypi.fury.io/arrow-nightlies/",
        "--prefer-binary",
        "--pre",
        "--upgrade",
        "pyarrow",
    )
    already_installed.add("pyarrow")

    session.install(
        "--extra-index-url",
        "https://pypi.anaconda.org/scipy-wheels-nightly/simple",
        "--prefer-binary",
        "--pre",
        "--upgrade",
        # We exclude each version individually so that we can continue to test
        # some prerelease packages. See:
        # https://github.com/googleapis/python-bigquery-dataframes/pull/268#discussion_r1423205172
        # "pandas!=2.1.4, !=2.2.0rc0, !=2.2.0, !=2.2.1",
        "pandas",
    )
    already_installed.add("pandas")

    # Ibis has introduced breaking changes. Let's exclude ibis head
    # from prerelease install list for now. We should enable the head back
    # once bigframes supports the version at HEAD.
    # session.install(
    #     "--upgrade",
    #     "-e",  # Use -e so that py.typed file is included.
    #     "git+https://github.com/ibis-project/ibis.git#egg=ibis-framework",
    # )
    session.install(
        "--upgrade",
        "--pre",
        "ibis-framework>=8.0.0,<9.0.0dev",
    )
    already_installed.add("ibis-framework")

    # Workaround https://github.com/googleapis/python-db-dtypes-pandas/issues/178
    session.install("--no-deps", "db-dtypes")
    already_installed.add("db-dtypes")

    # Ensure we catch breaking changes in the client libraries early.
    session.install(
        "--upgrade",
        "git+https://github.com/googleapis/python-bigquery.git#egg=google-cloud-bigquery",
    )
    already_installed.add("google-cloud-bigquery")
    session.install(
        "--upgrade",
        "-e",
        "git+https://github.com/googleapis/python-bigquery-storage.git#egg=google-cloud-bigquery-storage",
    )
    already_installed.add("google-cloud-bigquery-storage")

    # Workaround to install pandas-gbq >=0.15.0, which is required by test only.
    session.install("--no-deps", "pandas-gbq")
    already_installed.add("pandas-gbq")

    session.install(
        *set(UNIT_TEST_STANDARD_DEPENDENCIES + SYSTEM_TEST_STANDARD_DEPENDENCIES),
        "-c",
        constraints_path,
    )

    # Because we test minimum dependency versions on the minimum Python
    # version, the first version we test with in the unit tests sessions has a
    # constraints file containing all dependencies and extras.
    with open(
        CURRENT_DIRECTORY
        / "testing"
        / f"constraints-{UNIT_TEST_PYTHON_VERSIONS[0]}.txt",
        encoding="utf-8",
    ) as constraints_file:
        constraints_text = constraints_file.read()

    # Ignore leading whitespace and comment lines.
    deps = [
        match.group(1)
        for match in re.finditer(
            r"^\s*(\S+)(?===\S+)", constraints_text, flags=re.MULTILINE
        )
        if match.group(1) not in already_installed
    ]

    # We use --no-deps to ensure that pre-release versions aren't overwritten
    # by the version ranges in setup.py.
    session.install(*deps)
    session.install("--no-deps", "-e", ".")

    # Print out prerelease package versions.
    session.run("python", "-m", "pip", "freeze")

    # Run py.test against the tests.
    session.run(
        "py.test",
        "--quiet",
        "-n=20",
        # Any individual test taking longer than 10 mins will be terminated.
        "--timeout=600",
        f"--junitxml={os.path.split(tests_path)[-1]}_prerelease_{session.python}_sponge_log.xml",
        "--cov=bigframes",
        f"--cov={tests_path}",
        "--cov-append",
        "--cov-config=.coveragerc",
        "--cov-report=term-missing",
        "--cov-fail-under=0",
        tests_path,
        *session.posargs,
    )


@nox.session(python=UNIT_TEST_PYTHON_VERSIONS[-1])
def unit_prerelease(session: nox.sessions.Session):
    """Run the unit test suite with prerelease dependencies."""
    prerelease(session, os.path.join("tests", "unit"))


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS[-1])
def system_prerelease(session: nox.sessions.Session):
    """Run the system test suite with prerelease dependencies."""
    prerelease(session, os.path.join("tests", "system", "small"))


@nox.session(python=SYSTEM_TEST_PYTHON_VERSIONS)
def notebook(session: nox.Session):
    GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not GOOGLE_CLOUD_PROJECT:
        session.error(
            "Set GOOGLE_CLOUD_PROJECT environment variable to run notebook session."
        )

    session.install("-e", ".[all]")
    session.install(
        "pytest",
        "pytest-xdist",
        "pytest-retry",
        "nbmake",
        "google-cloud-aiplatform",
        "matplotlib",
        "seaborn",
    )

    notebooks_list = list(Path("notebooks/").glob("*/*.ipynb"))

    denylist = [
        # Regionalized testing is manually added later.
        "notebooks/location/regionalized.ipynb",
        # These notebooks contain special colab `param {type:"string"}`
        # comments, which make it easy for customers to fill in their
        # own information.
        #
        # With the notebooks_fill_params.py script, we are able to find and
        # replace the PROJECT_ID parameter, but not the others.
        #
        # TODO(ashleyxu): Test these notebooks by replacing parameters with
        # appropriate values and omitting cleanup logic that may break
        # our test infrastructure.
        "notebooks/getting_started/ml_fundamentals_bq_dataframes.ipynb",  # Needs DATASET.
        "notebooks/regression/bq_dataframes_ml_linear_regression.ipynb",  # Needs DATASET_ID.
        "notebooks/generative_ai/bq_dataframes_ml_drug_name_generation.ipynb",  # Needs CONNECTION.
        # TODO(b/332737009): investigate why we get 404 errors, even though
        # bq_dataframes_llm_code_generation creates a bucket in the sample.
        "notebooks/generative_ai/bq_dataframes_llm_code_generation.ipynb",  # Needs BUCKET_URI.
        "notebooks/generative_ai/sentiment_analysis.ipynb",  # Too slow
        "notebooks/vertex_sdk/sdk2_bigframes_pytorch.ipynb",  # Needs BUCKET_URI.
        "notebooks/vertex_sdk/sdk2_bigframes_sklearn.ipynb",  # Needs BUCKET_URI.
        "notebooks/vertex_sdk/sdk2_bigframes_tensorflow.ipynb",  # Needs BUCKET_URI.
        # The experimental notebooks imagine features that don't yet
        # exist or only exist as temporary prototypes.
        "notebooks/experimental/longer_ml_demo.ipynb",
    ]

    # Convert each Path notebook object to a string using a list comprehension.
    notebooks = [str(nb) for nb in notebooks_list]

    # Remove tests that we choose not to test.
    notebooks = list(filter(lambda nb: nb not in denylist, notebooks))

    # Regionalized notebooks
    notebooks_reg = {
        "regionalized.ipynb": [
            "asia-southeast1",
            "eu",
            "europe-west4",
            "southamerica-west1",
            "us",
            "us-central1",
        ]
    }
    notebooks_reg = {
        os.path.join("notebooks/location", nb): regions
        for nb, regions in notebooks_reg.items()
    }

    # The pytest --nbmake exits silently with "no tests ran" message if
    # one of the notebook paths supplied does not exist. Let's make sure that
    # each path exists.
    for nb in notebooks + list(notebooks_reg):
        assert os.path.exists(nb), nb

    # TODO(shobs): For some reason --retries arg masks exceptions occurred in
    # notebook failures, and shows unhelpful INTERNALERROR. Investigate that
    # and enable retries if we can find a way to surface the real exception
    # bacause the notebook is running against real GCP and something may fail
    # due to transient issues.
    pytest_command = [
        "py.test",
        "--nbmake",
        "--nbmake-timeout=900",  # 15 minutes
    ]

    logging_name_env_var = "BIGFRAMES_PERFORMANCE_LOG_NAME"

    try:
        # Populate notebook parameters and make a backup so that the notebooks
        # are runnable.
        session.run(
            "python",
            CURRENT_DIRECTORY / "scripts" / "notebooks_fill_params.py",
            *notebooks,
        )

        # Run notebooks in parallel session.run's, since each notebook
        # takes an environment variable for performance logging
        processes = []
        for notebook in notebooks:
            session.env[logging_name_env_var] = os.path.basename(notebook)
            process = Process(
                target=session.run,
                args=(*pytest_command, notebook),
            )
            process.start()
            processes.append(process)

        for process in processes:
            process.join()

    finally:
        # Prevent our notebook changes from getting checked in to git
        # accidentally.
        session.run(
            "python",
            CURRENT_DIRECTORY / "scripts" / "notebooks_restore_from_backup.py",
            *notebooks,
        )

    # Additionally run regionalized notebooks in parallel session.run's.
    # Each notebook takes a different region via env param.
    processes = []
    for notebook, regions in notebooks_reg.items():
        for region in regions:
            session.env[logging_name_env_var] = os.path.basename(notebook)
            process = Process(
                target=session.run,
                args=(*pytest_command, notebook),
                kwargs={"env": {"BIGQUERY_LOCATION": region}},
            )
            process.start()
            processes.append(process)

    for process in processes:
        process.join()

    # when run via pytest, notebooks output a .bytesprocessed report
    # collect those reports and print a summary
    _print_bytes_processed_report()


def _print_bytes_processed_report():
    """Add an informational report about http queries and bytes
    processed to the testlog output for purposes of measuring
    bigquery-related performance changes.
    """
    print("---BIGQUERY USAGE REPORT---")
    cumulative_queries = 0
    cumulative_bytes = 0
    for report in Path("notebooks/").glob("*/*.bytesprocessed"):
        with open(report, "r") as f:
            filename = report.stem
            lines = f.read().splitlines()
            query_count = len(lines)
            total_bytes = sum([int(line) for line in lines])
            format_string = f"{filename} - query count: {query_count}, bytes processed sum: {total_bytes}"
            print(format_string)
            cumulative_bytes += total_bytes
            cumulative_queries += query_count
    print(
        "---total queries: {total_queries}, total bytes: {total_bytes}---".format(
            total_queries=cumulative_queries, total_bytes=cumulative_bytes
        )
    )


@nox.session(python="3.10")
def release_dry_run(session):
    env = {}

    # If the project root is not set, then take current directory as the project
    # root. See the release script for how the project root is set/used. This is
    # specially useful when the developer runs the nox session on local machine.
    if not os.environ.get("PROJECT_ROOT") and not os.environ.get(
        "KOKORO_ARTIFACTS_DIR"
    ):
        env["PROJECT_ROOT"] = "."
    session.run(".kokoro/release-nightly.sh", "--dry-run", env=env)
