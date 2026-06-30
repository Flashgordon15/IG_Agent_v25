import { useEffect } from "react";
import { useCockpit } from "./CockpitProvider";

export function useKeyboardShortcuts() {
  const { setPanelFocus } = useCockpit();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.metaKey) return;
      switch (e.key.toLowerCase()) {
        case "l":
          e.preventDefault();
          setPanelFocus("logs");
          break;
        case "r":
          e.preventDefault();
          setPanelFocus("routing");
          break;
        case "s":
          e.preventDefault();
          setPanelFocus("strategy");
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPanelFocus]);
}
