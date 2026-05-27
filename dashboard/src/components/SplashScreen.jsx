export default function SplashScreen({ version, buildDate, onDismiss }) {
  const improvements = [
    "Web dashboard — no beach ball",
    "Tiered confidence — more trades",
    "Automatic single-click start",
    "ML data store — v26 ready",
    "Points-based self-correction",
  ];

  return (
    <div className="fixed inset-0 z-50 flex min-h-screen flex-col bg-[#0d1117] px-8 py-10 text-[13px]">
      <div className="mx-auto flex max-w-xl flex-1 flex-col justify-center">
        <p className="label-caps mb-2 text-blue">IG Agent v25</p>
        <h1 className="mb-1 text-2xl font-medium text-white">IG Agent v25</h1>
        <p className="mb-6 text-muted">
          Version {version}
          {buildDate ? ` · Build ${buildDate}` : ""}
        </p>

        <div className="mb-8 space-y-4 text-muted">
          <div>
            <p className="mb-2 font-medium text-white">Key improvements from v24</p>
            <ul className="list-disc space-y-2 pl-5">
              {improvements.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>

          <div>
            <p className="mb-2 font-medium text-white">How to operate</p>
            <p>
              One icon click starts everything — the trading engine and dashboard run
              together. Your browser opens automatically to this screen at{" "}
              <span className="text-white">http://localhost:8080</span>. Use System →
              Start DEMO when you are ready to trade.
            </p>
          </div>

          <div>
            <p className="mb-2 font-medium text-white">What to expect</p>
            <p>
              The master health badge begins as <span className="text-amber">WATCHING</span>{" "}
              while gates arm. The stream may show STALE briefly on startup — the stale
              gate self-clears within 15 seconds. Trading continues even if this UI is
              closed; the splash is display-only.
            </p>
          </div>
        </div>

        <button
          type="button"
          onClick={onDismiss}
          className="w-full rounded-lg bg-[#3fb950] py-3 text-[13px] font-medium text-[#0d1117] hover:opacity-90"
        >
          Start trading
        </button>
      </div>
    </div>
  );
}
