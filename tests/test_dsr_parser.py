from powerbi_extract.dsr_parser import parse_powerbi_dsr


def _wrap(ds):
    return {
        "results": [
            {
                "result": {
                    "data": {
                        "descriptor": {
                            "Select": [
                                {"Value": "G0", "Name": "Table.Name", "DN": "D0"},
                                {"Value": "G1", "Name": "Table.Score"},
                            ]
                        },
                        "dsr": {"DS": [ds]},
                    }
                }
            }
        ]
    }


def test_basic_rows_with_dictionary_and_plain_values():
    ds = {
        "ValueDicts": {"D0": ["Alice", "Bob"]},
        "PH": [
            {
                "DM0": [
                    {"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [0, 85]},
                    {"C": [1, 92]},
                ]
            }
        ],
    }
    rows, restart_tokens = parse_powerbi_dsr(_wrap(ds))

    assert restart_tokens is None
    assert rows == [
        {"Name": "Alice", "Score": 85},
        {"Name": "Bob", "Score": 92},
    ]


def test_repeated_value_uses_r_bitmask():
    ds = {
        "ValueDicts": {"D0": ["Alice", "Bob"]},
        "PH": [
            {
                "DM0": [
                    {"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [1, 92]},
                    {"R": 1, "C": [78]},
                ]
            }
        ],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [
        {"Name": "Bob", "Score": 92},
        {"Name": "Bob", "Score": 78},
    ]


def test_null_bitmask_produces_none():
    ds = {
        "ValueDicts": {},
        "PH": [
            {"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "Ø": 2, "C": ["Carol"]}]},
        ],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": "Carol", "Score": None}]


def test_restart_tokens_surfaced_for_pagination():
    ds = {
        "ValueDicts": {},
        "RT": {"some": "token"},
        "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": ["Dan", 10]}]}],
    }
    _, restart_tokens = parse_powerbi_dsr(_wrap(ds))

    assert restart_tokens == {"some": "token"}


def test_empty_ph_list_returns_no_rows():
    ds = {"ValueDicts": {}, "PH": []}
    rows, restart_tokens = parse_powerbi_dsr(_wrap(ds))

    assert rows == []
    assert restart_tokens is None


def test_malformed_payload_does_not_raise():
    rows, restart_tokens = parse_powerbi_dsr({"results": []})

    assert rows == []
    assert restart_tokens is None


def test_schema_change_mid_stream_resets_bit_positions_and_columns():
    ds = {
        "ValueDicts": {},
        "PH": [
            {
                "DM0": [
                    {"S": [{"N": "G0"}], "C": ["only-one-col"]},
                    {"S": [{"N": "G0"}, {"N": "G1"}], "C": ["x", 1]},
                ]
            }
        ],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": "only-one-col"}, {"Name": "x", "Score": 1}]


def test_g_prefixed_field_falls_back_to_d_prefixed_dict():
    ds = {
        "ValueDicts": {"D1": ["low", "high"]},
        "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": ["Eve", 1]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": "Eve", "Score": "high"}]


def test_field_code_directly_present_in_value_dicts():
    ds = {
        "ValueDicts": {"G1": ["zero", "one"]},
        "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": ["Frank", 1]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": "Frank", "Score": "one"}]


def test_index_based_d_fallback_when_no_dn_and_no_g_prefix_match():
    ds = {
        "ValueDicts": {"D1": ["nope"], "D-index": []},
        "PH": [{"DM0": [{"S": [{"N": "X0"}, {"N": "X1"}], "C": ["Gwen", 1]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"X0": "Gwen", "X1": 1}]


def test_out_of_range_dict_index_is_left_as_raw_int():
    ds = {
        "ValueDicts": {"D0": ["only-one"]},
        "PH": [{"DM0": [{"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [5, 1]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": 5, "Score": 1}]


def test_bool_values_are_never_treated_as_dict_indices():
    ds = {
        "ValueDicts": {"D0": ["Alice"]},
        "PH": [{"DM0": [{"S": [{"N": "G0", "DN": "D0"}, {"N": "G1"}], "C": [True, False]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": True, "Score": False}]


def test_repeated_bit_beyond_last_row_length_defaults_handled():
    ds = {
        "ValueDicts": {},
        "PH": [
            {
                "DM0": [
                    {"S": [{"N": "G0"}], "C": ["solo"]},
                    {"S": [{"N": "G0"}, {"N": "G1"}], "R": 1, "C": [42]},
                ]
            }
        ],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows[0] == {"Name": "solo"}
    assert rows[1]["Score"] == 42


def test_select_descriptor_without_value_is_skipped():
    ds = {
        "ValueDicts": {},
        "PH": [{"DM0": [{"S": [{"N": "G0"}], "C": ["Hank"]}]}],
    }
    payload = _wrap(ds)
    payload["results"][0]["result"]["data"]["descriptor"]["Select"].append(
        {"Name": "Table.NoValue"}
    )
    rows, _ = parse_powerbi_dsr(payload)

    assert rows == [{"Name": "Hank"}]


def test_matrix_shaped_x_entries_are_skipped_not_emitted_as_null_rows():
    # Matrix/pivot visuals encode rows as nested {"X": [...]} aggregate slices
    # instead of the flat {"S"/"C"/"R"} shape — the parser doesn't support
    # flattening that hierarchy, so it must skip these rather than silently
    # emit a row of all-None values that looks like real (masked) data.
    ds = {
        "ValueDicts": {},
        "PH": [
            {"DM0": [{"X": [{"S": [{"N": "A0", "T": 4}], "A0": 14811}, {"A0": 12959}]}]},
        ],
    }
    payload = _wrap(ds)

    rows, _ = parse_powerbi_dsr(payload)

    assert rows == []


def test_null_select_descriptor_is_skipped_not_crashed():
    # A subtotal-only slot (referenced elsewhere via an "A"-prefixed code) shows
    # up as a bare null in Select to keep the array index-aligned with the
    # schema — this must be skipped like a missing Value, not crash the parse.
    ds = {
        "ValueDicts": {},
        "PH": [{"DM0": [{"S": [{"N": "G0"}], "C": ["Hank"]}]}],
    }
    payload = _wrap(ds)
    payload["results"][0]["result"]["data"]["descriptor"]["Select"].insert(0, None)
    payload["results"][0]["result"]["data"]["descriptor"]["Select"].append(None)

    rows, _ = parse_powerbi_dsr(payload)

    assert rows == [{"Name": "Hank"}]


def test_no_schema_at_all_yields_no_rows():
    ds = {"ValueDicts": {}, "PH": [{"DM0": [{"C": []}]}]}
    payload = {
        "results": [
            {"result": {"data": {"descriptor": {"Select": []}, "dsr": {"DS": [ds]}}}}
        ]
    }
    rows, _ = parse_powerbi_dsr(payload)

    assert rows == []


def test_single_column_value_read_directly_from_field_code_when_no_c_array():
    ds = {
        "ValueDicts": {},
        "PH": [
            {
                "DM0": [
                    {"S": [{"N": "G0"}], "G0": "< 20 yrs old"},
                    {"G0": "20-24 yrs old"},
                ]
            }
        ],
    }
    payload = {
        "results": [
            {
                "result": {
                    "data": {
                        "descriptor": {"Select": [{"Value": "G0", "Name": "Table.Age Group"}]},
                        "dsr": {"DS": [ds]},
                    }
                }
            }
        ]
    }
    rows, _ = parse_powerbi_dsr(payload)

    assert rows == [{"Age Group": "< 20 yrs old"}, {"Age Group": "20-24 yrs old"}]


def test_field_code_direct_value_resolves_through_value_dict_when_int():
    ds = {
        "ValueDicts": {"D0": ["low", "high"]},
        "PH": [{"DM0": [{"S": [{"N": "G0", "DN": "D0"}], "G0": 1}]}],
    }
    payload = {
        "results": [
            {
                "result": {
                    "data": {
                        "descriptor": {"Select": [{"Value": "G0", "Name": "Table.Level"}]},
                        "dsr": {"DS": [ds]},
                    }
                }
            }
        ]
    }
    rows, _ = parse_powerbi_dsr(payload)

    assert rows == [{"Level": "high"}]


def test_missing_c_values_beyond_available_defaults_to_none():
    ds = {
        "ValueDicts": {},
        "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": ["OnlyOne"]}]}],
    }
    rows, _ = parse_powerbi_dsr(_wrap(ds))

    assert rows == [{"Name": "OnlyOne", "Score": None}]
