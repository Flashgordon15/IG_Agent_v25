import CockpitLayout from "./components/CockpitLayout";
import ErrorBanner from "./components/ErrorBanner";
import SplashScreen from "./components/SplashScreen";
import { TooltipProvider } from "./components/ui/Controls";
import { CockpitProvider, useCockpit } from "./hooks/CockpitProvider";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";

function CockpitShell() {
  const { showSplash } = useCockpit();
  useKeyboardShortcuts();

  return (
    <>
      {showSplash && <SplashScreen />}
      <div
        className={`flex h-full flex-col transition-all duration-700 ease-out ${
          showSplash
            ? "pointer-events-none translate-y-2 opacity-0"
            : "translate-y-0 opacity-100"
        }`}
      >
        <ErrorBanner />
        <div className="min-h-0 flex-1">
          <CockpitLayout />
        </div>
      </div>
    </>
  );
}

function App() {
  return (
    <TooltipProvider>
      <CockpitProvider>
        <CockpitShell />
      </CockpitProvider>
    </TooltipProvider>
  );
}

export default App;
