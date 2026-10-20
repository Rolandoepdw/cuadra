import { createContext, useContext, useState, type ReactNode } from "react";
import { clearPin, getPin, hasPin, setPin } from "@/lib/pin";

interface AuthValue {
  authed: boolean;
  login: (pin: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean>(() => hasPin());

  const login = (pin: string) => {
    if (pin) setPin(pin);
    setAuthed(true);
  };
  const logout = () => {
    clearPin();
    setAuthed(false);
  };

  return <AuthContext.Provider value={{ authed, login, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

export function currentPin(): string {
  return getPin();
}
