import { NavLink } from "react-router-dom";
import { LayoutDashboard, Package, Activity } from "lucide-react";
import { cn } from "@/lib/utils";

const items = [
  { to: "/", label: "General", icon: LayoutDashboard, end: true },
  { to: "/catalogo", label: "Catálogo", icon: Package, end: false },
  { to: "/salud", label: "Salud", icon: Activity, end: false },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1 p-3">
      <div className="px-2 py-3">
        <span className="text-lg font-semibold tracking-tight">Cuadra</span>
      </div>
      {items.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              isActive ? "bg-secondary text-secondary-foreground" : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )
          }
        >
          <Icon className="h-4 w-4" />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
