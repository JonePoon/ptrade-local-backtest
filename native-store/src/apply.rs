use crate::model::{Report, Result, TableReport};
use crate::{sql, stage, tables};
use duckdb::Connection;

fn join_keys(table: &tables::TableSpec, d: &str, s: &str) -> String {
    table
        .keys
        .iter()
        .map(|key| {
            let dest = format!("{d}.{}", sql::ident(key));
            let source = format!("{s}.{}", sql::ident(key));
            if matches!(*key, "code" | "index_code" | "ts_code") {
                format!("{}={source}", sql::code(&dest))
            } else {
                format!("{dest}={source}")
            }
        })
        .collect::<Vec<_>>()
        .join(" AND ")
}
pub fn apply(con: &Connection, report: &mut Report, name: &str) -> Result<()> {
    let spec = tables::spec(name);
    let stage_name = format!("__stage_{name}");
    let join = join_keys(spec, "d", "s");
    let key = stage::row_key("s", spec.keys);
    let columns = spec
        .columns
        .iter()
        .map(|(c, _)| sql::ident(c))
        .collect::<Vec<_>>();
    let quoted_table = sql::ident(name);
    let quoted_stage = sql::ident(&stage_name);
    let condition = if name == "ashare_index_weight" {
        "AND NOT (d.snapshot_date IS NULL AND coalesce(d.source,'') NOT LIKE 'cne:%')"
    } else {
        ""
    };
    // Per-key version check. Unknown Python-created provenance is zero, not a
    // made-up current timestamp that would suppress later legitimate corrections.
    con.execute_batch(&format!("CREATE OR REPLACE TEMP TABLE __accepted AS SELECT s.*,{key} _key FROM {quoted_stage} s LEFT JOIN __old_rowmeta m ON m.table_name={} AND m.row_key={key} WHERE s._version>=coalesce(m.fetched_at,0)",sql::lit(name)))?;
    let source = "__accepted";
    let active = if name == "ashare_calendar" {
        "AND s._active"
    } else {
        ""
    };
    let difference = spec
        .columns
        .iter()
        .map(|(c, _)| {
            let incoming = if name == "ashare_stock_basic"
                || name == "ashare_index_info_basic"
                || (name == "ashare_index_weight" && matches!(*c, "index_name" | "out_date"))
            {
                format!("coalesce(s.{},d.{})", sql::ident(c), sql::ident(c))
            } else {
                format!("s.{}", sql::ident(c))
            };
            format!("d.{} IS DISTINCT FROM {incoming}", sql::ident(c))
        })
        .collect::<Vec<_>>()
        .join(" OR ");
    let selected = sql::count(con, &format!("SELECT count(*) FROM {quoted_stage}"))?;
    let accepted = sql::count(con, "SELECT count(*) FROM __accepted")?;
    let inserted=sql::count(con,&format!("SELECT count(*) FROM {source} s WHERE NOT EXISTS(SELECT 1 FROM {quoted_table} d WHERE {join}) {active}"))?;
    let updated=sql::count(con,&format!("SELECT count(*) FROM {source} s JOIN {quoted_table} d ON {join} WHERE ({difference}) {condition} {active}"))?;
    let before = sql::count(con, &format!("SELECT count(*) FROM {quoted_table}"))?;
    let mut deleted = 0;
    if name == "ashare_calendar" {
        // An explicit is_trading=false is a tombstone for one calendar PK, not
        // a date-window deletion and never a deletion of price history.
        deleted=sql::count(con,&format!("SELECT count(*) FROM {quoted_table} d JOIN {source} s ON {join} WHERE NOT s._active"))?;
        con.execute_batch(&format!(
            "DELETE FROM {quoted_table} d USING {source} s WHERE {join} AND NOT s._active"
        ))?;
    } else {
        let assignments = spec
            .columns
            .iter()
            .map(|(c, _)| {
                let value = if name == "ashare_stock_basic"
                    || name == "ashare_index_info_basic"
                    || (name == "ashare_index_weight" && matches!(*c, "index_name" | "out_date"))
                {
                    format!("coalesce(s.{},d.{})", sql::ident(c), sql::ident(c))
                } else {
                    format!("s.{}", sql::ident(c))
                };
                format!("{}={value}", sql::ident(c))
            })
            .collect::<Vec<_>>()
            .join(",");
        con.execute_batch(&format!("UPDATE {quoted_table} d SET {assignments} FROM {source} s WHERE {join} AND ({difference}) {condition}"))?;
    }
    let projection = columns
        .iter()
        .map(|c| format!("s.{c}"))
        .collect::<Vec<_>>()
        .join(",");
    con.execute_batch(&format!("INSERT INTO {quoted_table}({}) SELECT {projection} FROM {source} s WHERE NOT EXISTS(SELECT 1 FROM {quoted_table} d WHERE {join}) {active}",columns.join(",")))?;
    con.execute_batch(&format!("INSERT INTO _native_store_rows SELECT {},_key,_version,_bar_fetch,_factor_fetch,_status_fetch FROM __accepted ON CONFLICT(table_name,row_key) DO UPDATE SET fetched_at=excluded.fetched_at,bar_fetched_at=excluded.bar_fetched_at,factor_fetched_at=excluded.factor_fetched_at,status_fetched_at=excluded.status_fetched_at",sql::lit(name)))?;
    let after = sql::count(con, &format!("SELECT count(*) FROM {quoted_table}"))?;
    report.tables.push(TableReport {
        table: name.into(),
        keys: spec.keys.iter().map(|s| (*s).into()).collect(),
        before,
        after,
        selected,
        inserted,
        updated,
        unchanged: accepted.saturating_sub(inserted + updated + deleted),
        stale: selected - accepted,
        deleted_keys: deleted,
    });
    report.after.insert(name.into(), after);
    Ok(())
}
