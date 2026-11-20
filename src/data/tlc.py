from collections import Counter
from fractions import Fraction
from itertools import combinations
import random

import numpy as np
import pandas as pd
from shapely import covers, points, prepare

from src.common.geo import distance_m


PICKUP = "tpep_pickup_datetime"
DROPOFF = "tpep_dropoff_datetime"
COORDINATES = ["pickup_longitude", "pickup_latitude", "dropoff_longitude", "dropoff_latitude"]
COLUMNS = [PICKUP, DROPOFF, *COORDINATES, "passenger_count", "trip_distance"]
PRIMARY_REASONS = (
    "invalid_pickup_time", "before_warmup", "invalid_coordinates", "outside_manhattan",
    "invalid_party_size", "short_direct_od", "unreachable_road_od",
)


def _local_datetime(values):
    strings = values.astype("string")
    naive_iso = strings.str.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?", na=False)
    return pd.to_datetime(strings.where(naive_iso), format="ISO8601", errors="coerce")


def classify(frame, area, max_party, min_m, windows):
    pickup = _local_datetime(frame[PICKUP])
    dropoff = _local_datetime(frame[DROPOFF])
    start, evaluation, tail = [pd.Timestamp(value) for value in windows]
    raw_coords = frame[COORDINATES]
    xy = raw_coords.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    missing_coord = (raw_coords.isna() | raw_coords.eq("")).any(axis=1).to_numpy()
    malformed_coord = (~np.isfinite(xy) & ~(raw_coords.isna() | raw_coords.eq("")).to_numpy()).any(axis=1)
    out_of_range = (np.abs(xy) > [180, 90, 180, 90]).any(axis=1)
    zero_coord = (xy == 0).any(axis=1)
    invalid_coord = missing_coord | malformed_coord | out_of_range | zero_coord
    good_coord = ~invalid_coord
    origin_in = np.zeros(len(frame), dtype=bool)
    destination_in = np.zeros(len(frame), dtype=bool)
    origin_in[good_coord] = covers(area, points(xy[good_coord, :2]))
    destination_in[good_coord] = covers(area, points(xy[good_coord, 2:]))
    straight = np.full(len(frame), np.nan)
    straight[good_coord] = distance_m(xy[good_coord, :2], xy[good_coord, 2:])

    party_raw = frame["passenger_count"]
    party = pd.to_numeric(party_raw, errors="coerce").astype(float)
    missing_party = party_raw.isna() | party_raw.eq("")
    integer_party = np.isfinite(party) & party.mod(1).eq(0)
    good_party = integer_party & party.between(1, max_party)
    miles = pd.to_numeric(frame["trip_distance"], errors="coerce").astype(float)
    duration_s = (dropoff - pickup).dt.total_seconds()
    positive_history = np.isfinite(miles) & miles.gt(0) & duration_s.gt(0)
    flags = pd.DataFrame({
        "invalid_pickup_time": pickup.isna(),
        "invalid_dropoff_time": dropoff.isna(),
        "before_warmup": pickup.lt(start),
        "missing_coordinate": missing_coord,
        "non_numeric_or_nonfinite_coordinate": malformed_coord,
        "out_of_range_coordinate": out_of_range,
        "zero_coordinate": zero_coord,
        "invalid_coordinates": invalid_coord,
        "origin_outside_manhattan": good_coord & ~origin_in,
        "destination_outside_manhattan": good_coord & ~destination_in,
        "outside_manhattan": good_coord & ~(origin_in & destination_in),
        "missing_party_size": missing_party,
        "non_integer_or_invalid_party_size": ~integer_party & ~missing_party,
        "party_zero": party.eq(0),
        "party_negative": party.lt(0),
        "party_over_max": party.gt(max_party),
        "invalid_party_size": ~good_party,
        "short_direct_od": straight < min_m,
        "historical_duration_nonpositive": duration_s.le(0),
        "historical_duration_over_24h": duration_s.gt(86400),
        "historical_miles_invalid": ~np.isfinite(miles),
        "historical_miles_nonpositive": np.isfinite(miles) & miles.le(0),
        "historical_speed_over_100mph": positive_history & miles.gt(duration_s / 36),
    }, index=frame.index)

    data_reasons = PRIMARY_REASONS[:-1]
    reason = pd.Series(np.select([flags[name] for name in data_reasons], data_reasons, default=""), index=frame.index)
    valid = reason.eq("")
    data = frame.loc[valid].copy()
    data["request_time_local"] = pickup.loc[valid]
    data["hour"] = pickup.loc[valid].dt.strftime("%Y-%m-%dT%H")
    data["cohort"] = np.select(
        [pickup.loc[valid].lt(evaluation), pickup.loc[valid].lt(tail)], ["WARMUP", "EVALUATION"], default="TAIL"
    )
    data["raw_direct_od_m"] = straight[valid]
    data["historical_trip_flags"] = flags.loc[valid, [
        "invalid_dropoff_time", "historical_duration_nonpositive", "historical_duration_over_24h",
        "historical_miles_invalid", "historical_miles_nonpositive", "historical_speed_over_100mph",
    ]].apply(lambda column: column.astype(int)).dot(1 << np.arange(6))
    return data, flags, reason


def _time_audit(pickup, hourly, seconds, batch_load):
    for counter, values in (
        (hourly, pickup.dt.strftime("%Y-%m-%dT%H")),
        (seconds, pickup.dt.second),
        (batch_load, pickup.dt.floor("10s")),
    ):
        counter.update(values.value_counts().to_dict())


def audit_tlc(path, area, max_party, min_m, windows):
    types = dict(pd.read_csv(path, nrows=0).dtypes)
    if set(types) != set(COLUMNS):
        raise ValueError(f"Expected TLC columns {COLUMNS}; received {list(types)}")
    prepare(area)
    primary, flag_counts, overlaps = Counter(), Counter(), Counter()
    hourly, seconds, batch_load, parties = Counter(), Counter(), Counter(), Counter()
    area_membership = Counter()
    rows, candidates = 0, []
    pickup_min, pickup_max, dropoff_min, dropoff_max = None, None, None, None
    dtype_counts = Counter()
    exact_minute_rows = 0
    for frame in pd.read_csv(path, dtype=str, keep_default_na=False, chunksize=100000):
        frame.insert(0, "request_id", np.arange(rows + 1, rows + len(frame) + 1))
        rows += len(frame)
        eligible, flags, reasons = classify(frame, area, max_party, min_m, windows)
        candidates.append(eligible)
        primary.update(reasons.value_counts().to_dict())
        flag_counts.update(flags.sum().to_dict())
        flag_array = flags.to_numpy().T
        for i, j in combinations(range(len(flags.columns)), 2):
            count = int(np.count_nonzero(flag_array[i] & flag_array[j]))
            if count:
                overlaps[f"{flags.columns[i]} & {flags.columns[j]}"] += count
        good = ~flags["invalid_coordinates"]
        outside_o, outside_d = flags["origin_outside_manhattan"], flags["destination_outside_manhattan"]
        area_membership.update({
            "both_inside": int((good & ~outside_o & ~outside_d).sum()),
            "origin_only_inside": int((good & ~outside_o & outside_d).sum()),
            "destination_only_inside": int((good & outside_o & ~outside_d).sum()),
            "both_outside": int((good & outside_o & outside_d).sum()),
            "invalid_coordinates": int((~good).sum()),
        })
        pickup, dropoff = _local_datetime(frame[PICKUP]), _local_datetime(frame[DROPOFF])
        if pickup.notna().any():
            min_time, max_time = pickup.min(), pickup.max()
            pickup_min = min_time if pickup_min is None else min(min_time, pickup_min)
            pickup_max = max_time if pickup_max is None else max(max_time, pickup_max)
        if dropoff.notna().any():
            min_time, max_time = dropoff.min(), dropoff.max()
            dropoff_min = min_time if dropoff_min is None else min(min_time, dropoff_min)
            dropoff_max = max_time if dropoff_max is None else max(max_time, dropoff_max)
        _time_audit(pickup, hourly, seconds, batch_load)
        exact_minute_rows += int(pickup.eq(pickup.dt.floor("min")).sum())
        parties.update(frame["passenger_count"].value_counts().to_dict())
        for column in COORDINATES + ["passenger_count", "trip_distance"]:
            values = pd.to_numeric(frame[column], errors="coerce")
            dtype_counts[column] += int(values.notna().sum())

    date_counts = Counter()
    for hour, count in hourly.items():
        date_counts[hour[:10]] += count
    return pd.concat(candidates, ignore_index=True), {
        "source_rows": rows,
        "columns": list(types),
        "parsed_types": {PICKUP: "local datetime64[ns]", DROPOFF: "local datetime64[ns]",
                         **{c: "float64" for c in COORDINATES + ["trip_distance"]},
                         "passenger_count": "integer required (audited numerically)"},
        "numeric_parse_count_by_column": dict(dtype_counts),
        "pickup_time_range": [str(pickup_min), str(pickup_max)],
        "dropoff_time_range": [str(dropoff_min), str(dropoff_max)],
        "by_date_source": dict(sorted(date_counts.items())),
        "by_hour_source": dict(sorted(hourly.items())),
        "arrival_second_histogram": {int(k): int(v) for k, v in sorted(seconds.items())},
        "exact_minute_arrivals": exact_minute_rows,
        "exact_minute_fraction_of_parseable_pickups": exact_minute_rows / sum(hourly.values()) if hourly else None,
        "arrival_count_histogram_per_nonempty_10s_bucket": dict(sorted(Counter(batch_load.values()).items())),
        "passenger_count_raw_histogram": dict(sorted(parties.items())),
        "geography": dict(area_membership),
        "overlapping_flag_counts": dict(flag_counts),
        "nonzero_pairwise_flag_overlaps": dict(overlaps),
        "removed_by_primary_reason": {name: primary[name] for name in PRIMARY_REASONS},
        "pre_road_candidates": primary[""],
    }


def stratified_sample(eligible, rate, seed):
    fraction = Fraction(str(rate))
    if fraction < 0 or fraction > 1:
        raise ValueError("sample rate must be in [0, 1]")
    groups = eligible.sort_values("request_id").groupby(["cohort", "hour"], sort=True)
    quotas = groups.size().rename("eligible").reset_index()
    raw = quotas["eligible"] * fraction.numerator
    quotas["sampled"], remainders = divmod(raw, fraction.denominator)
    quotas["remainder"] = remainders
    for _, period in quotas.groupby("cohort", sort=True):
        total = round(fraction * int(period["eligible"].sum()))
        extras = total - int(period["sampled"].sum())
        priority = period.sort_values(["remainder", "hour"], ascending=[False, True])
        quotas.loc[priority.index[:extras], "sampled"] += 1
    quotas["actual_rate"] = quotas["sampled"] / quotas["eligible"]
    rng = random.Random(seed)
    sampled = []
    for row in quotas.itertuples(index=False):
        group = groups.get_group((row.cohort, row.hour))
        sampled.append(group.iloc[rng.sample(range(len(group)), row.sampled)])
    result = pd.concat(sampled) if sampled else eligible.iloc[:0]
    return result.sort_values([PICKUP, "request_id"]), quotas.drop(columns="remainder")
