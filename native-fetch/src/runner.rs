use crate::config::{Config, Dataset, Symbol};
use crate::data::{Bar, in_session, normalize};
use crate::error::{ErrorDetail, FetchError, Result};
use crate::lake::{
    FileInfo, Lake, atomic_json, discover_symbols, load_manifest, scope_id, verify_scope,
};
use crate::tdx::{Client, Metrics, RateLimiter, WireExchange};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::Ordering};
use std::time::{Duration, Instant};
use tokio::task::JoinSet;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize)]
pub struct Job {
    pub dataset: Dataset,
    pub symbol: Symbol,
    pub scope: String,
}
#[derive(Debug, Serialize)]
pub struct Report {
    pub schema_version: u32,
    pub operation: String,
    pub status: String,
    pub counts: Value,
    pub coverage: Vec<Value>,
    pub errors: Vec<ErrorDetail>,
    pub warnings: Vec<Value>,
    pub files: Vec<FileInfo>,
    pub started_at: DateTime<Utc>,
    pub finished_at: DateTime<Utc>,
    pub timings: Value,
    pub capabilities: Value,
    pub wire: Vec<Value>,
    pub plan: Vec<Job>,
}
impl Report {
    pub fn new(operation: &str) -> Self {
        Self {
            schema_version: 1,
            operation: operation.into(),
            status: "succeeded".into(),
            counts: json!({}),
            coverage: Vec::new(),
            errors: Vec::new(),
            warnings: Vec::new(),
            files: Vec::new(),
            started_at: Utc::now(),
            finished_at: Utc::now(),
            timings: json!({}),
            capabilities: json!({}),
            wire: Vec::new(),
            plan: Vec::new(),
        }
    }
    pub fn exit_code(&self) -> i32 {
        match self.status.as_str() {
            "succeeded" => 0,
            "partial" => 4,
            "cancelled" => 130,
            _ => {
                if self.errors.iter().any(|e| e.code == "config_error") {
                    2
                } else {
                    3
                }
            }
        }
    }
    pub fn failed(operation: &str, error: FetchError) -> Self {
        let mut report = Self::new(operation);
        report.status = if matches!(error, FetchError::Cancelled) {
            "cancelled"
        } else {
            "failed"
        }
        .into();
        report.errors.push(error.detail(None));
        report
    }
}

pub fn doctor() -> Report {
    let mut report = Report::new("doctor");
    report.capabilities = json!({"native":true,"implementation":"rust","version":env!("CARGO_PKG_VERSION"),"python_subprocess":false,"provider":"tdx_protocol","datasets":["daily_bars","index_bars","minute_bars","minute_bars_5m"],"parquet":"57.0.0","tcp_header_bytes":16,"maximum_wire_body_bytes":65535,"maximum_page_rows":800,"timezone":{"bar_time":"naive Asia/Shanghai","fetched_at":"UTC"},"resume":"sha256_verified_manifest","immutable_shards":true,"writes_lake":false,"network_contacted":false});
    report.finished_at = Utc::now();
    report
}

pub fn jobs(cfg: &Config) -> Result<Vec<Job>> {
    let symbols = if cfg.symbols.is_empty() {
        discover_symbols(&cfg.lake)?
    } else {
        cfg.symbols
            .iter()
            .map(|v| Symbol::parse(v))
            .collect::<Result<Vec<_>>>()?
    };
    let mut out = Vec::new();
    for name in &cfg.datasets {
        let dataset = Dataset::parse(name)?;
        for symbol in &symbols {
            if dataset == Dataset::Daily && symbol.is_index
                || dataset == Dataset::Index && !symbol.is_index
            {
                continue;
            }
            // Index volume is not proven to be shares; the CNE minute dataset has
            // a shares contract. Do not mix an unverified index unit into it.
            if dataset.intraday() && symbol.is_index {
                continue;
            }
            out.push(Job {
                dataset,
                symbol: symbol.clone(),
                scope: scope_id(dataset, &symbol.symbol, cfg.start, cfg.end),
            });
        }
    }
    if out.is_empty() {
        return Err(FetchError::Config(
            "no supported symbol/dataset combinations (index intraday units are unverified)".into(),
        ));
    }
    Ok(out)
}

pub fn plan(cfg: &Config) -> Result<Report> {
    let mut report = Report::new("plan");
    let plan = jobs(cfg)?;
    let manifest = load_manifest(&cfg.lake)?;
    for job in &plan {
        let done = cfg.resume && !cfg.refresh && verify_scope(&cfg.lake, &manifest, &job.scope)?;
        report.coverage.push(json!({"scope":job.scope,"dataset":job.dataset.name(),"symbol":job.symbol.symbol,"requested_start":cfg.start,"requested_end":cfg.end,"resume_verified":done}));
    }
    report.counts = json!({"jobs_total":plan.len(),"network_requests":0,"writes_lake":false});
    report.plan = plan;
    report.finished_at = Utc::now();
    Ok(report)
}

#[derive(Debug)]
pub struct Download {
    pub job: Job,
    pub rows: Vec<Bar>,
    pub coverage: Value,
    pub wire: Vec<WireExchange>,
    pub error: Option<FetchError>,
    pub complete: bool,
    pub source_rows: u64,
    pub off_session: u64,
    pub duplicates: u64,
}

pub async fn download(
    cfg: Arc<Config>,
    job: Job,
    ordinal: usize,
    metrics: Arc<Metrics>,
    limiter: Arc<RateLimiter>,
) -> Download {
    let _flight = metrics.enter();
    let mut client: Option<Client> = None;
    let mut wire = Vec::new();
    let mut values: BTreeMap<NaiveDateTime, Bar> = BTreeMap::new();
    let mut source_rows = 0;
    let mut off_session = 0;
    let mut duplicates = 0;
    let mut prior_oldest: Option<NaiveDateTime> = None;
    let mut observed_oldest: Option<NaiveDateTime> = None;
    let mut observed_newest: Option<NaiveDateTime> = None;
    let mut error = None;
    let mut bracketed = false;
    let mut reason = "max_pages";
    let mut page_count = 0;
    for page in 0..cfg.max_pages {
        let start = (page * cfg.page_size) as u16;
        let mut result = None;
        for attempt in 0..=cfg.retries {
            if client.is_none() {
                let host = &cfg.tdx_hosts[(ordinal + page + attempt) % cfg.tdx_hosts.len()];
                match Client::connect(host, &cfg, limiter.clone(), metrics.clone()).await {
                    Ok(value) => client = Some(value),
                    Err(e) => error = Some(e),
                }
            }
            if let Some(connection) = client.as_mut() {
                match connection
                    .bars(
                        &job.symbol,
                        job.dataset.category(),
                        start,
                        cfg.page_size as u16,
                        job.dataset == Dataset::Index,
                    )
                    .await
                {
                    Ok(rows) => {
                        wire.append(&mut connection.exchanges);
                        result = Some(rows);
                        error = None;
                        break;
                    }
                    Err(e) => {
                        wire.append(&mut connection.exchanges);
                        error = Some(e);
                        client = None;
                    }
                }
            }
            if attempt < cfg.retries {
                metrics.retries.fetch_add(1, Ordering::Relaxed);
                tokio::time::sleep(Duration::from_millis(
                    cfg.retry_backoff_ms.saturating_mul(1u64 << attempt.min(8)),
                ))
                .await;
            }
        }
        let Some(rows) = result else {
            reason = "request_failed";
            break;
        };
        page_count += 1;
        source_rows += rows.len() as u64;
        if rows.is_empty() {
            reason = "end_of_history";
            break;
        }
        let oldest = rows.iter().map(|r| r.stamp).min().unwrap();
        let newest = rows.iter().map(|r| r.stamp).max().unwrap();
        observed_oldest = Some(observed_oldest.map_or(oldest, |old| old.min(oldest)));
        observed_newest = Some(observed_newest.map_or(newest, |old| old.max(newest)));
        if prior_oldest.is_some_and(|old| oldest >= old) {
            error = Some(FetchError::Protocol(
                "pagination did not move backwards; repeated/ignored start offset".into(),
            ));
            reason = "repeated_page";
            break;
        }
        prior_oldest = Some(oldest);
        let returned = rows.len();
        for raw in rows {
            if job.dataset.intraday()
                && raw.stamp.date() >= cfg.start
                && raw.stamp.date() <= cfg.end
                && !in_session(raw.stamp, job.dataset)
            {
                off_session += 1;
            }
            match normalize(raw, &job.symbol.symbol, job.dataset, cfg.start, cfg.end) {
                Ok(Some(row)) => {
                    if values.insert(row.stamp, row).is_some() {
                        duplicates += 1;
                    }
                }
                Ok(None) => {}
                Err(e) => {
                    error = Some(e);
                    reason = "invalid_row";
                    break;
                }
            }
        }
        if error.is_some() {
            break;
        }
        if oldest.date() < cfg.start || !job.dataset.intraday() && oldest.date() <= cfg.start {
            bracketed = true;
            reason = "window_start_reached";
            break;
        }
        if returned < cfg.page_size {
            reason = "end_of_history";
            break;
        }
    }
    let rows: Vec<Bar> = values.into_values().collect();
    let mut sessions: BTreeMap<NaiveDate, usize> = BTreeMap::new();
    for row in &rows {
        *sessions.entry(row.stamp.date()).or_default() += 1;
    }
    let sessions_complete = !job.dataset.intraday()
        || sessions
            .values()
            .all(|&count| count == job.dataset.expected_bars());
    let start_boundary = rows
        .first()
        .is_some_and(|row| row.stamp.date() == cfg.start);
    let end_boundary = rows.last().is_some_and(|row| row.stamp.date() == cfg.end);
    let complete = error.is_none()
        && bracketed
        && !rows.is_empty()
        && sessions_complete
        && start_boundary
        && end_boundary;
    if error.is_none() && bracketed && (!start_boundary || !end_boundary) {
        reason = "calendar_boundary_unverified";
    }
    let coverage = json!({"scope":job.scope,"dataset":job.dataset.name(),"symbol":job.symbol.symbol,"frequency":job.dataset.frequency(),"requested_start":cfg.start,"requested_end":cfg.end,"observed_start":rows.first().map(|r|r.stamp.date()),"observed_end":rows.last().map(|r|r.stamp.date()),"provider_oldest":observed_oldest,"provider_newest":observed_newest,"partial":!complete,"reason":reason,"pages":page_count,"source_rows":source_rows,"rows":rows.len(),"off_session_rows":off_session,"duplicates_removed":duplicates,"expected_bars_per_session":job.dataset.expected_bars(),"session_counts":sessions,"calendar_verified":false,"start_boundary_observed":start_boundary,"end_boundary_observed":end_boundary,"volume_unit":job.dataset.unit(),"data_version":job.dataset.version()});
    Download {
        job,
        rows,
        coverage,
        wire,
        error,
        complete,
        source_rows,
        off_session,
        duplicates,
    }
}

fn progress(
    path: Option<&Path>,
    metrics: &Metrics,
    completed: usize,
    total: usize,
    rows: u64,
    files: usize,
    phase: &str,
) -> Result<()> {
    if let Some(path) = path {
        atomic_json(
            path,
            &json!({"status":"running","phase":phase,"completed":completed,"total":total,"rows":rows,"files":files,"requests":metrics.requests.load(Ordering::Relaxed),"pages":metrics.pages.load(Ordering::Relaxed),"retries":metrics.retries.load(Ordering::Relaxed),"in_flight":metrics.in_flight.load(Ordering::SeqCst),"peak_in_flight":metrics.peak_in_flight.load(Ordering::SeqCst),"updated_at":Utc::now()}),
        )?;
    }
    Ok(())
}

pub async fn fetch(cfg: Config, progress_path: Option<PathBuf>) -> Report {
    let started = Instant::now();
    let mut report = Report::new("fetch");
    let plan = match jobs(&cfg) {
        Ok(value) => value,
        Err(e) => return Report::failed("fetch", e),
    };
    let mut lake = match Lake::open(&cfg.lake) {
        Ok(value) => value,
        Err(e) => return Report::failed("fetch", e),
    };
    let total = plan.len();
    report.plan = plan.clone();
    let run = Uuid::new_v4().to_string();
    let metrics = Arc::new(Metrics::default());
    let limiter = Arc::new(RateLimiter::new(cfg.requests_per_second));
    let cfg = Arc::new(cfg);
    let mut pending = Vec::new();
    let mut completed = 0;
    let mut skipped = 0;
    let mut succeeded = 0;
    let mut partial = 0;
    let mut failed = 0;
    let mut source_rows = 0;
    let mut off_session = 0;
    let mut duplicates = 0;
    let mut published_rows = 0;
    for job in plan {
        if cfg.resume && !cfg.refresh {
            match verify_scope(&lake.root, &lake.manifest, &job.scope) {
                Ok(true) => {
                    completed += 1;
                    skipped += 1;
                    let commit = &lake.manifest.completed[&job.scope];
                    report.coverage.push(json!({"scope":job.scope,"dataset":job.dataset.name(),"symbol":job.symbol.symbol,"requested_start":cfg.start,"requested_end":cfg.end,"resume_verified":true,"partial":false,"reason":"verified_resume","rows":commit.files.iter().filter_map(|p|lake.manifest.files.iter().find(|f|&f.path==p)).map(|f|f.rows).sum::<u64>()}));
                    continue;
                }
                Ok(false) => {}
                Err(e) => {
                    report.errors.push(e.detail(Some(job.scope)));
                    failed += 1;
                    completed += 1;
                    continue;
                }
            }
        }
        pending.push(job);
    }
    if report.errors.is_empty() {
        if let Err(e) = lake.save_watermarks() {
            report.errors.push(e.detail(None));
        }
    }
    let mut set = JoinSet::new();
    let mut next = 0;
    while next < pending.len() && set.len() < cfg.concurrency {
        let job = pending[next].clone();
        set.spawn(download(
            cfg.clone(),
            job,
            next,
            metrics.clone(),
            limiter.clone(),
        ));
        next += 1;
    }
    let mut ticker = tokio::time::interval(Duration::from_millis(500));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut cancelled = false;
    loop {
        if set.is_empty() {
            break;
        }
        tokio::select! {
            result=set.join_next()=>{
                let result=match result {Some(Ok(value))=>value,Some(Err(e))=>{failed+=1;completed+=1;report.errors.push(FetchError::Network(format!("worker failed: {e}")).detail(None));continue;},None=>break};
                completed+=1;source_rows+=result.source_rows;off_session+=result.off_session;duplicates+=result.duplicates;
                report.coverage.push(result.coverage.clone());
                if cfg.capture_wire {match lake.capture(&run,&result.job.scope,&result.wire){Ok(files)=>report.wire.extend(files),Err(e)=>report.errors.push(e.detail(Some(result.job.scope.clone())))}}
                if let Some(e)=result.error {failed+=1;report.errors.push(e.detail(Some(result.job.scope.clone())));}
                else if !result.complete {partial+=1;report.warnings.push(json!({"code":"partial_source_coverage","job":result.job.scope,"message":"requested history/session completeness not established; no partial shard or watermark is published"}));}
                else {let rows=result.rows.len()as u64;match lake.publish(result.job.dataset,&result.job.symbol.symbol,cfg.start,cfg.end,&result.rows,Utc::now(),result.coverage){Ok(files)=>{succeeded+=1;published_rows+=rows;report.files.extend(files);},Err(e)=>{failed+=1;report.errors.push(e.detail(Some(result.job.scope)));}}}
                if let Err(e)=progress(progress_path.as_deref(),&metrics,completed,total,published_rows,report.files.len(),"download_publish"){report.errors.push(e.detail(None));}
                while next<pending.len()&&set.len()<cfg.concurrency {let job=pending[next].clone();set.spawn(download(cfg.clone(),job,next,metrics.clone(),limiter.clone()));next+=1;}
            },
            _=ticker.tick()=>{if let Err(e)=progress(progress_path.as_deref(),&metrics,completed,total,published_rows,report.files.len(),"download"){report.errors.push(e.detail(None));}},
            _=tokio::signal::ctrl_c()=>{cancelled=true;set.abort_all();while set.join_next().await.is_some(){};break;}
        }
    }
    report.status = if cancelled {
        "cancelled"
    } else if !report.errors.is_empty() {
        "failed"
    } else if partial > 0 {
        "partial"
    } else {
        "succeeded"
    }
    .into();
    report.counts = json!({"jobs_total":total,"jobs_succeeded":succeeded,"jobs_skipped":skipped,"jobs_partial":partial,"jobs_failed":failed,"jobs_completed":completed,"rows":published_rows,"files":report.files.len(),"source_rows":source_rows,"off_session_rows":off_session,"duplicates_removed":duplicates,"requests":metrics.requests.load(Ordering::Relaxed),"pages":metrics.pages.load(Ordering::Relaxed),"retries":metrics.retries.load(Ordering::Relaxed),"bytes_received":metrics.bytes_received.load(Ordering::Relaxed),"in_flight":metrics.in_flight.load(Ordering::SeqCst),"peak_in_flight":metrics.peak_in_flight.load(Ordering::SeqCst)});
    report.finished_at = Utc::now();
    report.timings = json!({"elapsed_seconds":started.elapsed().as_secs_f64()});
    if let Some(path) = progress_path {
        if let Err(e) = atomic_json(
            &path,
            &json!({"status":report.status,"phase":"finished","completed":completed,"total":total,"rows":published_rows,"files":report.files.len(),"requests":metrics.requests.load(Ordering::Relaxed),"retries":metrics.retries.load(Ordering::Relaxed),"in_flight":0,"peak_in_flight":metrics.peak_in_flight.load(Ordering::SeqCst),"updated_at":report.finished_at}),
        ) {
            report.errors.push(e.detail(None));
            report.status = "failed".into();
        }
    }
    report
}
