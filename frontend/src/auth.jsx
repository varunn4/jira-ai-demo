import { createContext, useContext, useEffect, useState, useCallback } from "react";
import { apiFetch, getToken, setToken, onUnauthorized } from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    setToken("");
    setUser(null);
  }, []);

  // Validate any stored token on first load by fetching the current user.
  useEffect(() => {
    let active = true;
    async function bootstrap() {
      if (!getToken()) {
        setLoading(false);
        return;
      }
      try {
        const me = await apiFetch("/auth/me");
        if (active) setUser(me);
      } catch {
        if (active) {
          setToken("");
          setUser(null);
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  // Any 401 from the API clears the session.
  useEffect(() => onUnauthorized(() => logout()), [logout]);

  const login = useCallback(async (email, password) => {
    const data = await apiFetch("/auth/login", {
      method: "POST",
      body: { email, password },
    });
    setToken(data.access_token);
    setUser(data.user);
    return data.user;
  }, []);

  const hasTab = useCallback(
    (tab) => !!user && Array.isArray(user.permissions) && user.permissions.includes(tab),
    [user],
  );

  const value = { user, loading, login, logout, hasTab, isAdmin: user?.role === "admin" };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
