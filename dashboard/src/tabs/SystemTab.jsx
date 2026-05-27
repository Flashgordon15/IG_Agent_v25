import { useEffect, useState } from "react";
import { api } from "../api/client";

export default function SystemTab({ tick, reconnecting }) {
  const [sys, setSys] = useState(null);
  const [tests, setTests] = useState(null);
  const [busy, setBusy] = useState("");
  const [emergConfirm, setEmergConfirm] = useState(false);

  useEffect(() => {
    api.system().then(setSys).catch(() => {});
  }, []);

  const run = async (action, fn) => {
    setBusy(action);
    try {
      await fn();
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="p-4 max-w-3xl mx-auto space-y-4">
      <div className="card">
        <p className="label-caps mb-3">Agent controls</p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={!!busy}
            className="px-4 py-2 rounded bg-green text-bg font-medium"
            onClick={() => run("start", api.start)}
          >
            Start DEMO
          </button>
          <button
            type="button"
            disabled={!!busy}
            className="px-4 py-2 rounded border border-amber text-amber"
            onClick={() => run("stop", api.stop)}
          >
            Stop
          </button>
          <button
            type="button"
            disabled={!!busy}
            className="px-4 py-2 rounded border border-red text-red"
            onClick={() => {
              if (!emergConfirm) {
                setEmergConfirm(true);
                return;
              }
              run("emergency", api.emergencyStop);
              setEmergConfirm(false);
            }}
          >
            {emergConfirm ? "Confirm emergency stop" : "Emergency Stop"}
          </button>
        </div>
      </div>

      <div className="card">
        <p className="label-caps mb-2">Stream</p>
        <p className="text-[12px] text-muted">
          Status: {reconnecting ? "Reconnecting…" : tick?.stream_status || "—"} · Tick age{" "}
          {tick?.tick_age_s ?? "—"}s · Transport auto · Stale gate:{" "}
          {tick?.market_state === "STALE" ? "active" : "clear"}
        </p>
      </div>

      <div className="card">
        <p className="label-caps mb-2">REST</p>
        <p className="text-[12px] text-muted">
          Calls/min: {tick?.rest_calls_min ?? 0}/6 · Errors: {tick?.errors?.count ?? 0}{" "}
          {tick?.errors?.type || ""}
        </p>
      </div>

      <div className="card">
        <p className="label-caps mb-2">Operational</p>
        <p className="text-[12px] text-muted">
          Branch: {sys?.branch || "—"} · Commit: {sys?.commit || "—"} · E2E: pending Stage 2
        </p>
      </div>

      <div className="card">
        <p className="label-caps mb-2">ML data store</p>
        <p className="text-[12px] text-muted">
          Records: {sys?.ml_record_count ?? 0} · Fields: {sys?.ml_fields ?? 26} per trade
        </p>
        <p className="text-[11px] text-muted mt-1 break-all">{sys?.ml_store_path || "—"}</p>
      </div>

      <div className="card">
        <button
          type="button"
          disabled={!!busy}
          className="px-4 py-2 rounded border border-blue text-blue"
          onClick={async () => {
            setBusy("tests");
            try {
              setTests(await api.runTests());
            } catch (e) {
              setTests({ ok: false, error: e.message });
            } finally {
              setBusy("");
            }
          }}
        >
          Run system tests
        </button>
        {tests && (
          <p className={`mt-2 text-[13px] ${tests.ok ? "text-green" : "text-red"}`}>
            {tests.ok
              ? `PASS — ${tests.passed} passed`
              : `FAIL — ${tests.failed} failed · ${tests.summary || tests.error || ""}`}
          </p>
        )}
      </div>
    </div>
  );
}
