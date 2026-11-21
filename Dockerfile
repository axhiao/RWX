FROM buildpack-deps:bookworm-scm AS source
WORKDIR /src
RUN git init -q \
 && git fetch -q --depth=1 --no-tags --no-recurse-submodules https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
 && git checkout -q --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
 && rm -rf .git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY --from=source /src/ /opt/sqlglot/
WORKDIR /app
RUN python -m pip install --no-cache-dir python-dateutil==4.0.0 pytz==2026.6 tzdata==2026.6 typing_extensions==4.16.0 duckdb==2.0.0 pandas==3.1.1 numpy==2.5.5 \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY selector.py value_profiles.py replay.py dialect_state.py query_replay.py checkpoints.py ./
COPY maps/coverage.json maps/profiles.json maps/dialect-state.json.gz maps/queries.json.gz maps/call-inputs.json.gz maps/call-sites.json.gz maps/optimizer-fixtures.json.gz maps/
USER 65534:65534
ENTRYPOINT ["python", "/app/selector.py", "--replay"]
