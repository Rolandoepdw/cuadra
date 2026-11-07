import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/query";
import { AuthProvider, useAuth } from "@/lib/auth";
import { LoginGate } from "@/components/layout/login_gate";
import { AppShell } from "@/components/layout/app_shell";
import { GeneralPanel } from "@/panels/general/general_panel";
import { CatalogPanel } from "@/panels/catalog/catalog_panel";
import { HealthPanel } from "@/panels/health/health_panel";
import { GroupsPanel } from "@/panels/groups/groups_panel";
import { BroadcastsPanel } from "@/panels/broadcasts/broadcasts_panel";
import { StrategyPanel } from "@/panels/strategy/strategy_panel";
import { FinancePanel } from "@/panels/finance/finance_panel";
import { LogsPanel } from "@/panels/logs/logs_panel";

function Gated() {
  const { authed } = useAuth();
  if (!authed) return <LoginGate />;
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<GeneralPanel />} />
        <Route path="catalog" element={<CatalogPanel />} />
        <Route path="groups" element={<GroupsPanel />} />
        <Route path="broadcasts" element={<BroadcastsPanel />} />
        <Route path="strategy" element={<StrategyPanel />} />
        <Route path="finance" element={<FinancePanel />} />
        <Route path="logs" element={<LogsPanel />} />
        <Route path="health" element={<HealthPanel />} />
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

