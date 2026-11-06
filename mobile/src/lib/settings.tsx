import * as SecureStore from "expo-secure-store";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiClient } from "./api";

const URL_KEY = "press.backendUrl";
const TOKEN_KEY = "press.token";

type SettingsValue = {
  ready: boolean;
  backendUrl: string;
  token: string;
  configured: boolean;
  api: ApiClient;
  save: (backendUrl: string, token: string) => Promise<void>;
};

const SettingsContext = createContext<SettingsValue | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [backendUrl, setBackendUrl] = useState("");
  const [token, setToken] = useState("");

  useEffect(() => {
    (async () => {
      const [url, tok] = await Promise.all([SecureStore.getItemAsync(URL_KEY), SecureStore.getItemAsync(TOKEN_KEY)]);
      setBackendUrl(url ?? "");
      setToken(tok ?? "");
      setReady(true);
    })();
  }, []);

  const save = useCallback(async (url: string, tok: string) => {
    const cleaned = url.trim().replace(/\/+$/, "");
    await SecureStore.setItemAsync(URL_KEY, cleaned);
    await SecureStore.setItemAsync(TOKEN_KEY, tok.trim());
    setBackendUrl(cleaned);
    setToken(tok.trim());
  }, []);

  const value = useMemo<SettingsValue>(
    () => ({
      ready,
      backendUrl,
      token,
      configured: Boolean(backendUrl && token),
      api: new ApiClient(backendUrl, token),
      save,
    }),
    [ready, backendUrl, token, save],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used inside <SettingsProvider>");
  return ctx;
}
