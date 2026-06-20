//! Optional Rust PyO3 kernels for RSI / ATR Wilder smoothing.
//! Build: `maturin develop --release` from `native/apex_math/`.

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

fn wilder_rsi(values: &[f64], period: usize) -> Vec<f64> {
    let n = values.len();
    let mut out = vec![50.0_f64; n];
    if n < period + 1 || period == 0 {
        return out;
    }
    let mut avg_gain = 0.0_f64;
    let mut avg_loss = 0.0_f64;
    for i in 1..=period {
        let delta = values[i] - values[i - 1];
        if delta >= 0.0 {
            avg_gain += delta;
        } else {
            avg_loss -= delta;
        }
    }
    avg_gain /= period as f64;
    avg_loss /= period as f64;
    let p = period as f64;
    for i in period..n {
        let delta = values[i] - values[i - 1];
        let gain = if delta > 0.0 { delta } else { 0.0 };
        let loss = if delta < 0.0 { -delta } else { 0.0 };
        avg_gain = (avg_gain * (p - 1.0) + gain) / p;
        avg_loss = (avg_loss * (p - 1.0) + loss) / p;
        let rs = if avg_loss > 0.0 {
            avg_gain / avg_loss
        } else {
            f64::INFINITY
        };
        let rsi = 100.0 - (100.0 / (1.0 + rs));
        out[i] = rsi.clamp(15.0, 85.0);
    }
    out
}

fn wilder_atr(high: &[f64], low: &[f64], close: &[f64], period: usize) -> Vec<f64> {
    let n = close.len();
    let mut out = vec![0.0_f64; n];
    if n == 0 || period == 0 {
        return out;
    }
    let mut trs = vec![0.0_f64; n];
    trs[0] = (high[0] - low[0]).abs();
    for i in 1..n {
        let hl = (high[i] - low[i]).abs();
        let hc = (high[i] - close[i - 1]).abs();
        let lc = (low[i] - close[i - 1]).abs();
        trs[i] = hl.max(hc).max(lc);
    }
    if n < period {
        return out;
    }
    let mut atr = trs[..period].iter().sum::<f64>() / period as f64;
    out[period - 1] = atr;
    let p = period as f64;
    for i in period..n {
        atr = (atr * (p - 1.0) + trs[i]) / p;
        out[i] = atr;
    }
    out
}

#[pyfunction]
fn rsi_wilder<'py>(
    py: Python<'py>,
    close: PyReadonlyArray1<'py, f64>,
    period: usize,
) -> Bound<'py, PyArray1<f64>> {
    let slice = close.as_slice().unwrap();
    let out = wilder_rsi(slice, period.max(1));
    PyArray1::from_vec(py, out)
}

#[pyfunction]
fn atr_wilder<'py>(
    py: Python<'py>,
    high: PyReadonlyArray1<'py, f64>,
    low: PyReadonlyArray1<'py, f64>,
    close: PyReadonlyArray1<'py, f64>,
    period: usize,
) -> Bound<'py, PyArray1<f64>> {
    let h = high.as_slice().unwrap();
    let l = low.as_slice().unwrap();
    let c = close.as_slice().unwrap();
    let out = wilder_atr(h, l, c, period.max(1));
    PyArray1::from_vec(py, out)
}

#[pymodule]
fn apex_math(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rsi_wilder, m)?)?;
    m.add_function(wrap_pyfunction!(atr_wilder, m)?)?;
    Ok(())
}
