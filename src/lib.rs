use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};
use std::collections::HashMap;

fn dget<'py>(obj: &Bound<'py, PyAny>, key: &str) -> Option<Bound<'py, PyAny>> {
    obj.cast::<PyDict>().ok()?.get_item(key).ok().flatten()
}

fn aget<'py>(obj: &Bound<'py, PyAny>, idx: usize) -> Option<Bound<'py, PyAny>> {
    let list = obj.cast::<PyList>().ok()?;
    if idx < list.len() {
        list.get_item(idx).ok()
    } else {
        None
    }
}

fn as_list<'py>(obj: &Bound<'py, PyAny>) -> Vec<Bound<'py, PyAny>> {
    obj.cast::<PyList>()
        .map(|l| l.iter().collect())
        .unwrap_or_default()
}

fn as_str(obj: &Bound<'_, PyAny>) -> Option<String> {
    obj.extract::<String>().ok()
}

fn as_u64(obj: &Bound<'_, PyAny>) -> Option<u64> {
    obj.extract::<u64>().ok()
}

fn is_int_value(obj: &Bound<'_, PyAny>) -> bool {
    if obj.is_instance_of::<PyBool>() {
        return false;
    }
    obj.extract::<i64>().is_ok() || obj.extract::<u64>().is_ok()
}

fn as_index(obj: &Bound<'_, PyAny>) -> Option<usize> {
    if let Ok(i) = obj.extract::<i64>() {
        if i >= 0 {
            return Some(i as usize);
        }
        return None;
    }
    obj.extract::<u64>().ok().map(|u| u as usize)
}

fn schema_field_codes(schema: &[Bound<'_, PyAny>]) -> Vec<String> {
    schema
        .iter()
        .map(|f| dget(f, "N").and_then(|v| as_str(&v)).unwrap_or_default())
        .collect()
}

fn resolve_dict_key<'py>(
    field_info: &Bound<'py, PyAny>,
    field_code: &str,
    field_idx: usize,
    value_dicts: &Bound<'py, PyDict>,
) -> Option<String> {
    if let Some(dn) = dget(field_info, "DN").and_then(|v| as_str(&v)) {
        if value_dicts.contains(&dn).unwrap_or(false) {
            return Some(dn);
        }
    }
    if value_dicts.contains(field_code).unwrap_or(false) {
        return Some(field_code.to_string());
    }
    if let Some(rest) = field_code.strip_prefix('G') {
        return Some(format!("D{}", rest));
    }
    let fallback = format!("D{}", field_idx);
    if value_dicts.contains(&fallback).unwrap_or(false) {
        return Some(fallback);
    }
    None
}

fn parse_impl<'py>(
    py: Python<'py>,
    response_json: &Bound<'py, PyAny>,
) -> Option<(Bound<'py, PyList>, Bound<'py, PyAny>)> {
    let results = dget(response_json, "results")?;
    let first_result = aget(&results, 0)?;
    let result = dget(&first_result, "result")?;
    let data = dget(&result, "data")?;
    let dsr = dget(&data, "dsr")?;
    let ds_list = dget(&dsr, "DS")?;
    let ds = aget(&ds_list, 0)?;

    let ph_list = dget(&ds, "PH").map(|v| as_list(&v)).unwrap_or_default();
    let restart_tokens = dget(&ds, "RT").unwrap_or_else(|| py.None().into_bound(py));
    let value_dicts = dget(&ds, "ValueDicts").and_then(|v| v.cast_into::<PyDict>().ok());
    let empty_dict = PyDict::new(py);
    let value_dicts = value_dicts.unwrap_or(empty_dict);

    let select_descriptors = dget(&data, "descriptor")
        .and_then(|d| dget(&d, "Select"))
        .map(|v| as_list(&v))
        .unwrap_or_default();

    let mut val_to_name: HashMap<String, String> = HashMap::new();
    let mut default_schema: Vec<Bound<'py, PyAny>> = Vec::new();
    for item in &select_descriptors {
        let val_code = match dget(item, "Value").and_then(|v| as_str(&v)) {
            Some(s) if !s.is_empty() => s,
            _ => continue,
        };
        let name = dget(item, "Name").and_then(|v| as_str(&v)).unwrap_or_else(|| "Unknown".to_string());
        let clean_name = name.splitn(2, '.').last().unwrap_or(&name).to_string();
        val_to_name.insert(val_code.clone(), clean_name);

        let schema_item = PyDict::new(py);
        schema_item.set_item("N", &val_code).ok()?;
        if let Some(dn) = dget(item, "DN") {
            schema_item.set_item("DN", dn).ok()?;
        }
        default_schema.push(schema_item.into_any());
    }

    if ph_list.is_empty() {
        return Some((PyList::empty(py), restart_tokens));
    }

    let dm0 = dget(&ph_list[0], "DM0").map(|v| as_list(&v)).unwrap_or_default();

    let mut current_schema = default_schema;
    let mut field_codes = schema_field_codes(&current_schema);
    let mut schema_len = current_schema.len();
    let mut col_names: Vec<String> = field_codes
        .iter()
        .map(|code| val_to_name.get(code).cloned().unwrap_or_else(|| code.clone()))
        .collect();
    let none_val = py.None().into_bound(py);
    let mut last_row_values: Vec<Bound<'py, PyAny>> = vec![none_val.clone(); schema_len];

    let rows_list = PyList::empty(py);

    for entry in &dm0 {
        if let Some(schema_obj) = dget(entry, "S") {
            current_schema = as_list(&schema_obj);
            field_codes = schema_field_codes(&current_schema);
            schema_len = current_schema.len();
            col_names = field_codes
                .iter()
                .map(|code| val_to_name.get(code).cloned().unwrap_or_else(|| code.clone()))
                .collect();
            if last_row_values.len() != schema_len {
                last_row_values = vec![none_val.clone(); schema_len];
            }
        }

        if schema_len == 0 {
            continue;
        }

        let r_mask = dget(entry, "R").and_then(|v| as_u64(&v)).unwrap_or(0);
        let null_mask = dget(entry, "Ø").and_then(|v| as_u64(&v)).unwrap_or(0);
        let c_vals = dget(entry, "C").map(|v| as_list(&v)).unwrap_or_default();
        let mut c_idx = 0usize;

        let mut row_values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(schema_len);

        for field_idx in 0..schema_len {
            let bit = 1u64 << field_idx;
            let mut val: Bound<'py, PyAny> = if r_mask & bit != 0 {
                last_row_values
                    .get(field_idx)
                    .cloned()
                    .unwrap_or_else(|| none_val.clone())
            } else if null_mask & bit != 0 {
                none_val.clone()
            } else if c_idx < c_vals.len() {
                let v = c_vals[c_idx].clone();
                c_idx += 1;
                v
            } else {
                none_val.clone()
            };

            if is_int_value(&val) {
                let field_info = &current_schema[field_idx];
                let field_code = &field_codes[field_idx];
                if let Some(key) = resolve_dict_key(field_info, field_code, field_idx, &value_dicts) {
                    if let Some(dict_list_obj) = value_dicts.get_item(&key).ok().flatten() {
                        if let Ok(dict_list) = dict_list_obj.cast::<PyList>() {
                            if let Some(idx) = as_index(&val) {
                                if idx < dict_list.len() {
                                    val = dict_list.get_item(idx).ok().unwrap_or(val);
                                }
                            }
                        }
                    }
                }
            }

            row_values.push(val);
        }

        last_row_values = row_values.clone();

        let row_dict = PyDict::new(py);
        for (name, val) in col_names.iter().zip(row_values.iter()) {
            row_dict.set_item(name, val).ok()?;
        }
        rows_list.append(row_dict).ok()?;
    }

    Some((rows_list, restart_tokens))
}

#[pyfunction]
fn parse_dsr(py: Python<'_>, response_json: &Bound<'_, PyAny>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
    match parse_impl(py, response_json) {
        Some((rows, restart)) => Ok((rows.into_any().unbind(), restart.unbind())),
        None => Ok((PyList::empty(py).into_any().unbind(), py.None())),
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_dsr, m)?)?;
    Ok(())
}
