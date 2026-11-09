use crate::model::{Result, StoreError};
use crate::sql;
use duckdb::Connection;

pub struct TableSpec {
    pub name: &'static str,
    pub columns: &'static [(&'static str, &'static str)],
    pub keys: &'static [&'static str],
    pub required: bool,
}
const DAILY: &[(&str, &str)] = &[
    ("code", "VARCHAR"),
    ("date", "VARCHAR"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("money", "DOUBLE"),
    ("preclose", "DOUBLE"),
    ("adj_factor", "DOUBLE"),
    ("vwap", "DOUBLE"),
    ("name", "VARCHAR"),
    ("is_st", "INTEGER"),
    ("is_delisted", "INTEGER"),
    ("change", "DOUBLE"),
    ("pct_chg", "DOUBLE"),
];
const INDEX: &[(&str, &str)] = &[
    ("code", "VARCHAR"),
    ("date", "VARCHAR"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("money", "DOUBLE"),
    ("preclose", "DOUBLE"),
    ("change", "DOUBLE"),
    ("pct_chg", "DOUBLE"),
];
const MINUTE: &[(&str, &str)] = &[
    ("code", "VARCHAR"),
    ("date", "VARCHAR"),
    ("trade_time", "VARCHAR"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("money", "DOUBLE"),
    ("preclose", "DOUBLE"),
    ("change", "DOUBLE"),
    ("pct_chg", "DOUBLE"),
];
const MIN_INDEX: &[(&str, &str)] = &[
    ("code", "VARCHAR"),
    ("date", "VARCHAR"),
    ("trade_time", "VARCHAR"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("money", "DOUBLE"),
];
const BASIC: &[(&str, &str)] = &[
    ("code", "VARCHAR"),
    ("symbol", "VARCHAR"),
    ("name", "VARCHAR"),
    ("area", "VARCHAR"),
    ("industry", "VARCHAR"),
    ("fullname", "VARCHAR"),
    ("enname", "VARCHAR"),
    ("cnspell", "VARCHAR"),
    ("market", "VARCHAR"),
    ("exchange", "VARCHAR"),
    ("curr_type", "VARCHAR"),
    ("list_status", "VARCHAR"),
    ("list_date", "VARCHAR"),
    ("delist_date", "VARCHAR"),
    ("is_hs", "VARCHAR"),
    ("act_name", "VARCHAR"),
    ("act_ent_type", "VARCHAR"),
];
const WEIGHT: &[(&str, &str)] = &[
    ("index_code", "VARCHAR"),
    ("index_name", "VARCHAR"),
    ("code", "VARCHAR"),
    ("in_date", "VARCHAR"),
    ("out_date", "VARCHAR"),
    ("weight", "DOUBLE"),
    ("source", "VARCHAR"),
    ("snapshot_date", "VARCHAR"),
];
const INFO: &[(&str, &str)] = &[
    ("ts_code", "VARCHAR"),
    ("name", "VARCHAR"),
    ("market", "VARCHAR"),
    ("publisher", "VARCHAR"),
    ("category", "VARCHAR"),
    ("base_date", "VARCHAR"),
    ("base_point", "DOUBLE"),
    ("list_date", "VARCHAR"),
];
pub const TABLES: &[TableSpec] = &[
    TableSpec {
        name: "ashare_1d_stock",
        columns: DAILY,
        keys: &["code", "date"],
        required: true,
    },
    TableSpec {
        name: "ashare_1d_index",
        columns: INDEX,
        keys: &["code", "date"],
        required: true,
    },
    TableSpec {
        name: "ashare_1m_stock",
        columns: MINUTE,
        keys: &["code", "trade_time"],
        required: true,
    },
    TableSpec {
        name: "ashare_1m_index",
        columns: MIN_INDEX,
        keys: &["code", "trade_time"],
        required: false,
    },
    TableSpec {
        name: "ashare_stock_basic",
        columns: BASIC,
        keys: &["code"],
        required: true,
    },
    TableSpec {
        name: "ashare_calendar",
        columns: &[("date", "VARCHAR")],
        keys: &["date"],
        required: true,
    },
    TableSpec {
        name: "ashare_index_weight",
        columns: WEIGHT,
        keys: &["index_code", "code", "in_date"],
        required: false,
    },
    TableSpec {
        name: "ashare_index_info_basic",
        columns: INFO,
        keys: &["ts_code"],
        required: false,
    },
];
pub fn spec(name: &str) -> &'static TableSpec {
    TABLES
        .iter()
        .find(|t| t.name == name)
        .expect("internal table registry")
}
pub fn create_core(con: &Connection) -> Result<()> {
    for table in TABLES {
        if !sql::exists(con, table.name)? {
            con.execute_batch(&format!(
                "CREATE TABLE {} ({})",
                sql::ident(table.name),
                table
                    .columns
                    .iter()
                    .map(|(n, t)| format!("{} {t}", sql::ident(n)))
                    .collect::<Vec<_>>()
                    .join(",")
            ))?;
        } else {
            let current = sql::columns(con, table.name)?;
            for key in table.keys {
                if !current.contains(*key) {
                    return Err(
                        StoreError::schema(format!("existing table missing key {key}"))
                            .table(table.name),
                    );
                }
            }
            for (column, ty) in table.columns {
                if !current.contains(*column) {
                    con.execute_batch(&format!(
                        "ALTER TABLE {} ADD COLUMN {} {ty}",
                        sql::ident(table.name),
                        sql::ident(column)
                    ))?;
                }
            }
        }
    }
    con.execute_batch("CREATE TABLE IF NOT EXISTS _native_store_files(dataset VARCHAR,path VARCHAR,scope VARCHAR,sha256 VARCHAR,bytes UBIGINT,committed_at TIMESTAMP,PRIMARY KEY(dataset,path,scope));
        CREATE TABLE IF NOT EXISTS _native_store_rows(table_name VARCHAR,row_key VARCHAR,fetched_at BIGINT,bar_fetched_at BIGINT,factor_fetched_at BIGINT,status_fetched_at BIGINT,PRIMARY KEY(table_name,row_key));")?;
    Ok(())
}
pub fn old_views(con: &Connection) -> Result<()> {
    for table in TABLES {
        let present = sql::exists(con, table.name)?;
        let columns = if present {
            sql::columns(con, table.name)?
        } else {
            Default::default()
        };
        let projection = table
            .columns
            .iter()
            .map(|(name, ty)| {
                if !columns.contains(*name) {
                    return format!("NULL::{ty} AS {}", sql::ident(name));
                }
                let source = sql::ident(name);
                let expression = if matches!(*name, "code" | "index_code" | "ts_code") {
                    sql::code(&source)
                } else if matches!(
                    *name,
                    "date"
                        | "in_date"
                        | "out_date"
                        | "list_date"
                        | "delist_date"
                        | "snapshot_date"
                        | "base_date"
                ) {
                    sql::date8(&source)
                } else {
                    format!("TRY_CAST({source} AS {ty})")
                };
                format!("{expression} AS {}", sql::ident(name))
            })
            .collect::<Vec<_>>()
            .join(",");
        let from = if present {
            format!("FROM {}", sql::ident(table.name))
        } else {
            "WHERE FALSE".into()
        };
        con.execute_batch(&format!(
            "CREATE OR REPLACE TEMP VIEW {} AS SELECT {projection} {from}",
            sql::ident(&format!("__old_{}", table.name))
        ))?;
    }
    let meta = if sql::exists(con, "_native_store_rows")? {
        "SELECT * FROM _native_store_rows"
    } else {
        "SELECT NULL::VARCHAR table_name,NULL::VARCHAR row_key,NULL::BIGINT fetched_at,NULL::BIGINT bar_fetched_at,NULL::BIGINT factor_fetched_at,NULL::BIGINT status_fetched_at WHERE FALSE"
    };
    con.execute_batch(&format!(
        "CREATE OR REPLACE TEMP VIEW __old_rowmeta AS {meta}"
    ))?;
    Ok(())
}
