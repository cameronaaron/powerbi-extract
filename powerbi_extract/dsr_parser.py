"""Parse Power BI's compact DSR (data shape result) bitwise-encoded payload."""

import orjson

try:
    from powerbi_extract import _native
except ImportError:
    _native = None


def parse_powerbi_dsr_bytes(raw_bytes):
    """Parse a raw querydata response body into (rows, restart_tokens)."""
    return parse_powerbi_dsr(orjson.loads(raw_bytes))


def parse_powerbi_dsr_native(response_json):
    """Same result as :func:`parse_powerbi_dsr`, via the compiled Rust extension. Benchmarked slower than the pure-Python path (PyO3 per-object overhead), kept for experimentation only."""
    if _native is None:
        raise RuntimeError("the powerbi_extract native extension is not built")
    return _native.parse_dsr(response_json)


def parse_powerbi_dsr(response_json):
    """Parse a Power BI querydata response into (rows, restart_tokens)."""
    parsed_rows = []
    restart_tokens = None

    try:
        data = response_json["results"][0]["result"]["data"]
        ds = data["dsr"]["DS"][0]

        ph_list = ds.get("PH", [])
        restart_tokens = ds.get("RT")
        value_dicts = ds.get("ValueDicts", {})
        select_descriptors = data.get("descriptor", {}).get("Select", [])

        val_to_name = {}
        default_schema = []
        for item in select_descriptors:
            val_code = item.get("Value")
            if not val_code:
                continue
            val_to_name[val_code] = item.get("Name", "Unknown").split(".", 1)[-1]
            default_schema.append({"N": val_code, "DN": item.get("DN")})

        if not ph_list:
            return [], restart_tokens

        dm0 = ph_list[0].get("DM0", [])
        current_schema = default_schema
        schema_len = len(current_schema)
        bit_positions = [1 << i for i in range(schema_len)]
        field_codes = [f.get("N") for f in current_schema]
        col_names = [val_to_name.get(code, code) for code in field_codes]
        last_row_values = [None] * schema_len

        for entry in dm0:
            schema = entry.get("S")
            if schema is not None:
                current_schema = schema
                schema_len = len(current_schema)
                bit_positions = [1 << i for i in range(schema_len)]
                field_codes = [f.get("N") for f in current_schema]
                col_names = [val_to_name.get(code, code) for code in field_codes]
                if len(last_row_values) != schema_len:
                    last_row_values = [None] * schema_len

            if not schema_len:
                continue

            r_mask = entry.get("R", 0)
            null_mask = entry.get("Ø", 0)
            c_vals = entry.get("C", [])
            c_idx = 0
            n_c_vals = len(c_vals)
            row_values = [None] * schema_len

            for field_idx in range(schema_len):
                bit = bit_positions[field_idx]
                field_code = field_codes[field_idx]
                if r_mask & bit:
                    val = last_row_values[field_idx]
                elif null_mask & bit:
                    val = None
                elif c_idx < n_c_vals:
                    val = c_vals[c_idx]
                    c_idx += 1
                elif field_code in entry:
                    val = entry[field_code]
                else:
                    val = None

                if type(val) is int:
                    field_info = current_schema[field_idx]
                    dict_key = field_info.get("DN")
                    if not dict_key or dict_key not in value_dicts:
                        if field_code in value_dicts:
                            dict_key = field_code
                        elif field_code and field_code[0] == "G":
                            dict_key = "D" + field_code[1:]
                        else:
                            fallback = f"D{field_idx}"
                            dict_key = fallback if fallback in value_dicts else None

                    if dict_key:
                        dict_list = value_dicts.get(dict_key)
                        if dict_list is not None and 0 <= val < len(dict_list):
                            val = dict_list[val]

                row_values[field_idx] = val

            last_row_values = row_values
            parsed_rows.append(dict(zip(col_names, row_values)))

    except (KeyError, IndexError, TypeError):
        pass

    return parsed_rows, restart_tokens
