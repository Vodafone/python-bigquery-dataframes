FROM python:3.10.14-slim

ARG proxy="http://139.7.95.77:8080"
RUN pip install poetry==1.4.2

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=0 \
    POETRY_CACHE_DIR='/tmp/poetry_cache' \
    POETRY_VERSION=1.8.2 \
    HTTP_PROXY=http://139.7.95.77:8080 \
    HTTPS_PROXY=http://139.7.95.77:8080

WORKDIR /src

RUN pip install poetry==1.8.2 --ignore-installed --no-root && rm -rf $POETRY_CACHE_DIR

CMD ["\bin\bash"]
# Project initialization:
RUN poetry install --without dev

# Creating folders, and files for a project:
COPY pyproject.toml poetry.lock env.sh /src/
