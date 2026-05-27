import { useEffect, useState } from "react";
import { api } from "../api/client";

const E2E_STORAGE_KEY = "ig_agent_v25_last_e2e";

function loadLastE2e() {
  try {
    const raw = sessionStorage.getItem(E2E_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveLastE2e(result) {
  try {
    sessionStorage.setItem(
      E2E_STORAGE_KEY,
      JSON.stringify({
        ok: result.ok,
        summary: result.summary || result.error || "",
        at: new Date().toISOString(),
      }),
    );
  } catch {
    /* ignore */
  }
}

function formatCaffeinate(sys) {
  if (sys?.caffeinate_pid != null) {
    return `running (pid ${sys.caffeinate_pid})`;
  }
  if (sys?.caffeinate === true || sys?.caffeinate_running === true) {
    return "running";
  }
  if (sys?.caffeinate === false || sys?.caffeinate_running === false) {
    return "NOT RUNNING";
  }
  return "NOT RUNNING";
}

function formatSessionsPassed(sys) {
  if (sys?.sessions_passed != null) {
    const required = sys.sessions_required ?? 3;
    return `${sys.sessions_passed} of ${required}`;
  }
  return "—";
}

function formatLastE2e(lastE2e, e2e) {
  if (e2e) {
    return e2e.ok ? `PASS — ${e2e.summary || "OK"}` : `FAIL — ${e2e.summary || e2e.error || ""}`;
  }
  if (lastE2e) {
    const when = lastE2e.at ? new Date(lastE2e.at).toLocaleString() : "";
    const status = lastE2e.ok ? "PASS" : "FAIL";
    return `${status} — ${lastE2e.summary || ""}${when ? ` (${when})` : ""}`;
  }
  return "— (run E2E check below)";
}

export default function SystemTab({ tick, reconnecting }) {
  const [sys, setSys] = useState(null);
  const [tests, setTests] = useState(null);
  const [e2e, setE2e] = useState(null);
  const [lastE2e, setLastE2e] = useState(() => loadLastE2e());
  const [busy, setBusy] = useState("");
  const [emergConfirm, setEmergConfirm] = useState(false);

  useEffect(() => {
    api.system().then(setSys).catch(() => {});
    setLastE2e(loadLastE2e());
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
          {tick?.market_state === "MAINTENANCE"
            ? "maintenance (no REST)"
            : tick?.market_state === "STALE"
              ? "active"
              : "clear"}
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
        <dl className="text-[12px] space-y-1.5">
          <div className="flex justify-between gap-4">
            <dt className="text-muted">Caffeinate</dt>
            <dd className="text-white text-right">{formatCaffeinate(sys)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-muted">Sessions passed (Stage 2)</dt>
            <dd className="text-white text-right">{formatSessionsPassed(sys)}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-muted">E2E last result</dt>
            <dd
              className={`text-right max-w-[65%] leading-snug ${
                (e2e || lastE2e)?.ok === false ? "text-red" : (e2e || lastE2e)?.ok ? "text-green" : "text-muted"
              }`}
            >
              {formatLastE2e(lastE2e, e2e)}
            </dd>
          </div>
        </dl>
        <p className="text-[10px] text-muted mt-2 leading-snug">
          Branch: {sys?.branch || "—"} · Commit: {sys?.commit || "—"}
          {!sys?.caffeinate_pid && !sys?.sessions_passed ? " · host metrics not in /api/system" : ""}
        </p>
      </div>

      <div className="card">
        <p className="label-caps mb-2">ML data store</p>
        <p className="text-[12px] text-muted">
          Records: {sys?.ml_record_count ?? 0} · Fields: {sys?.ml_fields ?? 26} per trade
        </p>
        <p className="text-[11px] text-muted mt-1 break-all">{sys?.ml_store_path || "—"}</p>
      </div>

      <div className="card space-y-3">
        <p className="label-caps">Diagnostics</p>
        <p className="text-[11px] text-muted">
          E2E check validates the execution path (mock) and IG DEMO order routing (dry-run —
          no order placed). Full unit suite is separate.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={!!busy}
            className="px-4 py-2 rounded border border-blue text-blue font-medium"
            onClick={async () => {
              setBusy("e2e");
              setE2e(null);
              try {
                const result = await api.runE2eCheck();
                setE2e(result);
                saveLastE2e(result);
                setLastE2e(loadLastE2e());
              } catch (e) {
                const msg = e.message || String(e);
                const fail = {
                  ok: false,
                  error: msg,
                  summary: msg.includes("restart") ? msg : `${msg} — try restarting the agent`,
                  steps: [],
                };
                setE2e(fail);
                saveLastE2e(fail);
                setLastE2e(loadLastE2e());
              } finally {
                setBusy("");
              }
            }}
          >
            {busy === "e2e" ? "Running E2E…" : "Run E2E check"}
          </button>
          <button
            type="button"
            disabled={!!busy}
            className="px-4 py-2 rounded border border-blue text-blue"
            onClick={async () => {
              setBusy("tests");
              setTests(null);
              try {
                setTests(await api.runTests());
              } catch (e) {
                setTests({ ok: false, error: e.message });
              } finally {
                setBusy("");
              }
            }}
          >
            {busy === "tests" ? "Running tests…" : "Run all unit tests"}
          </button>
        </div>
        {e2e && (
          <div className="mt-2 space-y-2 text-[12px]">
            <p className={e2e.ok ? "text-green font-medium" : "text-red font-medium"}>
              E2E {e2e.ok ? "PASS" : "FAIL"} — {e2e.summary || e2e.error || ""}
            </p>
            {(e2e.steps || []).map((step) => (
              <div
                key={step.name}
                className={`rounded border px-3 py-2 ${
                  step.ok ? "border-green/40 bg-green/5" : "border-red/40 bg-red/5"
                }`}
              >
                <p className="font-medium text-text">
                  {step.name === "mock_pipeline" ? "Mock pipeline" : "IG DEMO routing"}
                </p>
                <p className="text-muted">{step.detail || step.summary || ""}</p>
                {step.name === "demo_routing" && step.ok && (
                  <p className="text-muted mt-1">
                    {step.epic} · {step.bid}/{step.offer} ({step.price_source || "rest"}) ·
                    balance {step.balance ?? "—"}
                  </p>
                )}
                {!step.ok && (
                  <p className="text-red mt-1">{step.error || step.summary}</p>
                )}
              </div>
            ))}
          </div>
        )}
        {tests && (
          <div className={`text-[13px] ${tests.ok ? "text-green" : "text-red"}`}>
            <p className="font-medium">
              Unit tests{" "}
              {tests.ok
                ? `PASS — ${tests.passed ?? 0} passed`
                : [
                    "FAIL",
                    tests.failed ? ` — ${tests.failed} failed` : "",
                    tests.errors ? ` — ${tests.errors} errors` : "",
                    !tests.failed && !tests.errors ? " — run did not complete" : "",
                  ].join("")}
            </p>
            {!tests.ok && (tests.error || tests.summary) && (
              <p className="text-muted mt-1 text-[11px] leading-snug">
                {tests.error || tests.summary}
              </p>
            )}
            {tests.ok && tests.summary && (
              <p className="text-muted mt-1 text-[11px]">{tests.summary}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
