# SPDX-FileCopyrightText: 2026 Cameron Aaron
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Parse Power BI's compact DSR (data shape result) payload into rows.

Each DM0 entry encodes one row against the current schema ("S"): values for
the fields not covered by the bitmasks arrive in order in "C"; bit i of "R"
repeats field i from the previous row, and bit i of "Ø" makes it null.
Integer values of dictionary-encoded fields index into "ValueDicts".
"""

import orjson


def parse_powerbi_dsr_bytes(raw_bytes):
    """Parse a raw querydata response body into (rows, restart_tokens)."""
    return parse_powerbi_dsr(orjson.loads(raw_bytes))


def _column_names(data):
    """Map each value code ("G0", "M1", ...) to its column name."""
    names = {}
    schema = []
    for item in data.get("descriptor", {}).get("Select", []):
        # Null entries are subtotal-only slots that keep the array index-aligned.
        if not item or not item.get("Value"):
            continue
        code = item["Value"]
        names[code] = item.get("Name", "Unknown").split(".", 1)[-1]
        schema.append({"N": code, "DN": item.get("DN")})
    return names, schema


def _dict_key(field, field_code, field_idx, value_dicts):
    """Which ValueDicts list an integer value of this field indexes into, if any."""
    key = field.get("DN")
    if key and key in value_dicts:
        return key
    if field_code in value_dicts:
        return field_code
    if field_code and field_code[0] == "G":
        return "D" + field_code[1:]
    fallback = f"D{field_idx}"
    return fallback if fallback in value_dicts else None


def _decode_row(entry, schema, field_codes, previous, value_dicts):
    r_mask = entry.get("R", 0)
    null_mask = entry.get("Ø", 0)
    values = entry.get("C", [])
    next_value = 0
    row = [None] * len(schema)

    for i, field_code in enumerate(field_codes):
        bit = 1 << i
        if r_mask & bit:
            val = previous[i]
        elif null_mask & bit:
            val = None
        elif next_value < len(values):
            val = values[next_value]
            next_value += 1
        else:
            val = entry.get(field_code)

        if type(val) is int:  # not bool: True/False are real values
            key = _dict_key(schema[i], field_code, i, value_dicts)
            lookup = value_dicts.get(key) if key else None
            if lookup is not None and 0 <= val < len(lookup):
                val = lookup[val]
        row[i] = val
    return row


def parse_powerbi_dsr(response_json):
    """Parse a Power BI querydata response into (rows, restart_tokens).

    A response without a data shape (no results, no DS) is an empty result.
    A data shape with a malformed row raises ValueError rather than silently
    returning the rows before it.
    """
    try:
        data = response_json["results"][0]["result"]["data"]
        ds = data["dsr"]["DS"][0]
    except (KeyError, IndexError, TypeError):
        return [], None

    restart_tokens = ds.get("RT")
    ph_list = ds.get("PH", [])
    if not ph_list:
        return [], restart_tokens

    value_dicts = ds.get("ValueDicts", {})
    names, schema = _column_names(data)
    field_codes = [f.get("N") for f in schema]
    columns = [names.get(code, code) for code in field_codes]
    previous = [None] * len(schema)
    rows = []

    for n, entry in enumerate(ph_list[0].get("DM0", []), 1):
        try:
            # Matrix/pivot rows ("X" without "C") are a different DSR flavor; a
            # flat table row never carries "X".
            if "X" in entry and "C" not in entry:
                continue
            if entry.get("S") is not None:
                schema = entry["S"]
                field_codes = [f.get("N") for f in schema]
                columns = [names.get(code, code) for code in field_codes]
                if len(previous) != len(schema):
                    previous = [None] * len(schema)
            if not schema:
                continue
            previous = _decode_row(entry, schema, field_codes, previous, value_dicts)
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise ValueError(f"malformed DSR row {n}: {exc!r}") from exc
        rows.append(dict(zip(columns, previous)))

    return rows, restart_tokens
