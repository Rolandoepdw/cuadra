import { NavLink } from "react-router-dom";
import {
  Activity,
  History,
  LayoutDashboard,
  LogOut,
  Megaphone,
  MessagesSquare,
  Package,
  ScrollText,
  SlidersHorizontal,
  Users,
  Wallet,
  Code,
  BarChart3,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

const items = [
  { to: "/", label: "General", icon: LayoutDashboard, end: true },
  { to: "/conversations", label: "Conversaciones", icon: MessagesSquare, end: false },
  { to: "/catalog", label: "Catálogo", icon: Package, end: false },
  { to: "/groups", label: "Grupos", icon: Users, end: false },
  { to: "/broadcasts", label: "Difusiones", icon: Megaphone, end: false },
  { to: "/strategy", label: "Estrategia", icon: SlidersHorizontal, end: false },
  { to: "/finance", label: "Finanzas", icon: Wallet, end: false },
  { to: "/audit", label: "Auditoría", icon: History, end: false },
  { to: "/logs", label: "Logs", icon: ScrollText, end: false },
  { to: "/traces", label: "Trazas", icon: Code, end: false },
  { to: "/metrics", label: "Métricas", icon: BarChart3, end: false },
  { to: "/health", label: "Salud", icon: Activity, end: false },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { logout } = useAuth();

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-center px-3 py-4">
        <img src="/logo.png" alt="Cuadra" className="w-32 rounded-lg" />
      </div>
      <nav className="flex flex-1 flex-col gap-1 p-3">
        {items.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t p-3">
        <Button variant="ghost" size="sm" className="w-full justify-start" onClick={logout}>
          <LogOut />
          Salir
        </Button>
      </div>
    </div>
  );
}

