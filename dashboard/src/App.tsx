import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/query";
import { AuthProvider, useAuth } from "@/lib/auth";
import { LoginGate } from "@/components/layout/login-gate";
import { AppShell } from "@/components/layout/app-shell";
import { GeneralPanel } from "@/paneles/general/general-panel";
import { CatalogoPanel } from "@/paneles/catalogo/catalogo-panel";
import { SaludPanel } from "@/paneles/salud/salud-panel";

function Gated() {
  const { authed } = useAuth();
  if (!authed) return <LoginGate />;
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<GeneralPanel />} />
        <Route path="catalogo" element={<CatalogoPanel />} />
        <Route path="salud" element={<SaludPanel />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter>
          <Gated />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}
