//! Optional PyO3 boundary; all persistent data is owned by the Rust index.

mod core;

use core::IndexCore;
use numpy::PyReadonlyArray1;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;

fn slot_query(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<i64> {
    match value.extract::<i64>() {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<PyOverflowError>(py) => {
            // Python integers may exceed int64. Saturation remains exact when
            // comparing against int32 slots, unlike narrowing/wrapping them.
            let integer = value.call_method0("__index__")?;
            if integer.lt(0)? {
                Ok(i64::MIN)
            } else {
                Ok(i64::MAX)
            }
        }
        Err(error) => Err(error),
    }
}

/// Immutable minute-row index with owned int32 slots and code ranges.
#[pyclass(frozen, module = "_ptrade_sim_native")]
struct MinuteIndex {
    inner: IndexCore,
}

#[pymethods]
impl MinuteIndex {
    #[new]
    #[pyo3(signature = (slot, starts, slot_count=None))]
    fn new(
        py: Python<'_>,
        slot: PyReadonlyArray1<'_, i32>,
        starts: HashMap<String, (isize, isize)>,
        slot_count: Option<usize>,
    ) -> PyResult<Self> {
        // ndarray iteration preserves logical order for strided/read-only input.
        // Copy before releasing the GIL: no mutable numpy storage is retained.
        let slots = slot
            .as_array()
            .iter()
            .copied()
            .collect::<Vec<_>>()
            .into_boxed_slice();
        let ranges = starts.into_iter().collect();
        let inner = py
            .allow_threads(move || IndexCore::new(slots, ranges, slot_count))
            .map_err(PyValueError::new_err)?;
        Ok(Self { inner })
    }

    /// Exact first row for a code/slot, or -1 when absent.
    fn row_of(&self, py: Python<'_>, code: &str, slot_idx: &Bound<'_, PyAny>) -> PyResult<isize> {
        Ok(self.inner.row_of(code, slot_query(py, slot_idx)?))
    }

    /// Half-open (start, stop) bounds; missing code yields (0, 0).
    fn rows_upto(
        &self,
        py: Python<'_>,
        code: &str,
        slot_idx: &Bound<'_, PyAny>,
        include: bool,
    ) -> PyResult<(usize, usize)> {
        Ok(self
            .inner
            .rows_upto(code, slot_query(py, slot_idx)?, include))
    }

    /// One exact row per requested code, preserving order and duplicates.
    fn rows_of(
        &self,
        py: Python<'_>,
        codes: Vec<String>,
        slot_idx: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<isize>> {
        let query = slot_query(py, slot_idx)?;
        // Batch work amortizes Python crossings and releases the GIL. Scalar
        // queries intentionally retain it: unlock/lock costs exceed two bounds.
        Ok(py.allow_threads(|| self.inner.rows_of(&codes, query)))
    }

    #[getter]
    fn nbytes(&self) -> usize {
        self.inner.nbytes()
    }

    #[getter]
    fn slot_count(&self) -> Option<usize> {
        self.inner.slot_count()
    }

    #[getter]
    fn nrows(&self) -> usize {
        self.inner.nrows()
    }

    #[getter]
    fn ncodes(&self) -> usize {
        self.inner.ncodes()
    }

    fn __len__(&self) -> usize {
        self.inner.nrows()
    }

    fn __repr__(&self) -> String {
        format!(
            "MinuteIndex(nrows={}, ncodes={}, nbytes={})",
            self.inner.nrows(),
            self.inner.ncodes(),
            self.inner.nbytes()
        )
    }
}

#[pymodule(gil_used = false)]
fn _ptrade_sim_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<MinuteIndex>()?;
    module.add("INDEX_API_VERSION", 1)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
