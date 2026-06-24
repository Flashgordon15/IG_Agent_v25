export const EPIC_LABELS: Record<string, string> = {
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "IX.D.DOW.IFM.IP": "WALL ST",
  "IX.D.NIKKEI.IFM.IP": "NIKKEI",
  "CS.D.EURUSD.CFD.IP": "EUR/USD",
  "CS.D.CRUDE.CFD.IP": "CRUDE",
  "IX.D.FTSE.IFM.IP": "FTSE",
  "IX.D.DAX.IFM.IP": "DAX",
};

export const CORRELATION_EPICS = [
  "CS.D.CFPGOLD.CFP.IP",
  "IX.D.DOW.IFM.IP",
  "IX.D.NIKKEI.IFM.IP",
  "CS.D.EURUSD.CFD.IP",
] as const;

export const BASE_MID: Record<string, number> = {
  "CS.D.CFPGOLD.CFP.IP": 2650.0,
  "IX.D.DOW.IFM.IP": 39500.0,
  "IX.D.NIKKEI.IFM.IP": 39000.0,
  "CS.D.EURUSD.CFD.IP": 1.085,
  "CS.D.CRUDE.CFD.IP": 78.5,
  "IX.D.FTSE.IFM.IP": 8200.0,
  "IX.D.DAX.IFM.IP": 18200.0,
};

export const LEVERAGE_TILES = [2, 5, 10, 50] as const;

export const STREAM_STALE_MS = 45_000;
export const FULFILLMENT_STALE_MS = 2_500;
