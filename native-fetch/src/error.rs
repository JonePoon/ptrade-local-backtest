use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum FetchError {
    #[error("configuration: {0}")]
    Config(String),
    #[error("I/O: {0}")]
    Io(String),
    #[error("network: {0}")]
    Network(String),
    #[error("protocol: {0}")]
    Protocol(String),
    #[error("integrity: {0}")]
    Integrity(String),
    #[error("cancelled")]
    Cancelled,
}

impl FetchError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Config(_) => "config_error",
            Self::Io(_) => "io_error",
            Self::Network(_) => "network_error",
            Self::Protocol(_) => "protocol_error",
            Self::Integrity(_) => "integrity_error",
            Self::Cancelled => "cancelled",
        }
    }
    pub fn exit_code(&self) -> i32 {
        match self {
            Self::Config(_) => 2,
            Self::Cancelled => 130,
            _ => 3,
        }
    }
    pub fn detail(&self, job: Option<String>) -> ErrorDetail {
        ErrorDetail {
            code: self.code().into(),
            message: self.to_string(),
            job,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ErrorDetail {
    pub code: String,
    pub message: String,
    pub job: Option<String>,
}

pub type Result<T> = std::result::Result<T, FetchError>;
