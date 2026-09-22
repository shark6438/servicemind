"use client";

import type Keycloak from "keycloak-js";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { publicConfig } from "@/lib/config";

type AuthState = { ready: boolean; authenticated: boolean; username: string; roles: ReadonlySet<string>;
  tenantId: string | null; login: () => Promise<void>; logout: () => Promise<void>; getToken: () => Promise<string> };
const AuthContext = createContext<AuthState | null>(null);
let keycloakPromise: Promise<Keycloak> | null = null;

async function getKeycloak(): Promise<Keycloak> {
  keycloakPromise ??= import("keycloak-js").then(({ default: KeycloakConstructor }) => new KeycloakConstructor({
    url: publicConfig.keycloakUrl, realm: publicConfig.keycloakRealm, clientId: publicConfig.keycloakClientId,
  }));
  return keycloakPromise;
}

function claimsFrom(client: Keycloak) {
  const parsed = client.tokenParsed as { preferred_username?: string; tenant_id?: string;
    realm_access?: { roles?: string[] } } | undefined;
  return { username: parsed?.preferred_username ?? "已认证用户", tenantId: parsed?.tenant_id ?? null,
    roles: new Set(parsed?.realm_access?.roles ?? []) };
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [identity, setIdentity] = useState(() => ({ username: "", tenantId: null as string | null, roles: new Set<string>() }));
  useEffect(() => {
    let active = true;
    void getKeycloak().then(async (client) => {
      if (!client.didInitialize) await client.init({ onLoad: "check-sso", pkceMethod: "S256", checkLoginIframe: false });
      if (!active) return;
      setAuthenticated(Boolean(client.authenticated)); setIdentity(claimsFrom(client));
      client.onTokenExpired = () => { void client.updateToken(30).catch(() => client.login()); };
    }).catch(() => { if (active) setAuthenticated(false); }).finally(() => { if (active) setReady(true); });
    return () => { active = false; };
  }, []);
  const login = useCallback(async () => { const client = await getKeycloak(); await client.login({ redirectUri: window.location.href }); }, []);
  const logout = useCallback(async () => { const client = await getKeycloak(); await client.logout({ redirectUri: window.location.origin }); }, []);
  const getToken = useCallback(async () => {
    const client = await getKeycloak();
    if (!client.authenticated) throw new Error("登录已失效，请重新登录");
    await client.updateToken(30);
    if (!client.token) throw new Error("无法获取访问令牌");
    return client.token;
  }, []);
  const value = useMemo<AuthState>(() => ({ ready, authenticated, ...identity, login, logout, getToken }),
    [ready, authenticated, identity, login, logout, getToken]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
