use crate::config::JobConfig;
use crate::model::{Finding, Report, Result};
use crate::source::{self, Sources};
use crate::sql;
use duckdb::Connection;

pub fn row_key(alias: &str, keys: &[&str]) -> String {
    format!(
        "concat_ws(chr(31),{})",
        keys.iter()
            .map(|k| format!("CAST({alias}.{} AS VARCHAR)", sql::ident(k)))
            .collect::<Vec<_>>()
            .join(",")
    )
}
fn make(con: &Connection, name: &str, select: &str) -> Result<()> {
    con.execute_batch(&format!(
        "CREATE OR REPLACE TEMP TABLE {} AS {select}",
        sql::ident(name)
    ))?;
    Ok(())
}
fn latest(
    con: &Connection,
    cfg: &JobConfig,
    sources: &Sources,
    name: &str,
    target: &str,
    keys: &str,
    report: &mut Report,
) -> Result<bool> {
    let Some(src) = sources.get(name) else {
        return Ok(false);
    };
    let symbol_scope = if src.columns.contains("symbol") {
        sql::symbol_filter(cfg, "__code")
    } else {
        "TRUE".into()
    };
    let time_scope = if src.columns.contains("trade_date") || src.columns.contains("as_of_date") {
        sql::bounds(cfg, "__date", true)
    } else {
        "TRUE".into()
    };
    let from = sql::ident(&source::frame(name));
    let predicate = format!("{symbol_scope} AND {time_scope}");
    let before = sql::count(
        con,
        &format!("SELECT count(*) FROM {from} WHERE {predicate}"),
    )?;
    make(con,target,&format!("SELECT * FROM {from} WHERE {predicate} QUALIFY row_number() OVER(PARTITION BY {keys} ORDER BY __fetch DESC,filename DESC,file_row_number DESC)=1"))?;
    let after = sql::count(con, &format!("SELECT count(*) FROM {}", sql::ident(target)))?;
    report.duplicates.insert(name.into(), before - after);
    Ok(true)
}
fn empty(con: &Connection, name: &str, fields: &str) -> Result<()> {
    make(con, name, &format!("SELECT {fields} WHERE FALSE"))
}
pub fn prepare(
    con: &Connection,
    cfg: &JobConfig,
    sources: &Sources,
    report: &mut Report,
) -> Result<Vec<String>> {
    let instruments = latest(
        con,
        cfg,
        sources,
        "instruments",
        "__instruments",
        "__code",
        report,
    )?;
    if !instruments {
        empty(con,"__instruments","NULL::VARCHAR __code,NULL::VARCHAR AS name,NULL::VARCHAR exchange,NULL::VARCHAR asset_type,NULL::DATE list_date,NULL::DATE delist_date,NULL::BIGINT __fetch")?;
    }
    let factors = latest(
        con,
        cfg,
        sources,
        "adj_factors",
        "__all_factors",
        "__code,__date,adjust_type",
        report,
    )?;
    if factors {
        make(
            con,
            "__factors",
            "SELECT * FROM __all_factors WHERE adjust_type='hfq'",
        )?;
    } else {
        empty(
            con,
            "__factors",
            "NULL::VARCHAR __code,NULL::VARCHAR __date,NULL::DOUBLE factor,NULL::BIGINT __fetch",
        )?;
    }
    let status = latest(
        con,
        cfg,
        sources,
        "trading_status",
        "__all_status",
        "__code,__date",
        report,
    )?;
    if status {
        let risk = sources["trading_status"].optional("risk_warning", "BOOLEAN");
        make(con,"__status",&format!("SELECT __code,__date,__fetch,status,is_trading,coalesce({risk},CASE WHEN status='risk_warning' THEN TRUE ELSE NULL END) risk_warning FROM __all_status"))?;
    } else {
        empty(con,"__status","NULL::VARCHAR __code,NULL::VARCHAR __date,NULL::BIGINT __fetch,NULL::VARCHAR status,NULL::BOOLEAN is_trading,NULL::BOOLEAN risk_warning")?;
    }
    let bars = latest(
        con,
        cfg,
        sources,
        "daily_bars",
        "__bars",
        "__code,__date",
        report,
    )?;
    let indices = latest(
        con,
        cfg,
        sources,
        "index_bars",
        "__indices",
        "__code,__date",
        report,
    )?;
    let minutes = latest(
        con,
        cfg,
        sources,
        "minute_bars",
        "__minutes",
        "__code,__date,bar_time,frequency",
        report,
    )?;
    let calendar = latest(
        con,
        cfg,
        sources,
        "trading_calendar",
        "__calendar",
        "__date",
        report,
    )?;
    let constituents = latest(
        con,
        cfg,
        sources,
        "index_constituents",
        "__constituents",
        "index_symbol,__code,__date",
        report,
    )?;
    let mut targets = vec![];

    if bars || cfg.wants("trading_status") || cfg.wants("adj_factors") {
        let mut branch = vec![];
        if bars {
            let src = &sources["daily_bars"];
            let raw_pre = src.optional_at("pre_close", "DOUBLE", "b");
            let bar_name = src.optional_at("name", "VARCHAR", "b");
            let bar_st = src.optional_at("is_st", "INTEGER", "b");
            let bar_delisted = src.optional_at("is_delisted", "INTEGER", "b");
            let volume = if cfg.require_volume_v2 {
                "CAST(b.volume AS DOUBLE)".to_owned()
            } else {
                "CAST(b.volume AS DOUBLE)*CASE WHEN b.data_version='v1' AND b.source IN ('tdx_protocol','sina') THEN 100 ELSE 1 END".into()
            };
            let eligible = if cfg.wants("daily_bars") {
                "TRUE".to_owned()
            } else {
                format!("d.code IS NOT NULL AND (({} AND f.__code IS NOT NULL) OR ({} AND st.__code IS NOT NULL))",cfg.wants("adj_factors"),cfg.wants("trading_status"))
            };
            // A later metadata publication must not let an older bar publication
            // overwrite a newer stored close. Keep component revision clocks.
            let use_bar = "b.__fetch>=coalesce(m.bar_fetched_at,0)";
            let use_factor = "f.__fetch>=coalesce(m.factor_fetched_at,0)";
            let use_status = "st.__fetch>=coalesce(m.status_fetched_at,0)";
            branch.push(format!(r#"SELECT b.__code code,b.__date date,
                CASE WHEN {use_bar} THEN b.open ELSE d.open END open,
                CASE WHEN {use_bar} THEN b.high ELSE d.high END high,
                CASE WHEN {use_bar} THEN b.low ELSE d.low END low,
                CASE WHEN {use_bar} THEN b.close ELSE d.close END AS close,
                CASE WHEN {use_bar} THEN {volume} ELSE d.volume END volume,
                CASE WHEN {use_bar} THEN b.amount ELSE d.money END money,
                CASE WHEN {use_bar} THEN {raw_pre} ELSE d.preclose END raw_preclose,
                coalesce(CASE WHEN {use_factor} THEN f.factor END,d.adj_factor) adj_factor,
                d.preclose old_preclose,coalesce(CASE WHEN {use_bar} THEN {bar_name} END,d.name) AS name,
                coalesce(CASE WHEN {use_status} THEN CAST(st.risk_warning AS INTEGER) END,CASE WHEN {use_bar} THEN {bar_st} END,d.is_st) is_st,
                coalesce(CASE WHEN {use_status} AND st.status IS NOT NULL THEN CASE WHEN st.status='delisted' THEN 1 ELSE 0 END END,
                    CASE WHEN {use_bar} THEN {bar_delisted} END,d.is_delisted,
                    CASE WHEN i.delist_date IS NOT NULL THEN CASE WHEN CAST(b.__date AS VARCHAR)>=strftime(i.delist_date,'%Y%m%d') THEN 1 ELSE 0 END END) is_delisted,
                greatest(b.__fetch,coalesce(m.bar_fetched_at,0)) _bar_fetch,
                greatest(coalesce(f.__fetch,0),coalesce(m.factor_fetched_at,0)) _factor_fetch,
                greatest(coalesce(st.__fetch,0),coalesce(m.status_fetched_at,0)) _status_fetch,
                {eligible} eligible, b.__fetch<coalesce(m.bar_fetched_at,0) _stale
                FROM __bars b LEFT JOIN __old_ashare_1d_stock d ON b.__code=d.code AND b.__date=d.date
                LEFT JOIN __old_rowmeta m ON m.table_name='ashare_1d_stock' AND m.row_key=concat_ws(chr(31),b.__code,b.__date)
                LEFT JOIN __factors f ON f.__code=b.__code AND f.__date=b.__date
                LEFT JOIN __status st ON st.__code=b.__code AND st.__date=b.__date
                LEFT JOIN __instruments i ON i.__code=b.__code"#));
        }
        let anti = if bars {
            "AND NOT EXISTS(SELECT 1 FROM __bars b WHERE b.__code=d.code AND b.__date=d.date)"
        } else {
            ""
        };
        let eligible = format!(
            "(({} AND f.__code IS NOT NULL) OR ({} AND st.__code IS NOT NULL))",
            cfg.wants("adj_factors"),
            cfg.wants("trading_status")
        );
        branch.push(format!(r#"SELECT d.code,d.date,d.open,d.high,d.low,d.close,d.volume,d.money,
            NULL::DOUBLE raw_preclose,coalesce(CASE WHEN f.__fetch>=coalesce(m.factor_fetched_at,0) THEN f.factor END,d.adj_factor) adj_factor,
            d.preclose old_preclose,d.name,
            coalesce(CASE WHEN st.__fetch>=coalesce(m.status_fetched_at,0) THEN CAST(st.risk_warning AS INTEGER) END,d.is_st) is_st,
            coalesce(CASE WHEN st.__fetch>=coalesce(m.status_fetched_at,0) AND st.status IS NOT NULL THEN CASE WHEN st.status='delisted' THEN 1 ELSE 0 END END,d.is_delisted) is_delisted,
            coalesce(m.bar_fetched_at,0) _bar_fetch,greatest(coalesce(f.__fetch,0),coalesce(m.factor_fetched_at,0)) _factor_fetch,
            greatest(coalesce(st.__fetch,0),coalesce(m.status_fetched_at,0)) _status_fetch,{eligible} eligible,FALSE _stale
            FROM __old_ashare_1d_stock d LEFT JOIN __old_rowmeta m ON m.table_name='ashare_1d_stock' AND m.row_key=concat_ws(chr(31),d.code,d.date)
            LEFT JOIN __factors f ON f.__code=d.code AND f.__date=d.date LEFT JOIN __status st ON st.__code=d.code AND st.__date=d.date
            WHERE {} AND {} {anti}"#,sql::bounds(cfg,"d.date",true),sql::symbol_filter(cfg,"d.code")));
        make(con, "__price_seed", &branch.join(" UNION ALL "))?;
        // Apply the requested lower bound AFTER the lag. Previous-day anchors
        // include both old target rows and lake history preceding the window.
        make(
            con,
            "__price_ordered",
            r#"SELECT *,lag(close) OVER(PARTITION BY code ORDER BY date) prior_close,
            lag(adj_factor) OVER(PARTITION BY code ORDER BY date) prior_factor FROM __price_seed"#,
        )?;
        make(
            con,
            "__price_resolved",
            r#"SELECT *,coalesce(raw_preclose,
            CASE WHEN prior_close IS NOT NULL AND prior_factor>0 AND adj_factor>0 THEN prior_close*prior_factor/adj_factor END,
            old_preclose,CASE WHEN prior_close IS NOT NULL AND prior_factor IS NULL AND adj_factor IS NULL THEN prior_close END) resolved_preclose
            FROM __price_ordered"#,
        )?;
        make(
            con,
            "__stage_ashare_1d_stock",
            &format!(
                r#"SELECT code,date,open,high,low,close,volume,money,resolved_preclose preclose,
            adj_factor,CASE WHEN volume>0 THEN money/volume ELSE NULL END vwap,name,is_st,is_delisted,
            close-resolved_preclose change,CASE WHEN resolved_preclose>0 THEN (close-resolved_preclose)/resolved_preclose*100 ELSE NULL END pct_chg,
            _bar_fetch,_factor_fetch,_status_fetch,greatest(_bar_fetch,_factor_fetch,_status_fetch) _version,_stale
            FROM __price_resolved WHERE eligible AND {}"#,
                sql::bounds(cfg, "date", false)
            ),
        )?;
        targets.push("ashare_1d_stock".into());
        for (column, kind) in [
            ("adj_factor", "unknown_hfq_factor"),
            ("preclose", "unknown_previous_close"),
            ("is_st", "unknown_historical_risk_warning"),
            ("is_delisted", "unknown_historical_delisting"),
            ("name", "unknown_historical_name"),
        ] {
            let rows = sql::count(
                con,
                &format!(
                    "SELECT count(*) FROM __stage_ashare_1d_stock WHERE {} IS NULL",
                    sql::ident(column)
                ),
            )?;
            if rows > 0 {
                report.unknown(Finding::new(
                    kind,
                    "no fabricated replacement; existing per-day facts retained when available",
                    Some("daily_bars"),
                    Some("ashare_1d_stock"),
                    Some(rows),
                ));
            }
        }
    }
    if indices && cfg.wants("index_bars") {
        let pre = sources["index_bars"].optional_at("pre_close", "DOUBLE", "b");
        make(
            con,
            "__index_seed",
            &format!(
                r#"SELECT b.__code code,b.__date date,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.open ELSE d.open END open,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.high ELSE d.high END high,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.low ELSE d.low END low,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.close ELSE d.close END AS close,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.volume ELSE d.volume END volume,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN b.amount ELSE d.money END money,
            CASE WHEN b.__fetch>=coalesce(m.bar_fetched_at,0) THEN {pre} ELSE d.preclose END raw_preclose,d.preclose old_preclose,TRUE eligible,
            greatest(b.__fetch,coalesce(m.bar_fetched_at,0)) _bar_fetch,b.__fetch<coalesce(m.bar_fetched_at,0) _stale
            FROM __indices b LEFT JOIN __old_ashare_1d_index d ON d.code=b.__code AND d.date=b.__date
            LEFT JOIN __old_rowmeta m ON m.table_name='ashare_1d_index' AND m.row_key=concat_ws(chr(31),b.__code,b.__date)
            UNION ALL SELECT d.code,d.date,d.open,d.high,d.low,d.close,d.volume,d.money,NULL::DOUBLE,d.preclose,FALSE,
            coalesce(m.bar_fetched_at,0),FALSE FROM __old_ashare_1d_index d
            LEFT JOIN __old_rowmeta m ON m.table_name='ashare_1d_index' AND m.row_key=concat_ws(chr(31),d.code,d.date)
            WHERE {} AND {} AND NOT EXISTS(SELECT 1 FROM __indices b WHERE b.__code=d.code AND b.__date=d.date)"#,
                sql::bounds(cfg, "d.date", true),
                sql::symbol_filter(cfg, "d.code")
            ),
        )?;
        make(con,"__index_ordered","SELECT *,lag(close) OVER(PARTITION BY code ORDER BY date) prior_close FROM __index_seed")?;
        make(
            con,
            "__stage_ashare_1d_index",
            &format!(
                r#"SELECT code,date,open,high,low,close,volume,money,
            coalesce(raw_preclose,prior_close,old_preclose) preclose,
            close-coalesce(raw_preclose,prior_close,old_preclose) change,
            CASE WHEN coalesce(raw_preclose,prior_close,old_preclose)>0 THEN (close/coalesce(raw_preclose,prior_close,old_preclose)-1)*100 END pct_chg,
            _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,_bar_fetch _version,_stale
            FROM __index_ordered WHERE eligible AND {}"#,
                sql::bounds(cfg, "date", false)
            ),
        )?;
        targets.push("ashare_1d_index".into());
        report.unknown(Finding::new("index_volume_unit","index provider quantity unit not independently established; raw value preserved, never multiplied by 100",Some("index_bars"),Some("ashare_1d_index"),None));
    }
    if minutes && cfg.wants("minute_bars") {
        let index = sql::index_code("b.__code");
        let daily = if sql::exists(con, "__stage_ashare_1d_stock")? {
            "__stage_ashare_1d_stock"
        } else {
            "__old_ashare_1d_stock"
        };
        make(
            con,
            "__minute_seed",
            &format!(
                r#"SELECT b.*,({index} OR coalesce(i.asset_type='index',FALSE)) __is_index,
            strftime(TRY_CAST(b.bar_time AS TIMESTAMP),'%Y-%m-%d %H:%M:%S') __time,d.preclose __preclose
            FROM __minutes b LEFT JOIN __instruments i ON i.__code=b.__code LEFT JOIN {daily} d ON d.code=b.__code AND d.date=b.__date
            WHERE {}"#,
                sql::bounds(cfg, "b.__date", false)
            ),
        )?;
        for (name, is_index) in [("ashare_1m_stock", false), ("ashare_1m_index", true)] {
            let extension = if is_index {
                String::new()
            } else {
                "b.__preclose preclose,b.close-b.__preclose change,CASE WHEN b.__preclose>0 THEN (b.close/b.__preclose-1)*100 END pct_chg,".into()
            };
            make(
                con,
                &format!("__stage_{name}"),
                &format!(
                    r#"SELECT b.__code code,b.__date date,b.__time trade_time,b.open,b.high,b.low,b.close,b.volume,b.amount money,
                {extension} b.__fetch _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,b.__fetch _version,FALSE _stale
                FROM __minute_seed b WHERE b.__is_index={is_index}"#
                ),
            )?;
            targets.push(name.into());
        }
    }
    if instruments && cfg.wants("instruments") {
        let src = &sources["instruments"];
        let list = src.optional("list_date", "DATE");
        let delist = src.optional("delist_date", "DATE");
        make(
            con,
            "__stage_ashare_stock_basic",
            &format!(
                r#"SELECT __code code,symbol,name,NULL::VARCHAR area,NULL::VARCHAR industry,
            NULL::VARCHAR fullname,NULL::VARCHAR enname,NULL::VARCHAR cnspell,
            CASE WHEN asset_type!='stock' THEN '基金' WHEN __code LIKE '68%.SS' THEN '科创板' WHEN __code LIKE '30%.SZ' THEN '创业板'
                WHEN __code LIKE '60%.SS' OR __code LIKE '00%.SZ' THEN '主板' ELSE '北交所' END market,
            exchange,'CNY' curr_type,CASE WHEN {delist} IS NOT NULL THEN 'D' ELSE 'L' END list_status,
            strftime({list},'%Y%m%d') list_date,strftime({delist},'%Y%m%d') delist_date,
            NULL::VARCHAR is_hs,NULL::VARCHAR act_name,NULL::VARCHAR act_ent_type,
            __fetch _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,__fetch _version,FALSE _stale
            FROM __instruments WHERE asset_type IN ('stock','etf','fund')"#
            ),
        )?;
        make(
            con,
            "__stage_ashare_index_info_basic",
            &format!(
                r#"SELECT __code ts_code,name,exchange market,NULL::VARCHAR publisher,NULL::VARCHAR category,
            NULL::VARCHAR base_date,NULL::DOUBLE base_point,strftime({list},'%Y%m%d') list_date,
            __fetch _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,__fetch _version,FALSE _stale FROM __instruments WHERE asset_type='index'"#
            ),
        )?;
        targets.extend([
            "ashare_stock_basic".into(),
            "ashare_index_info_basic".into(),
        ]);
    }
    if calendar && cfg.wants("trading_calendar") {
        make(con,"__stage_ashare_calendar",&format!("SELECT __date date,is_trading _active,__fetch _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,__fetch _version,FALSE _stale FROM __calendar WHERE {}",sql::bounds(cfg,"__date",false)))?;
        targets.push("ashare_calendar".into());
    }
    if constituents && cfg.wants("index_constituents") {
        make(
            con,
            "__stage_ashare_index_weight",
            &format!(
                r#"SELECT {} index_code,NULL::VARCHAR index_name,__code code,__date in_date,
            NULL::VARCHAR out_date,weight,'cne:'||source AS source,__date snapshot_date,
            __fetch _bar_fetch,0::BIGINT _factor_fetch,0::BIGINT _status_fetch,__fetch _version,FALSE _stale
            FROM __constituents WHERE {}"#,
                sql::code("index_symbol"),
                sql::bounds(cfg, "__date", false)
            ),
        )?;
        targets.push("ashare_index_weight".into());
        report.unknown(Finding::new("snapshot_membership","as_of_date remains snapshot_date; no invented historical membership intervals or removals",Some("index_constituents"),Some("ashare_index_weight"),None));
    }
    if cfg.wants("minute_bars_5m") {
        report.unknown(Finding::new(
            "frequency_not_in_physical_contract",
            "5m Parquet observed separately; not inserted into 1m tables",
            Some("minute_bars_5m"),
            None,
            None,
        ));
    }
    Ok(targets)
}
