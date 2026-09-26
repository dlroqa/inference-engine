import { createContext, useCallback, useContext, useMemo, useState, type JSX, type ReactNode } from "react";
import { getShowWiring, setShowWiring as saveShowWiring } from "../lib/prefs";

// "Show wiring": reveals a compact METHOD /path chip beside every "How this
// works" button. Off by default; remembered in this browser only.

interface WiringPrefs {
  showWiring: boolean;
  setShowWiring: (on: boolean) => void;
}

const WiringPrefsContext = createContext<WiringPrefs>({ showWiring: false, setShowWiring: () => {} });

export function useWiringPrefs(): WiringPrefs {
  return useContext(WiringPrefsContext);
}

export function WiringPrefsProvider({ children }: { children: ReactNode }): JSX.Element {
  const [showWiring, setState] = useState(getShowWiring);
  const setShowWiring = useCallback((on: boolean) => {
    saveShowWiring(on);
    setState(on);
  }, []);
  const value = useMemo(() => ({ showWiring, setShowWiring }), [showWiring, setShowWiring]);
  return <WiringPrefsContext.Provider value={value}>{children}</WiringPrefsContext.Provider>;
}
