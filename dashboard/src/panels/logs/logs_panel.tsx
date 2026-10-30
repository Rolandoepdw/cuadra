import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Database, Megaphone, MessagesSquare, ServerCog, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { listLogs } from "@/lib/api";
import type { LogCategory, LogEntry, LogWindow } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { LogsTab } from "./logs_tab";

const PAGE_SIZE = 50;

const TABS = [
  { id: "publications", label: "Publicaciones", icon: Megaphone },
  { id: "conversations", label: "Conversaciones", icon: MessagesSquare },
  { id: "entities", label: "Cambios", icon: Database },
  { id: "system", label: "Sistema", icon: ServerCog },
] as const satisfies readonly { id: LogCategory; label: string; icon: LucideIcon }[];

export function LogsPanel() {
  const [tab, setTab] = useState<LogCategory>("publications");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  // Seven days shows recent activity even after a short downtime, while still
  // keeping the first load bounded.
  const [range, setRange] = useState<LogWindow>("7d");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [selected, setSelected] = useState<LogEntry | null>(null);
  const [counts, setCounts] = useState<Record<LogCategory, number>>();

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Every filter change starts again from the first page.
  useEffect(() => {
    setLimit(PAGE_SIZE);
    setSelected(null);
  }, [tab, debouncedSearch, range]);

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["logs", tab, debouncedSearch, range, limit],
    queryFn: () =>
      listLogs({
        category: tab,
        search: debouncedSearch,
        window: range,
        limit,
      }),
    refetchInterval: autoRefresh ? 15_000 : false,
    // Keep the table while a new page loads, but never show the previous
    // category's rows under a different tab.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === tab ? previous : undefined,
  });

  // Counts do not depend on the active tab; keep the last known values so the
  // badges do not flash to zero while switching windows.
  useEffect(() => {
    if (data?.counts) setCounts(data.counts);
  }, [data]);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Logs</h1>
        <p className="text-sm text-muted-foreground">
          Auditoría resumida de publicaciones, conversaciones, cambios y sistema.
        </p>
      </div>

      <div className="flex gap-1 overflow-x-auto border-b">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={cn(
              "-mb-px inline-flex items-center gap-2 whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              tab === id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-4 w-4" />
            {label}
            <Badge variant={tab === id ? "default" : "secondary"} className="tabular-nums">
              {counts?.[id] ?? 0}
            </Badge>
          </button>
        ))}
      </div>

      <LogsTab
        data={data}
        isLoading={isLoading}
        isFetching={isFetching}
        error={(error as Error | null) ?? null}
        search={search}
        onSearchChange={setSearch}
        range={range}
        onRangeChange={setRange}
        autoRefresh={autoRefresh}
        onAutoRefreshChange={setAutoRefresh}
        onRefresh={() => void refetch()}
        onLoadMore={() => setLimit((current) => current + PAGE_SIZE)}
        selected={selected}
        onSelect={setSelected}
      />
    </div>
  );
}
