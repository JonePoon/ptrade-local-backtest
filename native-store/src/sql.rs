use crate::config::JobConfig;
use crate::model::Result;
use duckdb::Connection;
use std::collections::BTreeSet;

pub fn lit(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}
pub fn ident(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}
pub fn code(value: &str) -> String {
    format!("CASE WHEN right(upper(trim(CAST({value} AS VARCHAR))),3)='.SH' THEN left(upper(trim(CAST({value} AS VARCHAR))),6)||'.SS' ELSE upper(trim(CAST({value} AS VARCHAR))) END")
}
pub fn day(value: &str) -> String {
    format!("CAST(coalesce(TRY_STRPTIME(CAST({value} AS VARCHAR),'%Y%m%d'),TRY_STRPTIME(substr(CAST({value} AS VARCHAR),1,10),'%Y-%m-%d')) AS DATE)")
}
pub fn date8(value: &str) -> String {
    format!("strftime({},'%Y%m%d')", day(value))
}
pub fn symbol_filter(cfg: &JobConfig, column: &str) -> String {
    if cfg.symbols.is_empty() {
        "TRUE".into()
    } else {
        format!(
            "{column} IN ({})",
            cfg.symbols
                .iter()
                .map(|s| lit(s))
                .collect::<Vec<_>>()
                .join(",")
        )
    }
}
pub fn bounds(cfg: &JobConfig, column: &str, history: bool) -> String {
    let mut predicates = vec![format!("{column} IS NOT NULL")];
    if !history {
        if let Some(start) = &cfg.start {
            predicates.push(format!("{column}>={}", lit(start)));
        }
    }
    if let Some(end) = &cfg.end {
        predicates.push(format!("{column}<={}", lit(end)));
    }
    predicates.join(" AND ")
}
pub fn count(con: &Connection, sql: &str) -> Result<u64> {
    Ok(con.query_row(sql, [], |row| row.get::<_, i64>(0))? as u64)
}
pub fn texts(con: &Connection, sql: &str) -> Result<Vec<String>> {
    let mut statement = con.prepare(sql)?;
    let rows = statement.query_map([], |row| row.get::<_, String>(0))?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}
pub fn exists(con: &Connection, table: &str) -> Result<bool> {
    Ok(count(
        con,
        &format!(
            "SELECT count(*) FROM information_schema.tables WHERE table_name={}",
            lit(table)
        ),
    )? > 0)
}
pub fn columns(con: &Connection, relation: &str) -> Result<BTreeSet<String>> {
    let mut statement = con.prepare(&format!("DESCRIBE SELECT * FROM {}", ident(relation)))?;
    let rows = statement.query_map([], |row| row.get::<_, String>(0))?;
    Ok(rows.collect::<std::result::Result<BTreeSet<_>, _>>()?)
}
pub fn index_code(column: &str) -> String {
    format!("(({column} LIKE '000%.SS') OR ({column} LIKE '399%.SZ'))")
}
