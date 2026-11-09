use crate::config::JobConfig;
use crate::model::{Finding, Report, Result, StoreError, Validation};
use crate::source::Sources;
use crate::{sql, tables};
use duckdb::Connection;

fn check(
    con: &Connection,
    report: &mut Report,
    table: &str,
    name: &str,
    predicate: &str,
    scope: &str,
) -> Result<()> {
    let rows = sql::count(
        con,
        &format!(
            "SELECT count(*) FROM {} WHERE ({scope}) AND ({predicate})",
            sql::ident(table)
        ),
    )?;
    report.validation.push(Validation {
        check: name.into(),
        table: table.into(),
        rows,
        passed: rows == 0,
    });
    if rows > 0 {
        return Err(StoreError::data(format!("{name}: {rows} invalid rows")).table(table));
    }
    Ok(())
}
fn prices(
    con: &Connection,
    cfg: &JobConfig,
    report: &mut Report,
    table: &str,
    money: &str,
    scope: &str,
    units: bool,
) -> Result<()> {
    check(con,report,table,"finite_positive_ohlc","open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR NOT isfinite(open) OR NOT isfinite(high) OR NOT isfinite(low) OR NOT isfinite(close) OR open<=0 OR high<=0 OR low<=0 OR close<=0",scope)?;
    check(
        con,
        report,
        table,
        "ohlc_bounds",
        "high<low OR high<open OR high<close OR low>open OR low>close",
        scope,
    )?;
    check(con,report,table,"finite_nonnegative_volume_turnover",&format!("volume IS NULL OR NOT isfinite(CAST(volume AS DOUBLE)) OR volume<0 OR {money} IS NULL OR NOT isfinite({money}) OR {money}<0"),scope)?;
    if units {
        check(
            con,
            report,
            table,
            "traded_quantity_turnover_consistency",
            &format!("(volume>0 AND {money}<=0) OR (volume=0 AND {money}>0)"),
            scope,
        )?;
        check(
            con,
            report,
            table,
            "share_yuan_unit",
            &format!(
                "volume>0 AND ({money}/volume>high*{} OR {money}/volume<low/{})",
                cfg.unit_fault_factor, cfg.unit_fault_factor
            ),
            scope,
        )?;
    }
    Ok(())
}
pub fn sources(
    con: &Connection,
    cfg: &JobConfig,
    source: &Sources,
    report: &mut Report,
) -> Result<()> {
    if source.contains_key("daily_bars") {
        let scope = sql::bounds(cfg, "__date", false);
        if cfg.require_volume_v2 {
            check(
                con,
                report,
                "__bars",
                "required_volume_v2",
                "data_version IS DISTINCT FROM 'v2'",
                &scope,
            )?;
        } else {
            check(con,report,"__bars","known_legacy_volume_semantics","data_version IS DISTINCT FROM 'v2' AND NOT (data_version='v1' AND source IN ('tdx_protocol','sina','ths','baostock'))",&scope)?;
        }
        // Validate the normalized unit, not the raw v1 lot count.
        let volume = if cfg.require_volume_v2 {
            "CAST(volume AS DOUBLE)".into()
        } else {
            "CAST(volume AS DOUBLE)*CASE WHEN data_version='v1' AND source IN ('tdx_protocol','sina') THEN 100 ELSE 1 END".to_owned()
        };
        con.execute_batch(&format!("CREATE OR REPLACE TEMP VIEW __bars_unit_check AS SELECT * EXCLUDE(volume),{volume} volume FROM __bars"))?;
        prices(
            con,
            cfg,
            report,
            "__bars_unit_check",
            "amount",
            &scope,
            true,
        )?;
        let pre = source["daily_bars"].optional("pre_close", "DOUBLE");
        check(
            con,
            report,
            "__bars",
            "source_previous_close",
            &format!("{pre} IS NOT NULL AND (NOT isfinite({pre}) OR {pre}<=0)"),
            &scope,
        )?;
    }
    if source.contains_key("index_bars") {
        prices(
            con,
            cfg,
            report,
            "__indices",
            "amount",
            &sql::bounds(cfg, "__date", false),
            false,
        )?;
    }
    if source.contains_key("minute_bars") {
        let scope = sql::bounds(cfg, "__date", false);
        prices(con, cfg, report, "__minutes", "amount", &scope, true)?;
        check(con,report,"__minutes","minute_real_day_matches_timestamp","bar_time IS NULL OR CAST(bar_time AS DATE) IS DISTINCT FROM CAST(TRY_STRPTIME(__date,'%Y%m%d') AS DATE)",&scope)?;
        check(con,report,"__minutes","minute_session_no_padding","date_trunc('minute',bar_time) IS DISTINCT FROM bar_time OR NOT ((CAST(bar_time AS TIME)>='09:30:00'::TIME AND CAST(bar_time AS TIME)<='11:30:00'::TIME) OR (CAST(bar_time AS TIME)>='13:01:00'::TIME AND CAST(bar_time AS TIME)<='15:00:00'::TIME))",&scope)?;
    }
    if source.contains_key("adj_factors") {
        check(
            con,
            report,
            "__all_factors",
            "finite_positive_factor",
            "factor IS NULL OR NOT isfinite(factor) OR factor<=0",
            &sql::bounds(cfg, "__date", false),
        )?;
    }
    if source.contains_key("trading_status") {
        check(con,report,"__all_status","historical_status_enum","status IS NULL OR status NOT IN ('normal','suspended','delisted','risk_warning') OR is_trading IS NULL",&sql::bounds(cfg,"__date",false))?;
    }
    if source.contains_key("trading_calendar") {
        check(
            con,
            report,
            "__calendar",
            "calendar_boolean",
            "is_trading IS NULL",
            &sql::bounds(cfg, "__date", false),
        )?;
    }
    if source.contains_key("index_constituents") {
        check(
            con,
            report,
            "__constituents",
            "finite_weight",
            "weight IS NOT NULL AND (NOT isfinite(weight) OR weight<0 OR weight>100)",
            &sql::bounds(cfg, "__date", false),
        )?;
    }
    Ok(())
}
pub fn table(
    con: &Connection,
    cfg: &JobConfig,
    report: &mut Report,
    name: &str,
    physical: bool,
) -> Result<()> {
    let spec = tables::spec(name);
    let relation = if physical {
        name.to_owned()
    } else {
        format!("__stage_{name}")
    };
    let columns = sql::columns(con, &relation)?;
    for (column, _) in spec.columns {
        if !columns.contains(*column) {
            return Err(
                StoreError::schema(format!("missing contract column {column}")).table(name),
            );
        }
    }
    let date_column = if columns.contains("date") {
        Some("date")
    } else if columns.contains("in_date") {
        Some("in_date")
    } else {
        None
    };
    let scope = if let Some(date) = date_column {
        sql::bounds(cfg, &sql::date8(&sql::ident(date)), false)
    } else {
        "TRUE".into()
    };
    let symbol_column = if columns.contains("code") {
        Some("code")
    } else if columns.contains("ts_code") {
        Some("ts_code")
    } else {
        None
    };
    let symbol_scope = if let Some(column) = symbol_column {
        sql::symbol_filter(cfg, &sql::code(&sql::ident(column)))
    } else {
        "TRUE".into()
    };
    let scoped = format!("{scope} AND {symbol_scope}");
    let null_key = spec
        .keys
        .iter()
        .map(|k| format!("{} IS NULL", sql::ident(k)))
        .collect::<Vec<_>>()
        .join(" OR ");
    check(con, report, &relation, "non_null_key", &null_key, "TRUE")?;
    if let Some(date) = date_column {
        check(
            con,
            report,
            &relation,
            "actual_valid_yyyymmdd_date",
            &format!(
                "{} IS NULL OR NOT regexp_full_match(CAST({date} AS VARCHAR),'[0-9]{{8}}')",
                sql::day(&sql::ident(date))
            ),
            &symbol_scope,
        )?;
    }
    if let Some(symbol) = symbol_column {
        check(
            con,
            report,
            &relation,
            "canonical_symbol",
            &format!(
                "NOT regexp_full_match({},'[0-9]{{6}}\\.(SS|SZ|BJ)')",
                sql::code(&sql::ident(symbol))
            ),
            "TRUE",
        )?;
    }
    let key = spec
        .keys
        .iter()
        .map(|k| {
            if matches!(*k, "code" | "index_code" | "ts_code") {
                sql::code(&sql::ident(k))
            } else {
                sql::ident(k)
            }
        })
        .collect::<Vec<_>>()
        .join(",");
    let duplicates=sql::count(con,&format!("SELECT count(*) FROM (SELECT {key} FROM {} WHERE {scoped} GROUP BY {key} HAVING count(*)>1)",sql::ident(&relation)))?;
    report.validation.push(Validation {
        check: "unique_business_key".into(),
        table: relation.clone(),
        rows: duplicates,
        passed: duplicates == 0,
    });
    if duplicates > 0 {
        return Err(StoreError::data(format!(
            "{duplicates} duplicate keys; never clean history to hide them"
        ))
        .table(name));
    }
    if name.starts_with("ashare_1d") {
        check(
            con,
            report,
            &relation,
            "required_previous_close_anchor",
            "preclose IS NULL",
            &scoped,
        )?;
        if name == "ashare_1d_stock" {
            check(
                con,
                report,
                &relation,
                "required_hfq_anchor",
                "adj_factor IS NULL",
                &scoped,
            )?;
        }
    }
    if name.starts_with("ashare_1") {
        prices(
            con,
            cfg,
            report,
            &relation,
            "money",
            &scoped,
            name.ends_with("stock"),
        )?;
        if columns.contains("preclose") {
            check(
                con,
                report,
                &relation,
                "finite_previous_close",
                "preclose IS NOT NULL AND (NOT isfinite(preclose) OR preclose<=0)",
                &scoped,
            )?;
            check(con,report,&relation,"change_self_consistency","preclose IS NOT NULL AND change IS NOT NULL AND (NOT isfinite(change) OR abs(change-(close-preclose))>0.00001)",&scoped)?;
            check(con,report,&relation,"percent_self_consistency","preclose>0 AND pct_chg IS NOT NULL AND (NOT isfinite(pct_chg) OR abs(pct_chg-(close/preclose-1)*100)>0.0001)",&scoped)?;
        }
        if columns.contains("adj_factor") {
            check(con,report,&relation,"finite_hfq_factor","adj_factor IS NOT NULL AND (NOT isfinite(adj_factor) OR adj_factor<=0 OR NOT isfinite(close*adj_factor))",&scoped)?;
        }
        if columns.contains("trade_time") {
            check(con,report,&relation,"minute_date_timestamp_match","TRY_CAST(trade_time AS TIMESTAMP) IS NULL OR strftime(TRY_CAST(trade_time AS TIMESTAMP),'%Y%m%d') IS DISTINCT FROM date",&scoped)?;
            check(con,report,&relation,"minute_physical_session","NOT ((CAST(TRY_CAST(trade_time AS TIMESTAMP) AS TIME)>='09:30:00'::TIME AND CAST(TRY_CAST(trade_time AS TIMESTAMP) AS TIME)<='11:30:00'::TIME) OR (CAST(TRY_CAST(trade_time AS TIMESTAMP) AS TIME)>='13:01:00'::TIME AND CAST(TRY_CAST(trade_time AS TIMESTAMP) AS TIME)<='15:00:00'::TIME))",&scoped)?;
        }
        for column in ["is_st", "is_delisted"] {
            if columns.contains(column) {
                check(
                    con,
                    report,
                    &relation,
                    "historical_boolean",
                    &format!("{column} IS NOT NULL AND {column} NOT IN (0,1)"),
                    &scoped,
                )?;
            }
        }
    }
    Ok(())
}
pub fn all_physical(
    con: &Connection,
    cfg: &JobConfig,
    report: &mut Report,
    require_core: bool,
) -> Result<()> {
    for spec in tables::TABLES {
        if sql::exists(con, spec.name)? {
            table(con, cfg, report, spec.name, true)?;
        } else if require_core && spec.required {
            return Err(StoreError::schema("missing required physical table").table(spec.name));
        }
    }
    Ok(())
}
pub fn coverage(con: &Connection, cfg: &JobConfig, report: &mut Report) -> Result<()> {
    for spec in tables::TABLES {
        if !sql::exists(con, spec.name)? || !spec.columns.iter().any(|(c, _)| *c == "date") {
            continue;
        }
        let scope = sql::bounds(cfg, "date", false);
        let symbol = if spec.columns.iter().any(|(c, _)| *c == "code") {
            sql::symbol_filter(cfg, "code")
        } else {
            "TRUE".into()
        };
        let bounds=sql::texts(con,&format!("SELECT coalesce(min(date),'')||'|'||coalesce(max(date),'') FROM {} WHERE {scope} AND {symbol}",sql::ident(spec.name)))?.into_iter().next().unwrap_or_default();
        let (min, max) = bounds.split_once('|').unwrap_or(("", ""));
        let rows = sql::count(
            con,
            &format!(
                "SELECT count(*) FROM {} WHERE {scope} AND {symbol}",
                sql::ident(spec.name)
            ),
        )?;
        report.coverage.push(serde_json::json!({"table":spec.name,"rows":rows,"min_date":min,"max_date":max,"status":if rows==0&&spec.required{"partial"}else{"observed"}}));
        if spec.name.contains("1m") && rows > 0 {
            let groups=sql::texts(con,&format!("SELECT CAST(to_json(struct_pack(code:=code,date:=date,rows:=count(*),first_bar:=min(trade_time),last_bar:=max(trade_time))) AS VARCHAR) FROM {} WHERE {scope} AND {symbol} GROUP BY code,date ORDER BY code,date",sql::ident(spec.name)))?;
            for group in groups {
                let value: serde_json::Value = serde_json::from_str(&group)?;
                let n = value["rows"].as_u64().unwrap_or(0);
                if n < 240 {
                    report.unknown(Finding::new(
                        "partial_minute_session",
                        format!(
                            "{} {} has {n} observed bars; no auction/padding bars synthesized",
                            value["code"], value["date"]
                        ),
                        None,
                        Some(spec.name),
                        Some(n),
                    ));
                }
                report.coverage.push(value);
            }
        }
    }
    Ok(())
}
