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

"""Utilities for managing a default, globally available Session object."""

import threading
from typing import Callable, Optional, TypeVar

import bigframes._config
import bigframes.session

_global_session: Optional[bigframes.session.Session] = None
_global_session_lock = threading.Lock()


def close_session(session_id: Optional[str] = None, skip_cleanup: bool = False) -> None:
    """If session_id is not provided, starts a fresh session the
    next time a function requires a session. Also closes the current
    default session if it was already started (unless skip_cleanup is True).

    If session_id is provided, searches for temporary resources
    with the corresponding session_id and deletes them. In this case,
    the current default session is not affected.

    Args:
        session_id (str, default None):
            The session to close. If not provided, the default
            global session will be closed.

    Returns:
        None
    """
    global _global_session

    if session_id is None:
        with _global_session_lock:
            if _global_session is not None:
                if not skip_cleanup:
                    _global_session.close()
                _global_session = None

            bigframes._config.options.bigquery._session_started = False
    else:
        client = get_global_session().bqclient

        for dataset in client.list_datasets(include_all=True, page_size=1000):
            if dataset.dataset_id[0] != "_":
                continue
            bigframes.session._delete_tables_matching_session_id(
                client, dataset, session_id
            )


def get_global_session():
    """Gets the global session.

    Creates the global session if it does not exist.
    """
    global _global_session, _global_session_lock

    with _global_session_lock:
        if _global_session is None:
            _global_session = bigframes.session.connect(
                bigframes._config.options.bigquery
            )

    return _global_session


_T = TypeVar("_T")


def with_default_session(func: Callable[..., _T], *args, **kwargs) -> _T:
    return func(get_global_session(), *args, **kwargs)
