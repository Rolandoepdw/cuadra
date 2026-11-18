import type { ConversationMessage } from "@/types/api";
import { formatDateTime, formatMs } from "@/lib/format";
import { cn } from "@/lib/utils";
import { MESSAGE_AUTHOR_LABELS } from "./labels";

export function MessageBubble({ message }: { message: ConversationMessage }) {
  const inbound = message.direction === "INBOUND";

  return (
    <div className={cn("flex", inbound ? "justify-start" : "justify-end")}>
      <div
        className={cn(
          "max-w-[85%] rounded-lg border px-3 py-2 text-sm",
          inbound ? "bg-muted/40" : "bg-primary/10",
        )}
      >
        <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
          <span className="font-medium">
            {MESSAGE_AUTHOR_LABELS[message.author] ?? message.author}
          </span>
          <span>{formatDateTime(message.created_at)}</span>
          {!inbound && message.latency_ms != null && (
            <span>{formatMs(message.latency_ms)}</span>
          )}
        </div>
        <p className="whitespace-pre-wrap break-words">
          {message.body ?? (
            <span className="italic text-muted-foreground">[{message.type}]</span>
          )}
        </p>
        {message.file_path && (
          <p className="mt-1 text-xs text-muted-foreground">{message.file_path}</p>
        )}
      </div>
    </div>
  );
}
