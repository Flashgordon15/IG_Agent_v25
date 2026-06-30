import { useEffect } from "react";
import ChartStack from "./ChartStack";
import LogsPanel from "./LogsPanel";
import RiskPanel from "./RiskPanel";
import RoutingPanel from "./RoutingPanel";
import StatusMatrix from "./StatusMatrix";
import SystemHealthWidget from "./SystemHealthWidget";
import TopBar from "./TopBar";
import IgBudgetBanner from "./IgBudgetBanner";
import UnifiedTradingPanels from "./UnifiedTradingPanels";
import TradingPathPanel from "./TradingPathPanel";
import { useCockpit } from "../hooks/CockpitProvider";

export default function CockpitLayout() {
  const { panelFocus } = useCockpit();

  useEffect(() => {
    if (!panelFocus) return;
    const id =
      panelFocus === "logs"
        ? "panel-logs"
        : panelFocus === "routing"
          ? "panel-routing"
          : "panel-strategy";
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [panelFocus]);

  return (
    <div className="flex h-full flex-col bg-bg">
      <IgBudgetBanner />
      <TopBar />

      <div className="grid min-h-0 flex-1 grid-cols-[240px_1fr_300px] gap-2 p-2">
        <aside className="min-h-0 overflow-hidden">
          <StatusMatrix />
        </aside>

        <main className="min-h-0 overflow-hidden">
          <ChartStack />
        </main>

        <aside className="flex min-h-0 flex-col gap-2 overflow-hidden">
          <SystemHealthWidget />
          <TradingPathPanel />
          <UnifiedTradingPanels />
          <RoutingPanel />
          <RiskPanel />
          <LogsPanel />
        </aside>
      </div>
    </div>
  );
}
