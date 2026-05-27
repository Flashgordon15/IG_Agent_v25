import { useCallback, useEffect, useState } from "react";
import SplashScreen from "./components/SplashScreen";
import StatusBar from "./components/StatusBar";
import LiveTab from "./tabs/LiveTab";
import TradesTab from "./tabs/TradesTab";
import PointsTab from "./tabs/PointsTab";
import SystemTab from "./tabs/SystemTab";
import { api } from "./api/client";
import { useWebSocket } from "./hooks/useWebSocket";

const TABS = [
  { id: "live", label: "Live" },
  { id: "trades", label: "Trades" },
  { id: "points", label: "Points" },
  { id: "system", label: "System" },
];

export default function App() {
  const [tab, setTab] = useState("live");
  const [splash, setSplash] = useState(null);
  const { tick, reconnecting } = useWebSocket();

  useEffect(() => {
    api.splash().then((s) => {
      if (!s.shown) setSplash(s);
    });
  }, []);

  const dismissSplash = useCallback(async () => {
    await api.dismissSplash();
    setSplash(null);
  }, []);

  const goSystem = useCallback(() => setTab("system"), []);

  if (splash) {
    return (
      <SplashScreen
        version={splash.version || "25.1.0"}
        buildDate={splash.build_date}
        onDismiss={dismissSplash}
      />
    );
  }

  return (
    <div className="min-h-screen flex flex-col">
      <StatusBar tick={tick} reconnecting={reconnecting} onErrorsClick={goSystem} />
      <nav className="flex border-b border-border bg-surface px-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`tab-btn ${tab === t.id ? "tab-btn-active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <main className="flex-1 overflow-y-auto">
        {tab === "live" && <LiveTab tick={tick} />}
        {tab === "trades" && <TradesTab />}
        {tab === "points" && <PointsTab tick={tick} />}
        {tab === "system" && <SystemTab tick={tick} reconnecting={reconnecting} />}
      </main>
    </div>
  );
}
