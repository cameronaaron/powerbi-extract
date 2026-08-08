from powerbi_extract.modules import QueryModule


def test_query_module_holds_its_fields():
    module = QueryModule(
        name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False)],
        query_template={"Query": {}},
        output_filename="out.csv",
    )

    assert module.name == "mod"
    assert module.from_entities == {"u": "Units"}
    assert module.select_columns == [("u", "Name", False)]
    assert module.query_template == {"Query": {}}
    assert module.output_filename == "out.csv"
