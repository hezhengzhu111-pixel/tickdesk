"""Shared report revision ordering for presentation and point-in-time readers."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import polars as pl

FINANCIAL_TABLES = ("metrics", "income", "balance_sheet", "cash_flow", "shares")
REPORT_KEYS = ("symbol", "period_end")
SOURCE = "_financial_source"
OBSERVED_AT = "_financial_observed_at"
OBSERVATION_ONLY = "_financial_observation_only"
CONTENT_ID = "_financial_content_id"
REVISION_ID = "_financial_revision_id"
PROVENANCE = (SOURCE, OBSERVED_AT, OBSERVATION_ONLY, CONTENT_ID, REVISION_ID)
CONFLICTING_FIELDS = "financial_conflicting_fields"
_REPORT_DAY = "_financial_current_day"
_CAPTURE_TIME = "_financial_current_capture"


def report_date(expression: pl.Expr) -> pl.Expr:
    """Read the provider's calendar date without assigning a publication hour."""
    return (
        expression.cast(pl.String)
        .str.extract(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ])", 1)
        .str.to_date("%Y-%m-%d", strict=False)
    )


def _row_identities(frame: pl.DataFrame) -> pl.DataFrame:
    if REVISION_ID in frame.columns and frame[REVISION_ID].null_count() == 0:
        return frame

    def digest(value: object) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    content_ids = []
    revision_ids = []
    for row in frame.iter_rows(named=True):
        content_id = digest({
            name: int(value) if isinstance(value, float) and value.is_integer() else value
            for name, value in row.items()
            if name not in PROVENANCE and name != "announce_date" and value is not None
        })
        content_ids.append(content_id)
        revision_ids.append(digest([
            content_id, row["announce_date"], row.get(SOURCE), row.get(OBSERVATION_ONLY) or False,
            row.get(OBSERVED_AT),
        ]))
    return frame.with_columns(
        pl.Series(CONTENT_ID, content_ids, dtype=pl.String),
        pl.Series(REVISION_ID, revision_ids, dtype=pl.String),
    )


def merge_report_revisions(*frames: pl.DataFrame) -> pl.DataFrame:
    """Keep each distinct capture, deduplicating exact replays."""
    valid = []
    for frame in frames:
        if frame.is_empty() or not set(REPORT_KEYS) <= set(frame.columns):
            continue
        valid.append(frame.with_columns(
            pl.col("symbol").cast(pl.String),
            report_date(pl.col("period_end")).cast(pl.String),
            (
                report_date(pl.col("announce_date")).cast(pl.String)
                if "announce_date" in frame.columns else pl.lit(None, dtype=pl.String)
            ).alias("announce_date"),
        ))
    if not valid:
        return pl.DataFrame()

    # Widen provider data types before computing content identities. IDs already
    # in the archive describe their original payload and survive schema growth.
    combined = pl.concat(valid, how="diagonal_relaxed").drop_nulls(REPORT_KEYS)
    for column in (SOURCE, OBSERVED_AT, OBSERVATION_ONLY):
        if column not in combined.columns:
            dtype = pl.Boolean if column == OBSERVATION_ONLY else pl.String
            combined = combined.with_columns(pl.lit(None, dtype=dtype).alias(column))
    if REVISION_ID in combined.columns:
        new = combined.filter(pl.col(REVISION_ID).is_null())
        combined = pl.concat([
            combined.filter(pl.col(REVISION_ID).is_not_null()),
            _row_identities(new),
        ], how="diagonal_relaxed") if not new.is_empty() else combined
    else:
        combined = _row_identities(combined)
    # A capture with unchanged content can still establish a reversion or a
    # conflict once an overlapping sync commits its observations.
    return (
        combined.unique(subset=REVISION_ID)
        .sort([*REPORT_KEYS, "announce_date", OBSERVED_AT, REVISION_ID])
    )


def _current_columns(columns: Sequence[str]) -> tuple[list[str], list[str]]:
    fields = [column for column in columns if column not in (*PROVENANCE, CONFLICTING_FIELDS)]
    metrics = sorted(column for column in fields if column not in (*REPORT_KEYS, "announce_date"))
    return fields, metrics


def current_reports(frame: pl.DataFrame) -> pl.DataFrame:
    """Current fields per period, with unresolved same-clock disagreements withheld."""
    if not set(REPORT_KEYS) <= set(frame.columns):
        return frame
    fields, metrics = _current_columns(frame.columns)
    announcement = [pl.col("announce_date").max()] if "announce_date" in fields else []
    announce = (
        report_date(pl.col("announce_date"))
        if "announce_date" in frame.columns else pl.lit(None, dtype=pl.Date)
    )
    capture = (
        pl.col(OBSERVED_AT).cast(pl.String).str.to_datetime("%+", time_zone="UTC", strict=False)
        if OBSERVED_AT in frame.columns else pl.lit(None, dtype=pl.Datetime(time_zone="UTC"))
    )
    clock = [_REPORT_DAY, _CAPTURE_TIME]
    capture_rows = (
        frame.with_columns(announce.alias(_REPORT_DAY), capture.alias(_CAPTURE_TIME))
        # An undated row cannot supply a value attributed to a dated report.
        .filter(pl.col(_REPORT_DAY).is_not_null() | pl.col(_REPORT_DAY).max().over(REPORT_KEYS).is_null())
        .group_by(*REPORT_KEYS, *clock)
        .agg(
            *announcement,
            *(pl.struct(
                value=pl.col(column).drop_nulls().first(),
                disputed=pl.col(column).drop_nulls().n_unique() > 1,
            ).alias(column) for column in metrics),
        )
    )
    # Missing values provide no new evidence. A disputed field stays unknown
    # until a more recent clock provides a value for that particular field.
    newest = (
        capture_rows.sort([*REPORT_KEYS, *clock])
        .group_by(*REPORT_KEYS, maintain_order=True)
        .agg(
            *announcement,
            *(pl.col(column).filter(pl.col(column).struct.field("value").is_not_null()).last()
              for column in metrics),
        )
    )
    names = [
        pl.when(pl.col(column).struct.field("disputed")).then(pl.lit(column))
        for column in metrics
    ]
    quality = pl.concat_list(names).list.drop_nulls() if names else pl.lit([], dtype=pl.List(pl.String))
    return newest.select(
        *(pl.when(~pl.col(column).struct.field("disputed"))
          .then(pl.col(column).struct.field("value")).alias(column)
          if column in metrics else pl.col(column) for column in fields),
        quality.alias(CONFLICTING_FIELDS),
    )


def current_reports_sql(scan: str, columns: Sequence[str]) -> str:
    """DuckDB equivalent of current_reports for an existing parquet schema."""
    if not set(REPORT_KEYS) <= set(columns):
        return f"SELECT * FROM {scan}"

    def quote(column: str) -> str:
        return '"' + column.replace('"', '""') + '"'

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    fields, metrics = _current_columns(columns)
    report_day = (
        "TRY_CAST(regexp_extract(CAST(announce_date AS VARCHAR), "
        "'^(\\d{4}-\\d{2}-\\d{2})($|[T ])', 1) AS DATE)"
        if "announce_date" in columns else "CAST(NULL AS DATE)"
    )
    capture_text = f"CAST({quote(OBSERVED_AT)} AS VARCHAR)"
    capture_time = (
        f"CASE WHEN regexp_matches({capture_text}, '(Z|[+-][0-9]{{2}}:?[0-9]{{2}})$') "
        f"THEN TRY_CAST({capture_text} AS TIMESTAMPTZ) END"
        if OBSERVED_AT in columns else "CAST(NULL AS TIMESTAMPTZ)"
    )
    keys = ", ".join(REPORT_KEYS)
    clock = f"{_REPORT_DAY}, {_CAPTURE_TIME}"
    ordering = f"{_REPORT_DAY} DESC NULLS LAST, {_CAPTURE_TIME} DESC NULLS LAST"
    quoted_metrics = [quote(column) for column in metrics]
    announcement = ["max(announce_date) AS announce_date"] if "announce_date" in fields else []
    capture_values = ", ".join([
        keys, clock, *announcement,
        *(f"struct_pack(value := any_value({col}), disputed := count(DISTINCT {col}) > 1) AS {col}"
          for col in quoted_metrics),
    ])
    newest_values = ", ".join([
        keys, *announcement,
        *(f"first({col} ORDER BY {ordering}) FILTER (WHERE {col}.value IS NOT NULL) AS {col}"
          for col in quoted_metrics),
    ])
    def output_column(column: str) -> str:
        col = quote(column)
        if column in metrics:
            return f"CASE WHEN NOT {col}.disputed THEN {col}.value END AS {col}"
        return col

    values = ", ".join(output_column(column) for column in fields)
    name_cases = ", ".join(
        f"CASE WHEN {col}.disputed THEN {literal(column)} END"
        for column, col in zip(metrics, quoted_metrics, strict=True)
    )
    quality = f"list_filter([{name_cases}], field -> field IS NOT NULL)" if name_cases else "[]::VARCHAR[]"
    return f"""
        WITH reports AS (
                 SELECT *, {report_day} AS {_REPORT_DAY}, {capture_time} AS {_CAPTURE_TIME}
                 FROM {scan}
             ),
             eligible AS (
                 SELECT * FROM reports
                 QUALIFY {_REPORT_DAY} IS NOT NULL
                     OR max({_REPORT_DAY}) OVER (PARTITION BY {keys}) IS NULL
             ),
             captures AS (SELECT {capture_values} FROM eligible GROUP BY {keys}, {clock}),
             newest AS (
                 SELECT {newest_values} FROM captures GROUP BY {keys}
             )
        SELECT {values}, {quality} AS {CONFLICTING_FIELDS} FROM newest
    """


def point_in_time_report_timeline(
    frame: pl.DataFrame,
    fields: Sequence[str],
    *,
    include_announcement_day: bool = False,
    require_field_value: bool = False,
    include_evidence: bool = False,
    observed_clock: bool = False,
) -> pl.DataFrame:
    """Emit changes in the latest eligible report; dates are eligibility boundaries.

    Reused announcement dates do not date a correction. An unambiguous first
    version can use its reported announcement, and later observed payloads are
    bounded by their actual observation in Shanghai. An indistinguishable batch
    is omitted from research even though its rows are retained in the archive.
    The assistant's observed clock uses the capture instant for observed
    payloads and conservatively defers date-only releases to the following day.
    """
    if frame.is_empty() or not {*REPORT_KEYS, "announce_date"} <= set(frame.columns):
        return pl.DataFrame()

    announced = report_date(pl.col("announce_date"))
    observed = (
        pl.col(OBSERVED_AT).cast(pl.String).str.to_datetime("%+", time_zone="UTC", strict=False)
        if OBSERVED_AT in frame.columns else pl.lit(None, dtype=pl.Datetime(time_zone="UTC"))
    )
    observation_only = (
        pl.col(OBSERVATION_ONLY).fill_null(False)
        if OBSERVATION_ONLY in frame.columns else pl.lit(False)
    )
    content = pl.struct(fields)
    if CONTENT_ID in frame.columns:
        content = pl.col(CONTENT_ID).fill_null(content.struct.json_encode())

    def finite(column: str) -> pl.Expr:
        if column not in frame.columns:
            return pl.lit(None, dtype=pl.Float64).alias(column)
        numeric = pl.col(column).cast(pl.Float64, strict=False)
        return pl.when(numeric.is_finite()).then(numeric).alias(column)

    metadata = (*PROVENANCE, *REPORT_KEYS, "announce_date")
    if observed_clock:
        metadata += ("currency", "period_basis")
    evidence_columns = [column for column in metadata if column in frame.columns]
    evidence = [pl.struct(evidence_columns).struct.json_encode().alias("_evidence")] if include_evidence else []
    by_release = ["symbol", "_period", "_release"]
    observed_day = pl.col("_observed").dt.convert_time_zone("Asia/Shanghai").dt.date()
    if include_announcement_day:
        # The share-capital display uses the public date inclusively. Actual
        # observations can occur after the session on that date.
        observed_day = observed_day.dt.offset_by("1d")
    needs_observation = pl.col("_observation_only") | (
        pl.col("_observed").is_not_null() & (pl.col("_legacy") | (pl.col("_sequence") > 1))
    )
    available = pl.when(needs_observation).then(
        pl.when(pl.col("_observed").is_not_null()).then(pl.max_horizontal("_release", observed_day))
    ).otherwise(pl.col("_release"))
    capture_present = []
    distinct_capture = pl.lit(True)
    if observed_clock:
        period = report_date(pl.col("period_end"))
        announced = pl.coalesce(announced, pl.when(observed.is_not_null()).then(period))
        capture_present = [(pl.col(OBSERVED_AT).is_not_null() if OBSERVED_AT in frame.columns else pl.lit(False))
                           .alias("_capture_present")]
        provenance = [name for name in (SOURCE, "currency", "period_basis") if name in frame.columns]
        content = pl.struct(*fields, *provenance).struct.json_encode()
        distinct_capture = pl.col("_content").ne_missing(pl.col("_content").shift().over(by_release))
        date_boundary = pl.col("_release").dt.offset_by("1d").cast(pl.Datetime).dt.replace_time_zone(
            "Asia/Shanghai",
        ).dt.convert_time_zone("UTC")
        available = (
            pl.when(pl.col("_observed").is_not_null())
            .then(pl.when(pl.col("_release") <= observed_day).then(pl.col("_observed")))
            .otherwise(pl.when(~(pl.col("_observation_only") | pl.col("_capture_present"))).then(date_boundary))
        )
    events = (
        frame.with_columns(*(finite(column) for column in fields))
        .select(
            pl.col("symbol").cast(pl.String),
            report_date(pl.col("period_end")).alias("_period"),
            announced.alias("_release"),
            observed.alias("_observed"),
            observation_only.alias("_observation_only"),
            content.alias("_content"),
            *fields,
            *evidence,
            *capture_present,
        )
        .drop_nulls(by_release)
        .filter(pl.col("_period") <= pl.col("_release") if observed_clock else pl.lit(True))
        .unique(subset=[*by_release, "_observed", "_observation_only", "_content"], keep="first")
        .with_columns(
            pl.len().over([*by_release, "_observed"]).alias("_simultaneous"),
            pl.col("_observed").rank("dense").over(by_release).alias("_sequence"),
            pl.col("_observed").is_null().any().over(by_release).alias("_legacy"),
        )
        .filter(pl.col("_simultaneous") == 1)
        .with_columns(available.alias("_available"))
        .drop_nulls("_available")
        .sort(["symbol", "_period", "_available", "_release", "_observed"])
        .filter(distinct_capture)
        .filter(pl.col("_release") == pl.col("_release").cum_max().over(["symbol", "_period"]))
        # Carry each value's own contributor through the identical fill window.
        .with_columns(*(
            [pl.when(pl.col(column).is_not_null())
             .then(pl.struct("_evidence", "_available", "_release").struct.json_encode())
             .alias(f"_field_evidence_{column}") for column in fields]
            if include_evidence else []
        ))
        .with_columns(
            pl.col(fields).forward_fill().over(["symbol", "_period"]),
            *([pl.col(f"_field_evidence_{column}").forward_fill()
               .over(["symbol", "_period"]) for column in fields] if include_evidence else []),
        )
    )
    if require_field_value:
        events = events.filter(pl.any_horizontal(pl.col(fields).is_not_null()))
    return (
        events.sort(["symbol", "_available", "_period", "_release", "_observed"])
        .filter(pl.col("_period") == pl.col("_period").cum_max().over("symbol"))
        .unique(subset=["symbol", "_available"], keep="last", maintain_order=True)
        .select("symbol", "_available", pl.col("_release").alias("_announce"), *fields,
                *(["_evidence", pl.struct([
                    pl.col(f"_field_evidence_{column}").alias(column) for column in fields
                ]).alias("_field_evidence")] if include_evidence else []))
    )
